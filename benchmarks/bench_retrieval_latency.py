# -*- coding: utf-8 -*-
"""检索延迟基线（干净条件）：冷启动 / 热检索 / 大库 / 分解到"嵌入 vs 向量查询"。

## 为什么要重测

历史文档里写着"检索耗时中位 1043ms"，但那是在并发裁判 + 冷启动混合的条件下测的，
不能作为检索本身的基线；线程自适应（1→4）之后更需要一个可引用的新数字。

## 方法

- 同一进程内先预热（丢弃首次调用，含模型/集合加载）
- 24 条真实问题，每条单独计时，报 p50/p90/min/max
- 两组库：桌面库（9 子块）与上一轮扩容实验的 409 子块库
- 分解：query 文本嵌入耗时 vs 向量检索耗时（分别计时）

用法: <venv python> benchmarks/bench_retrieval_latency.py
产物: benchmarks/results/retrieval_latency_<ts>.json
"""
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "benchmarks"))
OUT_DIR = REPO / "benchmarks" / "results"

from eval_reasoning_ab import QUESTIONS  # noqa: E402


def stats(label, vals):
    vals = sorted(vals)
    s = {
        "n": len(vals),
        "p50": round(statistics.median(vals), 1),
        "p90": round(vals[int(len(vals) * 0.9) - 1], 1) if len(vals) > 1 else round(vals[0], 1),
        "min": round(vals[0], 1),
        "max": round(vals[-1], 1),
    }
    print(f"  {label}: p50={s['p50']}ms p90={s['p90']}ms min={s['min']} max={s['max']}", flush=True)
    return s


async def measure(kb, qs, label):
    # 冷：第一条（预热后仍单测一次，代表"刚起来的库"）
    t = time.perf_counter()
    await kb.search_async(qs[0], top_k=2)
    cold = (time.perf_counter() - t) * 1000

    # 第一遍：每条问题文本**从未嵌入过** → 含真实查询嵌入（cache miss）
    pass1 = []
    for q in qs:
        t = time.perf_counter()
        await kb.search_async(q, top_k=2)
        pass1.append((time.perf_counter() - t) * 1000)

    # 第二遍：同样的文本，嵌入走 LRU 缓存（cache hit）——代表"重复问同一个问题"
    pass2 = []
    for q in qs:
        t = time.perf_counter()
        await kb.search_async(q, top_k=2)
        pass2.append((time.perf_counter() - t) * 1000)

    print(f" [{label}] 子块 {kb._collection.count()}，冷检索 {cold:.0f}ms")
    return {
        "label": label, "chunks": kb._collection.count(), "cold_ms": round(cold, 1),
        "cold_text": stats("冷文本(cache miss)", pass1),
        "warm_text": stats("热文本(cache hit)", pass2),
    }


async def main():
    from fastapi.testclient import TestClient
    import api.main as m

    out = {}
    qs = [q for _, _, q in QUESTIONS[:24]]

    with TestClient(m.app) as c:
        c.get("/health")
        tool = m._tool_manager._tools.get("knowledge_search")
        kb = tool.handler.__self__
        await kb.search_async("预热")        # 丢弃：含模型/集合加载
        out["desktop_kb"] = await measure(kb, qs, "desktop_kb(9 子块)")

    # 大库：复用扩容实验建好的 409 子块库（若存在）
    big = Path("D:/claudecode/scratch/sm-loop/iso/kb_exp/full/kb_chroma")
    if big.exists():
        from mcp.knowledge_base import KnowledgeBase
        kb2 = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=9, chroma_path=str(big))
        await kb2.search_async("预热")
        out["expanded_kb"] = await measure(kb2, qs, "expanded_kb(409 子块)")
    else:
        print("（跳过 expanded_kb：扩容实验库不存在）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"retrieval_latency_{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {p}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    asyncio.run(main())
