# -*- coding: utf-8 -*-
"""更强 LLM 意图识别对照组评测（补 docs/BENCHMARK.md 明确声明的缺口）。

## 为什么需要这个脚本

`docs/BENCHMARK.md` 边界第 3 条原文承认：**"更强 LLM 的对照未测"**。
面试被追问"你凭什么说 96.42% 比 LLM 好"时，答案只能是"我用的那个 LLM 只有 64.57%"——
这恰恰把技术讨论拉低成"你赢是因为对手弱"。

本脚本用**当前可用的强模型**（glm-4.6 / qwen-max）做零样本意图分类，
在**同一测试集、同一类目、同一判分口径**下与 bge 原型集成正面对比。

## 判分口径（必须与 eval_prototype.py 一致，否则不可比）

- 测试集：test2_v4.json 的 new_items，剔除与 eval_prototype.py 完全相同的 7 条
- 类目：19 类 IntentCategory 的 value（与 bge 侧同一套）
- 准确率：逐条 top-1 命中率（accuracy = ok / n）
- macro_recall：各类召回率均值

## 用法

    <venv python> benchmarks/intent/eval_strong_llm.py --provider bigmodel --model glm-4.6
    <venv python> benchmarks/intent/eval_strong_llm.py --provider dashscope --model qwen-max
    <venv python> benchmarks/intent/eval_strong_llm.py --sample 60     # 抽样快速跑

产物：benchmarks/intent/results/strong_llm_<provider>_<model>.json
纪律：并发受控（默认 4），失败重试 2 次，逐条预测落盘可审计。
"""
import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
from core.intent_recognizer import IntentCategory

# 与 eval_prototype.py 逐字一致的剔除集（同一判分口径）
REMOVED = {
    ".中控室人员疏散集合点的设置和引导怎么做？", ".中控室人员疏散集合点的设置和引导咋做？",
    ".仓库人员疏散集合点的设置和引导怎么做？", ".车间人员疏散集合点的设置和引导怎么做？",
    ".装卸区人员疏散集合点的设置和引导怎么做？",
    "政府部门联合检查和专项检查的区别是什么？",
    "政府部门联合看和专项检查的区别是什么呗？",
}

PROVIDERS = {
    "bigmodel": {
        "base": "https://open.bigmodel.cn/api/anthropic",
        "key_env": "BIGMODEL_KEY",
        "default_key": "<REDACTED_RELAY_KEY>",
    },
    "dashscope": {
        "base": "https://dashscope.aliyuncs.com/apps/anthropic",
        "key_env": "DASHSCOPE_KEY",
        "default_key": "<REDACTED_DASHSCOPE_KEY>",
    },
}

LABELS = [c.value for c in IntentCategory]

SYSTEM = (
    "你是安全生产意图分类器。只输出一个 JSON，不要任何解释。\n"
    "从下面 19 个类目里选出**最匹配的一个**：\n"
    + "\n".join(f"- {v}" for v in LABELS) +
    '\n\n输出格式严格为：{"intent":"<类目名>"}'
)


def load_items(split="test2"):
    if split == "test2":
        data = json.load(open(os.path.join(HERE, "test2_v4.json"), encoding="utf-8"))["new_items"]
        return [d for d in data if d["text"] not in REMOVED]
    return [d for d in json.load(open(os.path.join(HERE, "intent_eval_v3.1.json"), encoding="utf-8"))["data"]
            if d["split"] in (split, "all")]


