# -*- coding: utf-8 -*-
"""真实评测：**生产注入链路**的 RAG 精度（口径修正）。

## 为什么需要这个脚本

上一轮 `eval_rag_precision.py` 只测了 `kb.search_async`（ChromaDB 7 篇演示库）这一条通道，
得出的 27.3% precision@2 被当作"生产 RAG 精度"。但 `/chat` 实际走的是
`_build_knowledge_context()`：**知识库通道 + corpus 通道（K07，1698 条自有资料）**
合并后注入。只测一条通道会让结论失真——低精度可能来自被绕过的通道，也可能来自
corpus 通道的噪声（corpus 命中同样会进 prompt）。

本脚本直接调用生产函数，逐条统计：
  - 实际注入几条引用、分别来自哪个通道（kb / corpus / catalog）
  - 裁判判定每条引用是否相关 → 生产口径 precision
  - 分通道 precision（哪条通道在贡献噪声）
  - 与 kb-only 口径的差值（量化"口径漏测"的幅度）

## 决策规则（先定规则再看数据）

- 生产 precision ≥ 60%：RAG 质量合格，上一轮"27.3%"的结论应更正为 kb-only 口径的结论，
  后续不再在检索侧继续投入，转做其他轴。
- 生产 precision < 60%：按通道定位噪声来源，另起一轮只改贡献噪声的那条通道。

用法:
    <venv python> benchmarks/eval_rag_context_precision.py --n 24
产物: benchmarks/results/rag_context_precision_<ts>.json
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

SYS = ("你是检索质量评审员。给定一个问题与若干编号资料片段（来自安全知识库/资料库），"
       "判断**每个片段是否与问题直接相关**（能用于回答该问题）。不要评价片段本身写得好不好。\n"
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


def channel_of(cit):
    if cit.get("source") == "corpus":
        return "catalog" if cit.get("catalog_only") else "corpus"
    return "kb"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--top-k", type=int, default=2)
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
            if m._tool_manager is None:
                print("工具管理器未初始化，无法评测")
                return 1

            async def one(i, cat, sub, q):
                text, used, cits = await m._build_knowledge_context(
                    q, intent=None, top_k=args.top_k)
                cits = [x for x in (cits or []) if isinstance(x, dict)]
                if not used or not cits:
                    rows.append({"i": i, "q": q, "sub": sub, "n": 0, "rel": None,
                                 "channels": [], "chars": 0})
                    print(f"[{i+1}/{len(qs)}] {sub:9s} 无注入", flush=True)
                    return
                body = "\n\n".join(
                    f"片段 {k+1}:\n{str(x.get('snippet') or '')[:600]}"
                    for k, x in enumerate(cits))
                async with sem:
                    data = await post(client, {
                        "model": JUDGE_MODEL, "max_tokens": 1024, "temperature": 0,
                        "messages": [{"role": "system", "content": SYS},
                                     {"role": "user", "content": f"问题：{q}\n\n{body}"}]})
                rel = parse_rel(extract(data), len(cits))
                channels = [channel_of(x) for x in cits]
                rows.append({"i": i, "q": q, "sub": sub, "n": len(cits), "rel": rel,
                             "channels": channels, "chars": len(text)})
                if rel is None:
                    print(f"[{i+1}/{len(qs)}] {sub:9s} 裁判解析失败（不计入统计）", flush=True)
                else:
                    print(f"[{i+1}/{len(qs)}] {sub:9s} n={len(cits)} "
                          f"rel={sum(rel)}/{len(rel)} ch={channels} {len(text)}字", flush=True)

            await asyncio.gather(*[one(i, c, s, q) for i, (c, s, q) in enumerate(qs)])

    judged = [r for r in rows if r.get("rel") is not None]
    if not judged:
        print("\n无有效样本，不做结论")
        return 1
    total = sum(len(r["rel"]) for r in judged)
    hit = sum(sum(r["rel"]) for r in judged)
    prec = hit / total if total else 0.0

    by_ch = {}
    for r in judged:
        for ch, v in zip(r["channels"], r["rel"]):
            d = by_ch.setdefault(ch, [0, 0])
            d[0] += v
            d[1] += 1

    print("\n=== 生产注入口径（知识库 + corpus）===")
    print(f"  题数 {len(rows)}（计入统计 {len(judged)}）")
    print(f"  引用总数 {total}，每题均值 {total/len(judged):.2f}")
    print(f"  production precision = {prec:.4f}（相关 {hit}/{total}）")
    for ch, (h, n) in sorted(by_ch.items()):
        print(f"  通道 {ch:8s}: {h}/{n} = {h/n:.4f}")
    print(f"  注入文本字符中位 = {statistics.median([r['chars'] for r in judged]):.0f}")
    verdict = "ok" if prec >= 0.60 else "noisy"
    print(f"  裁决：{'生产口径合格（≥60%）→ 更正上一轮 kb-only 结论' if prec >= 0.60 else '生产口径仍 <60% → 按通道定位噪声'}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"rag_context_precision_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({
        "top_k": args.top_k, "verdict": verdict,
        "precision": round(prec, 4), "judged": len(judged),
        "by_channel": {k: {"hit": v[0], "n": v[1]} for k, v in by_ch.items()},
        "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    raise SystemExit(asyncio.run(main()))
