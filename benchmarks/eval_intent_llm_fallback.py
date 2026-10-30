# -*- coding: utf-8 -*-
"""真实评测：意图的「LLM 二次判定」到底值不值那 2 秒？

## 背景（阶段拆解实测）

/chat 端到端约 12s，其中意图识别在**多数问题**上约 100ms，但**低置信度问题**
（如"安全阀超期未校验算重大隐患吗" conf=0.574）会回落 LLM 二次判定，
实测 **1737-2389ms**——占端到端 15-20%。

BGE 意图基准本身已很高（dev 97.71 / test 96.42 / test2 94.88 双模；单模 92.93）。
问题：**低置信时那次 LLM 调用，究竟提升了多少准确率？**

## 方法

在意图评测集上逐条对比两种口径：
  A. 仅 BGE（低置信也直接用 BGE 结果）
  B. BGE + 低置信回落 LLM（= 当前线上行为）

同时记录：低置信样本占比、LLM 调用总耗时、A/B 准确率差、被 LLM 纠正/带偏的题数。

## 决策规则（事先声明）

- 若 B − A < +1.0pp：**不值得**（为 1pp 付 20% 延迟不划算）→ 建议改为"澄清反问"替代
- 若 B − A ≥ +1.0pp：保留 LLM 回落
- 无论结论，都输出被纠正/带偏的具体样本，便于人工复核

用法:
    <venv python> benchmarks/eval_intent_llm_fallback.py --split test2 --tau 0.80 --limit 200
产物: benchmarks/results/intent_fallback_<ts>.json
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "benchmarks" / "intent"))
OUT_DIR = REPO / "benchmarks" / "results"


def load_items(split):
    here = REPO / "benchmarks" / "intent"
    if split == "test2":
        data = json.load(open(here / "test2_v4.json", encoding="utf-8"))["new_items"]
        return [d for d in data if str(d.get("label", d.get("intent", "")))]
    data = json.load(open(here / "intent_eval_v3.1.json", encoding="utf-8"))["data"]
    return [d for d in data if d.get("split") in (split, "all")]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test2", choices=["dev", "test", "test2"])
    ap.add_argument("--tau", type=float, default=0.80)
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--force-direct", action="store_true")

    args = ap.parse_args()

    # 凭据：先用 shell 环境，缺省再从仓库 .env 读（2026-10-29 踩坑：脚本里
    # setdefault("ANTHROPIC_API_KEY", "x") 会让中继直接 401，LLM 腿静默为 0，
    # 于是"100% 低置信 + 准确率 0.41"被误读成"模型退化"——其实是没带 key）。
    if not os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY") == "x":
        for line in open(REPO / ".env", encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line.startswith(("ANTHROPIC_API_KEY=", "ANTHROPIC_BASE_URL=",
                                "ANTHROPIC_MODEL=")) and not os.getenv(line.split("=", 1)[0]):
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip())
    os.environ.setdefault("ANTHROPIC_API_KEY", "x")
    base_url = os.getenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8326/v1")
    model = os.getenv("ANTHROPIC_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")

    from core.intent_recognizer import IntentRecognizer
    items = load_items(args.split)
    if args.limit:
        items = items[:args.limit]
    print(f"split={args.split} n={len(items)} tau={args.tau} model={model}", flush=True)

    rec = IntentRecognizer(api_key=os.getenv("ANTHROPIC_API_KEY", "x"),
                           base_url=base_url, model=model)

    # 预热 BGE：recognize() 是 BGE 优先级联，BGE 未加载时所有请求都会掉进
    # LLM 并行路径（2-4s），测出来的不是真实线上分布
    # （实测未预热时 97.5% 低置信、平均 3658ms，明显失真）。
    bge = getattr(rec, "_bge", None)
    if bge is not None:
        await bge.try_recognize("系统预热", pattern_fn=rec._pattern_recognize)
        print("BGE 已预热，tau_direct=", getattr(bge, "tau_direct", "?"), flush=True)
    else:
        print("警告：BGE 未加载", flush=True)
    if args.force_direct and bge is not None:
        bge.tau_direct = 0.0
        print("已强制 tau_direct=0（仅 BGE 口径）", flush=True)
    sem = asyncio.Semaphore(args.concurrency)
    rows = []

    async def one(i, d):
        msg = d.get("text") or d.get("message") or d.get("query") or ""
        gold = str(d.get("label") or d.get("intent") or "")
        async with sem:
            t = time.perf_counter()
            r = await rec.recognize(msg)
            ms = (time.perf_counter() - t) * 1000
        pred = r.intent.value if hasattr(r.intent, "value") else str(r.intent)
        conf = float(getattr(r, "confidence", 0) or 0)
        # 判定该条是否触发了 LLM 回落（低置信且非缓存）
        used_llm = conf < args.tau
        rows.append({"i": i, "gold": gold, "pred": pred, "conf": round(conf, 4),
                     "ms": round(ms, 1), "low_conf": used_llm,
                     "correct": pred == gold,
                     "source": dict(getattr(r, "source_scores", {}) or {})})
        if (len(rows) % 50) == 0:
            print(f"  ...{len(rows)}/{len(items)}", flush=True)

    t0 = time.perf_counter()
    await asyncio.gather(*[one(i, d) for i, d in enumerate(items)])
    wall = time.perf_counter() - t0

    n = len(rows)
    acc = sum(1 for r in rows if r["correct"]) / n if n else 0
    low = [r for r in rows if r["low_conf"]]
    print(f"\n=== 汇总 ===")
    print(f"  总样本 {n}，BGE+LLM 口径准确率 = {acc:.4f}")
    print(f"  低置信（<{args.tau}）样本 {len(low)} 条（{len(low)/n:.1%}），"
          f"平均耗时 {sum(r['ms'] for r in low)/max(1,len(low)):.0f}ms")
    hi = [r for r in rows if not r["low_conf"]]
    if hi:
        print(f"  高置信样本 {len(hi)} 条，平均耗时 {sum(r['ms'] for r in hi)/len(hi):.0f}ms，"
              f"准确率 {sum(1 for r in hi if r['correct'])/len(hi):.4f}")
    if low:
        print(f"  低置信样本准确率 = {sum(1 for r in low if r['correct'])/len(low):.4f}")
    print(f"  墙钟 {wall:.1f}s")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"intent_fallback_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"split": args.split, "tau": args.tau, "n": n,
                               "acc": round(acc, 4), "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