async def classify(client, model: str, text: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                r = await client.messages.create(
                    model=model, max_tokens=256, temperature=0.0, system=SYSTEM,
                    messages=[{"role": "user", "content": text}],
                    # 关键：强模型的思考块会把小额度预算吃光，正文一个字都不剩
                    # （同一根因在 core/tool_execution.py 也踩过，见 core/llm_params.py）。
                    # 意图分类只需要一个类目名，显式关闭思考既稳又快。
                    extra_body={"thinking": {"type": "disabled"}},
                )
                raw = "".join(getattr(b, "text", "") for b in r.content
                              if getattr(b, "type", "") == "text").strip()
                pred = None
                try:
                    obj = json.loads(raw)
                    pred = obj.get("intent")
                except Exception:
                    # 兜底：从文本里找类目名
                    for v in LABELS:
                        if v in raw:
                            pred = v
                            break
                return {"text": text, "raw": raw[:120], "pred": pred,
                        "ms": round((time.monotonic() - t0) * 1000, 1), "attempt": attempt}
            except Exception as e:
                if attempt == 2:
                    return {"text": text, "error": f"{type(e).__name__}: {str(e)[:150]}",
                            "pred": None, "ms": round((time.monotonic() - t0) * 1000, 1)}
                await asyncio.sleep(2 * (attempt + 1))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="bigmodel", choices=list(PROVIDERS))
    ap.add_argument("--model", default="glm-4.6")
    ap.add_argument("--split", default="test2")
    ap.add_argument("--sample", type=int, default=0, help="0=全量")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    cfg = PROVIDERS[args.provider]
    key = os.getenv(cfg["key_env"]) or cfg["default_key"]
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=key, base_url=cfg["base"], timeout=90, max_retries=0)

    items = load_items(args.split)
    if args.sample > 0:
        # 分层抽样：保证每类都出现（否则 macro 不可信）
        by = defaultdict(list)
        for d in items:
            by[d["label"]].append(d)
        picked, i = [], 0
        while len(picked) < min(args.sample, len(items)):
            added = False
            for lab in sorted(by):
                if i < len(by[lab]) and len(picked) < args.sample:
                    picked.append(by[lab][i]); added = True
            if not added:
                break
            i += 1
        items = picked

    print(f"provider={args.provider} model={args.model} split={args.split} n={len(items)}", flush=True)
    sem = asyncio.Semaphore(args.concurrency)
    t0 = time.time()
    results = await asyncio.gather(*[classify(client, args.model, d["text"], sem) for d in items])
    elapsed = time.time() - t0

    ok, n = 0, 0
    rec = defaultdict(lambda: [0, 0])
    errors = 0
    for d, r in zip(items, results):
        if r.get("pred") is None:
            errors += 1
            continue
        n += 1
        rec[d["label"]][1] += 1
        if r["pred"] == d["label"]:
            ok += 1
            rec[d["label"]][0] += 1
    acc = ok / n if n else 0
    macro = sum(a / b for a, b in rec.values() if b) / len([1 for a, b in rec.values() if b]) if rec else 0
    lat = sorted(r["ms"] for r in results if r.get("pred") is not None)
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0

    print(f"\naccuracy={acc:.4f}  macro_recall={macro:.4f}  n={n} 失败={errors}")
    print(f"单条延迟 p50={p50:.0f}ms p95={p95:.0f}ms（并发 {args.concurrency}，总耗时 {elapsed:.0f}s）")
    zero = [c for c, (a, b) in rec.items() if b and a == 0]
    print("零命中类:", zero if zero else "无")
    for c, (a, b) in sorted(rec.items()):
        print(f"  {c:24s} {a}/{b} = {a/b:.3f}")

    out = {
        "provider": args.provider, "model": args.model, "split": args.split,
        "n": n, "errors": errors, "accuracy": round(acc, 4), "macro_recall": round(macro, 4),
        "latency_p50_ms": round(p50, 1), "latency_p95_ms": round(p95, 1),
        "elapsed_s": round(elapsed, 1), "concurrency": args.concurrency,
        "per_class": {c: {"ok": a, "n": b, "recall": round(a / b, 4)} for c, (a, b) in sorted(rec.items())},
        "predictions": [{"text": d["text"], "label": d["label"], "pred": r.get("pred"),
                         "raw": r.get("raw"), "error": r.get("error"), "ms": r.get("ms")}
                        for d, r in zip(items, results)],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    path = os.path.join(HERE, "results", f"strong_llm_{args.provider}_{args.model.replace('/', '_')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nwritten {path}")
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
