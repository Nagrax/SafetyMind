# -*- coding: utf-8 -*-
"""真实评测：是否把默认模型换成候选（三轮交替、/chat 全链路口径）。

## 为什么不用裸 API 横评

裸 API 只能说明"模型本身快慢"，说明不了"接到 /chat 全链路后是否更快、更好"：
意图识别、RAG、工具回路都会随模型变化（工具调用格式、回答长度）而变化。

## 方法

- 同一组 8 条真实问题，三轮
- 每轮交替两臂：现役（.env 的 ANTHROPIC_MODEL）vs 候选 `aggregate/qwen3-8-flash`
- 每臂单独起停应用（模型在客户端构造时固定），全链路 POST /chat
- 控温 `SAFETYMIND_TEMPERATURE=0`、关回复缓存
- 同一裁判（关推理）单采样打四维分

## 决策规则（先定再看数据）

采纳候选需**同时**满足：
  1. 三轮合并后，延迟中位比 ≤ 0.80（即快 ≥20%）
  2. 配对 accuracy 差不低于 −0.3，safety 差不低于 −0.3
  3. 空答/报错数不高于现役
否则保持现役。

用法: <venv python> benchmarks/eval_model_swap_ab.py --rounds 3 --n 8
产物: benchmarks/results/model_swap_ab_<ts>.json
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

REPO = Path("D:/claudecode/SafetyMind")
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
CANDIDATE = os.getenv("MODEL_SWAP_CANDIDATE", "aggregate/qwen3-8-flash")

from eval_reasoning_ab import QUESTIONS  # noqa: E402

SYS = ("你是安全生产领域的答案评审员。按四维各打 1-5 分：accuracy=事实/数值/条款准确性；"
       "evidence=可核对依据；concrete=可执行程度；safety=不确定时是否提示核实。\n"
       "只输出 JSON：{\"accuracy\":n,\"evidence\":n,\"concrete\":n,\"safety\":n}")

DIMS = ("accuracy", "evidence", "concrete", "safety")


def judge(client, q, ans):
    for _ in range(3):
        try:
            r = client.post(GATEWAY + "/chat/completions",
                            headers={"Authorization": "Bearer " + GKEY},
                            json={"model": JUDGE_MODEL, "max_tokens": 800, "temperature": 0,
                                  "reasoning_effort": "none",
                                  "messages": [{"role": "system", "content": SYS},
                                               {"role": "user",
                                                "content": f"问题：{q}\n\n答案：{ans[:4000]}"}]},
                            timeout=300)
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            m = re.search(r"\{[\s\S]*\}", txt)
            if m:
                return json.loads(m.group(0))
        except Exception:
            time.sleep(2)
    return None


def run_arm(model, picked, round_idx):
    os.environ["ANTHROPIC_MODEL"] = model
    os.environ["SAFETYMIND_TEMPERATURE"] = "0"
    os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
    from fastapi.testclient import TestClient
    import importlib
    import api.main as m
    importlib.reload(m)   # 让模块级客户端按新模型重建
    rows = []
    with TestClient(m.app) as c:
        c.get("/health")
        for i, (sub, q) in enumerate(picked):
            t = time.perf_counter()
            r = c.post("/chat", json={"message": q, "user_id": f"swap-{model[:12]}-{round_idx}-{i}"})
            ms = (time.perf_counter() - t) * 1000
            ans = (r.json() or {}).get("response", "") if r.status_code == 200 else ""
            rows.append({"round": round_idx, "model": model, "i": i, "sub": sub, "q": q,
                         "ans": ans, "ms": round(ms, 1), "code": r.status_code})
            print(f"[r{round_idx} {model[:22]:22s}] {sub:9s} {ms:6.0f}ms {len(ans):5d}字",
                  flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--n", type=int, default=8)
    args = ap.parse_args()

    current = os.getenv("ANTHROPIC_MODEL", "")
    if not current:
        for line in open(REPO / ".env", encoding="utf-8", errors="ignore"):
            if line.startswith("ANTHROPIC_MODEL="):
                current = line.split("=", 1)[1].strip()
    print(f"现役={current} 候选={CANDIDATE}", flush=True)

    picked = [(sub, q) for cat, sub, q in QUESTIONS[:args.n]]
    rows = []
    for rnd in range(1, args.rounds + 1):
        order = [current, CANDIDATE] if rnd % 2 == 1 else [CANDIDATE, current]
        for model in order:
            rows.extend(run_arm(model, picked, rnd))

    with httpx.Client(timeout=300) as jc:
        for r in rows:
            if r["ans"]:
                sc = judge(jc, r["q"], r["ans"])
                if sc:
                    r["score"] = {k: float(sc.get(k) or 0) for k in DIMS}
                print(f"  judge[r{r['round']} {r['model'][:18]:18s}] {r.get('score') or 'FAIL'}",
                      flush=True)

    print("\n=== 换模型 A/B（/chat 全链路、三轮、控温）===")
    summary = {}
    for model in (current, CANDIDATE):
        grp = [r for r in rows if r["model"] == model]
        ok = [r for r in grp if r["ans"]]
        summary[model] = {
            "n": len(grp), "answered": len(ok),
            "ms_p50": round(statistics.median([r["ms"] for r in grp]), 0) if grp else 0,
            "chars_p50": round(statistics.median([len(r["ans"]) for r in grp]), 0) if grp else 0,
        }
        sc = [r["score"] for r in grp if r.get("score")]
        if sc:
            summary[model]["score_mean"] = {
                d: round(statistics.mean([s[d] for s in sc]), 3) for d in DIMS}
        print(f"  {model[:34]:34s} {summary[model]}")

    pairs = []
    for rnd in range(1, args.rounds + 1):
        for i in range(len(picked)):
            a = next((r for r in rows if r["round"] == rnd and r["i"] == i
                      and r["model"] == CANDIDATE and r.get("score")), None)
            b = next((r for r in rows if r["round"] == rnd and r["i"] == i
                      and r["model"] == current and r.get("score")), None)
            if a and b:
                pairs.append((a, b))

    verdict = "insufficient"
    if pairs:
        diff = {d: round(statistics.mean([a["score"][d] - b["score"][d] for a, b in pairs]), 3)
                for d in DIMS}
        ratio = statistics.median([a["ms"] / b["ms"] for a, b in pairs if b["ms"] > 0])
        print(f"  配对 {len(pairs)} 组：四维差(候选-现役) {diff}；延迟比中位 {ratio:.2f}")
        ok = (ratio <= 0.80 and diff["accuracy"] >= -0.3 and diff["safety"] >= -0.3
              and summary[CANDIDATE]["answered"] >= summary[current]["answered"])
        verdict = "switch" if ok else "keep"
        print(f"  决策：{verdict}（规则：延迟比≤0.80 且 accuracy/safety 不低于 −0.3）")
    else:
        print("  样本不足")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"model_swap_ab_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"verdict": verdict, "current": current,
                               "candidate": CANDIDATE,
                               "summary": summary, "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {out}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
