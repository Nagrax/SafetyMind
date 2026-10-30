# -*- coding: utf-8 -*-
"""真实评测：RAG 注入片段的**精度**（引得多，是否真有用）。

## 为什么

/chat 每问注入约 3162 字、9 条引用（实测）。但"注入量"从不等于"有用"：
- 无关片段进入 prompt = 噪声，可能误导模型、也白烧 token 与延迟
- 更糟的是"看似相关但错位"（问动火票却注入受限空间票）——安全领域的依据错位

此前只测过"RAG 开/关"的整体质量差（yes/no），从未测过**逐片段精度**。

## 方法

1. 24 条真实安全问题，走生产同一条检索路径（kb.search_async，top_k=3）
2. 每条问题把它命中的 3 个片段编号交给裁判，问"哪些片段与问题直接相关"
3. 统计 precision@3、无关片段占比、最差案例（3 条全无关）
4. 同时记录注入字符数（代价侧）

## 决策规则

- precision@3 ≥ 80%：检索质量合格，不再优化（记录"已验证无问题"）
- 60%~80%：可接受，但记录可优化方向
- < 60%：应当加相关性过滤（阈值过滤/重排），并另起一轮做 A/B

用法:
    <venv python> benchmarks/eval_rag_precision.py --n 24
产物: benchmarks/results/rag_precision_<ts>.json
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
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
       "只输出 JSON：{\"rel\":{\"1\":0或1,\"2\":0或1,\"3\":0或1}}")


async def post(client, payload, timeout=300):
    for attempt in range(3):
        try:
            r = await client.post(GATEWAY + "/chat/completions",
                                  headers={"Authorization": "Bearer " + GKEY},
                                  json=payload, timeout=timeout)
            if r.status_code == 429:
                await asyncio.sleep(min(20, 2 * (attempt + 1) ** 2)); continue
            if r.status_code >= 400:
                if attempt == 2:
                    return {"error": f"HTTP{r.status_code}"}
                await asyncio.sleep(2); continue
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
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    qs = QUESTIONS[:args.n]
    sem = asyncio.Semaphore(args.concurrency)

    # 用应用 lifespan 起 KB（复用生产同一条检索路径，含嵌入器预热）
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
                t = time.perf_counter()
                res = await kb.search_async(q, top_k=args.top_k)
                ms = (time.perf_counter() - t) * 1000
                items = [r for r in (res or []) if isinstance(r, dict)]
                if not items:
                    rows.append({"i": i, "q": q, "sub": sub, "n": 0,
                                 "rel": [], "chars": 0, "ms": round(ms, 1),
                                 "scores": []})
                    print(f"[{i+1}/{len(qs)}] {sub:9s} 无命中", flush=True)
                    return
                chars = sum(len(str(it.get("snippet") or it.get("content") or ""))
                            for it in items)
                body = "\n\n".join(
                    f"片段 {k+1}:\n{str(it.get('snippet') or it.get('content') or '')[:700]}"
                    for k, it in enumerate(items))
                async with sem:
                    data = await post(client, {
                        "model": JUDGE_MODEL, "max_tokens": 1024, "temperature": 0,
                        "messages": [{"role": "system", "content": SYS},
                                     {"role": "user",
                                      "content": f"问题：{q}\n\n{body}"}]})
                rel = parse_rel(extract(data), len(items))
                rows.append({
                    "i": i, "q": q, "sub": sub, "n": len(items),
                    "rel": rel, "chars": chars, "ms": round(ms, 1),
                    "scores": [it.get("score") for it in items],
                    "titles": [str(it.get("title", ""))[:40] for it in items],
                })
                print(f"[{i+1}/{len(qs)}] {sub:9s} n={len(items)} rel={rel} "
                      f"{chars}字 {ms:.0f}ms", flush=True)

            await asyncio.gather(*[one(i, c, s, q)
                                   for i, (c, s, q) in enumerate(qs)])

    judged = [r for r in rows if r["rel"]]
    total = sum(len(r["rel"]) for r in judged)
    hit = sum(sum(r["rel"]) for r in judged)
    prec = hit / total if total else 0
    per_q = [sum(r["rel"]) / len(r["rel"]) for r in judged]
    worst = [r for r in judged if sum(r["rel"]) == 0]

    print("\n=== 汇总 ===")
    print(f"  题数 {len(rows)}（有命中 {len(judged)}），片段总数 {total}")
    print(f"  precision@{args.top_k} = {prec:.4f}（相关片段 {hit}/{total}）")
    print(f"  逐题均值 = {statistics.mean(per_q):.4f}；"
          f"全无关题数 = {len(worst)}")
    print(f"  注入字符中位 = {statistics.median([r['chars'] for r in rows]):.0f}")
    print(f"  检索耗时中位 = {statistics.median([r['ms'] for r in rows]):.0f}ms")
    for r in worst[:5]:
        print(f"  [全无关] {r['q'][:34]} → {r['titles']}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"rag_precision_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"top_k": args.top_k, "rows": rows,
                               "precision": round(prec, 4),
                               "judged": len(judged)}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    raise SystemExit(asyncio.run(main()))
