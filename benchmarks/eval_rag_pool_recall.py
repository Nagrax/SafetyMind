# -*- coding: utf-8 -*-
"""真实评测：RAG 候选池的**召回上限**（先证伪 rerank 假设，再决定是否引入重排模型）。

## 为什么要先测这个

上一轮把注入片段从 3 降到 2、并加了分数过滤，precision@2 从 19.7% 升到 27.3%。
自然的下一步是"上 BGE 重排（rerank）"，但重排**只能改变候选池内部的顺序**，
不能创造候选池里本来没有的相关片段。

所以在引入任何重排模型（下载 1GB 权重 + CPU 前向 ~百毫秒）之前，必须先把
**oracle 上界**测出来：

    若候选池里每题只有 0~1 个相关片段，那么任何 rerank 的 precision@2 上限 ≈ 50%，
    再花代价也换不来质量 → 结论应转向"上游召回/问句改写"，而不是重排。

## 方法

1. 24 条真实安全问题，同一条生产检索路径，一次取 top_k=8 个候选（7 篇演示库基本全取）
2. 把**全部候选**编号交给同一个裁判，问"哪些片段与问题直接相关"
3. 统计：
   - pool_recall = 候选池里相关片段数（衡量召回侧天花板）
   - precision@1 / precision@2（当前余弦顺序，与 baseline 口径一致）
   - oracle@2 = 池内最优 2 条的相关率（**任何 rerank 的数学上限**）
   - gap = oracle@2 − precision@2（重排可争取的空间）
   - 无解题数（池内相关片段 = 0，rerank 无论如何都救不了）

## 决策规则（先定规则再看数据）

- gap < 10pp 或 无解题 ≥ 40%  → **不引入 rerank**，记录"该库规模下召回是瓶颈"，
  优化转向上游（问句改写 / 文档切分 / 库规模）。
- gap ≥ 10pp 且 无解题 < 40% → rerank 有可争取空间，另起一轮做 A/B（含延迟预算）。

用法:
    <venv python> benchmarks/eval_rag_pool_recall.py --n 24 --candidates 8
产物: benchmarks/results/rag_pool_recall_<ts>.json
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "benchmarks"))
OUT_DIR = REPO / "benchmarks" / "results"
GATEWAY = os.getenv("JUDGE_GATEWAY", "http://127.0.0.1:8326/v1")
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
GKEY = _judge_key()
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")

from eval_reasoning_ab import QUESTIONS  # noqa: E402

SYS = ("你是检索质量评审员。给定一个问题与若干编号资料片段，判断**每个片段是否与问题直接相关**"
       "（能用于回答该问题）。不要评价片段本身写得好不好。\n"
       "只输出 JSON：{\"rel\":{\"1\":0或1,\"2\":0或1,...}}，键覆盖每一个给定片段编号。")


async def post(client, payload, timeout=300):
    for attempt in range(3):
        try:
            r = await client.post(GATEWAY + "/chat/completions",
                                  headers={"Authorization": "Bearer " + GKEY},
                                  json=payload, timeout=timeout)
            if r.status_code == 429:
                await asyncio.sleep(min(20, 2 * (attempt + 1) ** 2))
                continue
            if r.status_code >= 400:
                if attempt == 2:
                    return {"error": f"HTTP{r.status_code}"}
                await asyncio.sleep(2)
                continue
            return r.json()
        except Exception:
            if attempt == 2:
                return {"error": "exc"}
            await asyncio.sleep(2)
    return {"error": "retries"}


def extract(d):
    if d.get("error"):
        return ""
    m = ((d.get("choices") or [{}])[0].get("message") or {})
    return (m.get("content") or "").strip() or (m.get("reasoning_content") or "").strip()


def parse_rel(txt, n):
    """解析裁判 JSON；缺键返回 None（不猜测、不补齐）。"""
    m = re.search(r"\{[\s\S]*\}", txt or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    rel = d.get("rel") or d
    out = []
    for i in range(1, n + 1):
        v = rel.get(str(i), rel.get(i))
        if v is None:
            return None
        out.append(1 if float(v) >= 0.5 else 0)
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--candidates", type=int, default=8)
    ap.add_argument("--top-k", type=int, default=2, help="生产实际注入条数")
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()
    qs = QUESTIONS[:args.n]
    sem = asyncio.Semaphore(args.concurrency)

    from fastapi.testclient import TestClient
    import api.main as m

    rows = []
    async with httpx.AsyncClient(timeout=400) as client:
        with TestClient(m.app) as c:
            c.get("/health")
            tool = m._tool_manager._tools.get("knowledge_search") if m._tool_manager else None
            if tool is None:
                print("knowledge_search 工具未注册，无法评测")
                return 1
            kb = tool.handler.__self__

            async def one(i, cat, sub, q):
                res = await kb.search_async(q, top_k=args.candidates)
                items = [r for r in (res or []) if isinstance(r, dict)]
                if not items:
                    rows.append({"i": i, "q": q, "sub": sub, "n": 0, "rel": None,
                                 "pool": 0, "p1": 0.0, "p2": 0.0, "oracle": 0.0})
                    print(f"[{i+1}/{len(qs)}] {sub:9s} 无命中", flush=True)
                    return
                body = "\n\n".join(
                    f"片段 {k+1}:\n{str(it.get('snippet') or it.get('content') or '')[:600]}"
                    for k, it in enumerate(items))
                async with sem:
                    data = await post(client, {
                        "model": JUDGE_MODEL, "max_tokens": 1024, "temperature": 0,
                        "messages": [{"role": "system", "content": SYS},
                                     {"role": "user", "content": f"问题：{q}\n\n{body}"}]})
                rel = parse_rel(extract(data), len(items))
                if rel is None:
                    rows.append({"i": i, "q": q, "sub": sub, "n": len(items), "rel": None,
                                 "pool": 0, "p1": 0.0, "p2": 0.0, "oracle": 0.0,
                                 "judge_error": True})
                    print(f"[{i+1}/{len(qs)}] {sub:9s} 裁判解析失败（不计入统计）", flush=True)
                    return
                k = min(args.top_k, len(rel))
                pool = sum(rel)
                p1 = rel[0]
                p2 = sum(rel[:k]) / k
                oracle = min(pool, k) / k
                rows.append({"i": i, "q": q, "sub": sub, "n": len(items), "rel": rel,
                             "pool": pool, "p1": float(p1), "p2": round(p2, 4),
                             "oracle": round(oracle, 4),
                             "titles": [str(it.get("title", ""))[:40] for it in items],
                             "scores": [it.get("score") for it in items]})
                print(f"[{i+1}/{len(qs)}] {sub:9s} n={len(items)} 池内相关={pool} "
                      f"p@{k}={p2:.2f} oracle={oracle:.2f}", flush=True)

            await asyncio.gather(*[one(i, c, s, q) for i, (c, s, q) in enumerate(qs)])

    judged = [r for r in rows if r.get("rel")]
    errs = [r for r in rows if r.get("rel") is None]
    p2s = [r["p2"] for r in judged]
    p1s = [r["p1"] for r in judged]
    ors = [r["oracle"] for r in judged]
    pools = [r["pool"] for r in judged]
    zero = [r for r in judged if r["pool"] == 0]

    print("\n=== 召回上限汇总 ===")
    print(f"  题数 {len(rows)}（计入统计 {len(judged)}，裁判失败 {len(errs)}）")
    if not judged:
        print("  无有效样本，不做结论")
        return 1
    print(f"  候选池相关片段：均值 {statistics.mean(pools):.2f} / 中位 {statistics.median(pools)}"
          f"（每题最多取 {min(args.top_k, args.candidates)} 条）")
    print(f"  precision@1 = {statistics.mean(p1s):.4f}")
    print(f"  precision@{args.top_k} = {statistics.mean(p2s):.4f}（余弦顺序，当前线上口径）")
    print(f"  oracle@{args.top_k} = {statistics.mean(ors):.4f}（**任何 rerank 的数学上限**）")
    print(f"  gap = {statistics.mean(ors) - statistics.mean(p2s):+.4f}")
    print(f"  池内无相关（rerank 无论如何救不了）：{len(zero)}/{len(judged)}"
          f" = {len(zero)/len(judged):.0%}")

    gap = statistics.mean(ors) - statistics.mean(p2s)
    unlockable = len(zero) / len(judged)
    if gap < 0.10 or unlockable >= 0.40:
        verdict = "no_rerank"   # 重排无空间：瓶颈在召回侧
        print("  裁决：**不引入 rerank** —— gap<10pp 或无解题≥40%，瓶颈在召回侧")
    else:
        verdict = "rerank_worth_ab"   # 有可争取空间，另起一轮做含延迟预算的 A/B
        print("  裁决：rerank 有可争取空间 → 另起一轮做 A/B（含 CPU 前向延迟预算）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"rag_pool_recall_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({
        "top_k": args.top_k, "candidates": args.candidates,
        "verdict": verdict, "gap": round(gap, 4),
        "unlockable_ratio": round(unlockable, 4),
        "mean_pool": round(statistics.mean(pools), 4),
        "p1": round(statistics.mean(p1s), 4),
        "p_at_k": round(statistics.mean(p2s), 4),
        "oracle": round(statistics.mean(ors), 4),
        "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    raise SystemExit(asyncio.run(main()))
