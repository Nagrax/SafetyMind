# -*- coding: utf-8 -*-
"""真实评测：候选模型横评（质量 + 延迟 + token），据此决定默认模型/按意图路由。

## 为什么

`ANTHROPIC_MODEL` 默认 `raccoon/sn-deepseek-v4-1-flash-1017` 是当初**依据"能跑通 +
响应快"**选定的，从未在同一套真实安全问题集上与其它模型横评过。模型选择影响
每一条回答的质量与延迟，必须用真实评测决定。

## 方法

1. 同一 24 题（复用 eval_reasoning_ab.QUESTIONS，覆盖全部意图类别）
2. 每个候选模型各生成 1 次（temp=0.7，避免中继缓存；统一 reasoning_effort=none）
3. 单裁判（trae/deepseek-v4.1-flash）四维打分——用户已指出多裁判是过度设计，
   同一裁判才能跨模型可比
4. 输出：总分、分意图子集得分、延迟中位、token 中位、空回答数
5. 决策规则（事先声明）：
   - 某模型整体分 ≥ 当前模型 +0.05 且延迟不超 1.5× → 建议切换
   - 某意图子集上另一模型显著更好（≥ +0.08）→ 建议该意图单独路由
   - 否则维持现状（并记录"已测但无更优"）

用法:
    <venv python> benchmarks/eval_model_compare.py --concurrency 10
产物: benchmarks/results/model_compare_<ts>.json
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "benchmarks" / "results"
GATEWAY = os.getenv("JUDGE_GATEWAY", "http://127.0.0.1:8326/v1")
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
GKEY = _judge_key()
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "trae/deepseek-v4.1-flash")

CANDIDATES = [m for m in os.getenv("CANDIDATE_MODELS", ",".join([
    "raccoon/sn-deepseek-v4-1-flash-1017",   # 当前默认
    "trae/deepseek-v4.1-flash",
    "trae/glm-5.3-flash",
    "trae/kimi-k2.6",
    "trae/qwen-3.6-plus",
])).split(",") if m]
CURRENT = CANDIDATES[0]

from eval_reasoning_ab import QUESTIONS, JUDGE_SYS  # noqa: E402


async def post(client, payload, timeout=400):
    for attempt in range(4):
        try:
            r = await client.post(GATEWAY + "/chat/completions",
                                  headers={"Authorization": "Bearer " + GKEY},
                                  json=payload, timeout=timeout)
            if r.status_code == 429:
                await asyncio.sleep(min(30, 2 * (attempt + 1) ** 2))
                continue
            if r.status_code >= 400:
                if attempt == 3:
                    return {"error": f"HTTP{r.status_code}"}
                await asyncio.sleep(2)
                continue
            return r.json()
        except Exception as ex:
            if attempt == 3:
                return {"error": type(ex).__name__}
            await asyncio.sleep(2)
    return {"error": "retries"}


def extract(data):
    if data.get("error"):
        return None, data["error"], 0
    ch = (data.get("choices") or [{}])[0] or {}
    msg = ch.get("message") or {}
    text = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
    return text, None, ((data.get("usage") or {}).get("completion_tokens") or 0)


def parse_json(txt, keys):
    if not txt:
        return None
    m = re.search(r"\{[\s\S]*?\}", txt)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    return d if all(k in d for k in keys) else None


async def one(client, sem, model, q):
    async with sem:
        t = time.perf_counter()
        data = await post(client, {
            "model": model, "max_tokens": 3072, "temperature": 0.7,
            "reasoning_effort": "none",
            "messages": [{"role": "user", "content": q}]})
        ms = (time.perf_counter() - t) * 1000
    text, err, tok = extract(data)
    return {"model": model, "ms": round(ms, 1), "text": text, "error": err, "tok": tok}


async def judge(client, sem, q, text):
    async with sem:
        data = await post(client, {
            "model": JUDGE_MODEL, "max_tokens": 1024, "temperature": 0,
            "messages": [{"role": "system", "content": JUDGE_SYS},
                         {"role": "user", "content": f"问题：{q}\n\n回答：\n{(text or '')[:4000]}"}]})
    txt, _, _ = extract(data)
    d = parse_json(txt, ["relevance", "accuracy", "completeness", "safety"])
    if not d:
        return None
    return round(sum(float(d[k]) for k in
                     ("relevance", "accuracy", "completeness", "safety")) / 4 / 5, 4)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=10)
    args = ap.parse_args()
    sem = asyncio.Semaphore(args.concurrency)

    print(f"模型横评：{len(CANDIDATES)} 个候选 × {len(QUESTIONS)} 题，裁判={JUDGE_MODEL}",
          flush=True)
    rows = []
    async with httpx.AsyncClient(timeout=500) as client:
        async def one_q(i, cat, sub, q):
            gens = await asyncio.gather(*[one(client, sem, m, q) for m in CANDIDATES])
            scores = await asyncio.gather(*[
                judge(client, sem, q, g["text"] or "") for g in gens])
            row = {"i": i, "cat": cat, "sub": sub, "q": q,
                   "gen": {g["model"]: {"ms": g["ms"], "tok": g["tok"],
                                        "error": g["error"],
                                        "len": len(g["text"] or "")} for g in gens},
                   "score": {g["model"]: s for g, s in zip(gens, scores)}}
            rows.append(row)
            brief = " ".join(f"{m.split('/')[-1][:12]}={s}"
                             for m, s in row["score"].items())
            print(f"[{len(rows)}/{len(QUESTIONS)}] {sub:9s} {brief}", flush=True)

        await asyncio.gather(*[one_q(i, c, s, q)
                               for i, (c, s, q) in enumerate(QUESTIONS)])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"model_compare_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"judge": JUDGE_MODEL, "candidates": CANDIDATES,
                               "rows": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    def agg(model, sub=None):
        sel = [r for r in rows if sub is None or r["sub"] == sub]
        sc = [r["score"].get(model) for r in sel if r["score"].get(model) is not None]
        ms = [r["gen"][model]["ms"] for r in sel if not r["gen"][model]["error"]]
        tk = [r["gen"][model]["tok"] for r in sel if not r["gen"][model]["error"]]
        em = sum(1 for r in sel if not r["gen"][model]["len"])
        return {"n": len(sel),
                "score": round(statistics.mean(sc), 4) if sc else None,
                "ms": round(statistics.median(ms), 1) if ms else None,
                "tok": round(statistics.median(tk), 1) if tk else None,
                "empty": em}

    print("\n=== 整体 ===")
    print(f"  {'模型':34s} {'分':>6s} {'延迟ms':>8s} {'tok':>6s} {'空':>3s}")
    for m in CANDIDATES:
        a = agg(m)
        print(f"  {m:34s} {str(a['score']):>6s} {str(a['ms']):>8s} "
              f"{str(a['tok']):>6s} {a['empty']:>3d}")

    print("\n=== 按子集（找某类问题该用哪个模型）===")
    subs = sorted({r["sub"] for r in rows})
    for s in subs:
        line = []
        for m in CANDIDATES:
            a = agg(m, s)
            line.append(f"{m.split('/')[-1][:14]}={a['score']}")
        print(f"  [{s:9s}] " + "  ".join(line))

    cur = agg(CURRENT)
    print("\n=== 决策（阈值：整体 ≥ +0.05 且延迟 ≤ 1.5× 才建议切换）===")
    best = None
    for m in CANDIDATES[1:]:
        a = agg(m)
        if a["score"] is None or cur["score"] is None:
            continue
        d = a["score"] - cur["score"]
        ratio = (a["ms"] / cur["ms"]) if (a["ms"] and cur["ms"]) else None
        verdict = "候选更优" if (d >= 0.05 and (ratio or 9) <= 1.5) else "不切换"
        print(f"  {m:34s} Δ分={d:+.4f} 延迟比={ratio:.2f}x → {verdict}")
        if d >= 0.05 and (ratio or 9) <= 1.5 and (best is None or a["score"] > best[1]):
            best = (m, a["score"])
    if best:
        print(f"\n  建议默认模型改为 {best[0]}（分 {best[1]} vs 当前 {cur['score']}）")
    else:
        print(f"\n  => 维持当前默认 {CURRENT}（已测 {len(CANDIDATES)} 个候选，无显著更优）")
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
