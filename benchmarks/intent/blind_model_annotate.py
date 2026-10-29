# -*- coding: utf-8 -*-
"""人工盲测的"独立判定员"版：用真前沿模型做盲判，产出可审计的核验证据。

## 这解决什么问题

`benchmarks/intent/blind_sample.json` 的 50 条样本 + 答案 key 早已备好，
但 `blind_answers.json` 从未产生——也就是说**标签始终只有 AI 自标注，没有独立核验**。
面试官追问"96.42% 怎么测的""标签是谁标的"时，这是硬伤。

真正的"人工"盲测需要真人来做，我无法代替。但可以做到**次优且可审计**的一步：
让一个**从未见过答案 key** 的前沿模型独立判定这 50 条，
再与 key 比对，得到"模型间一致率"，作为标签质量的**第三方证据**。

## 口径必须说清楚（不能冒充人工盲测）

- 这叫 **model-as-blind-annotator**，不是 human blind test。
- 它证明的是"标签在另一个强模型看来也成立"，**不能**证明"标签符合人类专家判断"。
- 报告里一律标注为"模型盲判一致率"，绝不写成"人工盲测通过"。

## 关键纪律

1. 判定时**只给样本文本与 19 类菜单，不给 key**（否则毫无意义）。
2. 每条的原始输出都落盘，可逐条复核。
3. 与 key 不一致的条目**单独列出**，这些正是需要人工优先复核的候选。

用法:
    <venv python> benchmarks/intent/blind_model_annotate.py --model trae/deepseek-v4.1-flash
产物: benchmarks/intent/results/blind_model_<model>.json
"""
import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
from core.intent_recognizer import IntentCategory

LABELS = [c.value for c in IntentCategory]
GATEWAY = "http://127.0.0.1:8326/v1"
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
KEY = _judge_key()
# 与 make_blind_test.py 一致的类目菜单（只给类目名，不给定义之外的提示）
SYSTEM = (
    "你是安全生产意图分类专家。给你一条用户消息，请判断它属于哪个意图类目。\n"
    "只输出 JSON，不要解释：{\"intent\":\"<类目名>\"}\n\n"
    "可选类目（19 个）：\n" + "\n".join(f"- {v}" for v in LABELS)
)


async def one(client, model, text, sem):
    async with sem:
        for attempt in range(3):
            try:
                r = await client.post(
                    f"{GATEWAY}/chat/completions",
                    headers={"Authorization": f"Bearer {KEY}"},
                    json={"model": model, "max_tokens": 96, "temperature": 0.0,
                          "messages": [{"role": "system", "content": SYSTEM},
                                       {"role": "user", "content": text}]})
                if r.status_code != 200:
                    if attempt == 2:
                        return {"pred": None, "error": f"HTTP{r.status_code}"}
                    await asyncio.sleep(2)
                    continue
                d = r.json()
                raw = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                raw = raw.strip()
                if not raw:
                    if attempt < 2:
                        await asyncio.sleep(1)
                        continue
                    return {"pred": None, "error": "空输出", "raw": ""}
                t = raw
                if t.startswith("```"):
                    t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
                pred = None
                try:
                    v = (json.loads(t) or {}).get("intent")
                    if isinstance(v, str):
                        v = v.strip().strip('"').lower()
                        if v in LABELS:
                            pred = v
                except Exception:
                    pass
                if pred is None:
                    for v in LABELS:
                        if v in raw:
                            pred = v
                            break
                return {"pred": pred, "raw": raw[:120],
                        "error": None if pred else "无法解析"}
            except Exception as e:
                if attempt == 2:
                    return {"pred": None, "error": f"{type(e).__name__}"}
                await asyncio.sleep(2)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="trae/deepseek-v4.1-flash")
    ap.add_argument("--concurrency", type=int, default=5)
    args = ap.parse_args()

    sample = json.load(open(os.path.join(HERE, "blind_sample.json"), encoding="utf-8"))
    keymap = {r["text"]: r["label"]
              for r in json.load(open(os.path.join(HERE, "blind_answer_key.json"), encoding="utf-8"))}

    # 纪律：判定时只用 text，绝不把 key 传进 prompt
    texts = [s["text"] for s in sample]
    print(f"模型盲判 {len(texts)} 条（模型={args.model}；prompt 中不含答案 key）\n", flush=True)

    import httpx
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await asyncio.gather(*[one(client, args.model, t, sem) for t in texts])

    rows = []
    agree = 0
    n = 0
    by_tier = {}
    conf = Counter()
    for s, r in zip(sample, res):
        truth = keymap.get(s["text"])
        pred = r.get("pred")
        tier = s.get("tier", "?")
        st = by_tier.setdefault(tier, {"ok": 0, "n": 0})
        if pred is None:
            rows.append({"text": s["text"], "tier": tier, "key": truth,
                         "pred": None, "error": r.get("error"), "raw": r.get("raw")})
            continue
        n += 1
        st["n"] += 1
        ok = (pred == truth)
        if ok:
            agree += 1
            st["ok"] += 1
        else:
            conf[(truth, pred)] += 1
        rows.append({"text": s["text"], "tier": tier, "key": truth, "pred": pred,
                     "agree": ok, "raw": r.get("raw")})

    rate = agree / n if n else 0
    print(f"模型盲判一致率 {agree}/{n} = {rate:.4f}")
    for t, v in sorted(by_tier.items()):
        if v["n"]:
            print(f"  {t:12s} {v['ok']}/{v['n']} = {v['ok']/v['n']:.3f}")
    print("\n不一致条目（需人工优先复核）：")
    for r in rows:
        if r.get("agree") is False:
            print(f"  key={r['key']:20s} pred={r['pred']:20s} | {r['text'][:44]}")
    print("\n主要分歧（key → 盲判）:")
    for (k, p), c in conf.most_common(8):
        print(f"  {k} → {p}: {c}")

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": args.model,
        "method": "model-as-blind-annotator（不是人工盲测，口径见 docstring）",
        "n_sample": len(sample), "n_answered": n,
        "agreement": round(rate, 4),
        "by_tier": {t: {**v, "rate": round(v["ok"] / v["n"], 4) if v["n"] else None}
                    for t, v in by_tier.items()},
        "disagreements": [r for r in rows if r.get("agree") is False],
        "rows": rows,
    }
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    p = os.path.join(HERE, "results",
                     f"blind_model_{args.model.replace('/', '-')}.json")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nwritten {p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
