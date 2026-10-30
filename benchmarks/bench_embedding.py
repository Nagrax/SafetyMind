# -*- coding: utf-8 -*-
"""BGE 嵌入吞吐基准：线程数 × batch size（一次进程只测一组，避免进程全局设置串味）。

用法:
    <venv python> benchmarks/bench_embedding.py --threads 1 --batch 32 --n 100
    <venv python> benchmarks/bench_embedding.py --threads 4 --batch 64 --n 100

为什么需要（2026-10-29）：/chat 检索中位 ~1s、批量灌库实测 ~1.2s/条，
而本机有 20 个逻辑核、torch 默认只给 1 线程——先量清楚"加线程/改批量"能不能救回来，
再决定是否改默认值（改默认要同时评估对系统整体响应的影响）。
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("HF_HOME", "D:/hf_cache")

SEED = Path(__file__).resolve().parents[1] / "data/knowledge_seed/xlsx_docs.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    os.environ["SAFETYMIND_TORCH_THREADS"] = str(args.threads)
    from core.bge_embedding import BgeEmbeddingFunction
    import torch
    emb = BgeEmbeddingFunction(batch_size=args.batch)
    print(f"threads={torch.get_num_threads()} batch={args.batch} n={args.n}", flush=True)

    docs = json.loads(SEED.read_text(encoding="utf-8"))[:args.n]
    texts = [str(d.get("content") or "") for d in docs if str(d.get("content") or "").strip()]

    # 预热（模型首次推理含 JIT/内存分配）
    emb.embed_documents(texts[:min(8, len(texts))])

    runs = []
    for _ in range(args.repeat):
        t = time.perf_counter()
        emb.embed_documents(texts)
        runs.append(time.perf_counter() - t)
    best = min(runs)
    per = best / len(texts)
    print(json.dumps({
        "threads": torch.get_num_threads(), "batch": args.batch, "n": len(texts),
        "seconds": round(best, 1),
        "ms_per_text": round(per * 1000, 1),
        "texts_per_sec": round(len(texts) / best, 2),
        "all_runs": [round(r, 1) for r in runs],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
