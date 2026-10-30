# -*- coding: utf-8 -*-
# 事实型问题跳过澄清的 A/B 真实评测（2026-10-29 闭环第 52 轮）。
#
# ## 背景
#
# 事实稳定性评测里，"噪声作业的职业接触限值是多少？依据哪个标准？" 5 次全部返回
# A/B 澄清提示（"A 法规查询 / B 职业健康"），一次数值都没给。用户要的是数字，
# 反问只是让他多点一次。
#
# 根因：`.env` 里 SAFETYMIND_BGE_TAU=0.40，澄清带 [0.40, 0.55) 命中；该机制原本用于
# "意图可能错、先确认再答"，但对**明显的数值/条款问题**并不合适。
#
# ## 方法
#
# - 6 条事实型问题 × 3 次，两臂：澄清照旧（默认）vs 事实型跳过澄清
# - 指标：澄清率、事实覆盖（union/always/coverage）、裁判四维分
#
# ## 决策规则（先定）
#
# 采纳"跳过"需同时满足：
#   1. 澄清率下降 ≥ 50%
#   2. 事实 coverage 不降（不低于基线 −0.05）
#   3. 配对 accuracy 不低于 −0.2
#
# 用法: <venv python> benchmarks/eval_clarify_factual_ab.py --runs 3
# 产物: benchmarks/results/clarify_factual_ab_<ts>.json
import argparse
import importlib
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

from eval_fact_stability import QS  # noqa: E402
from eval_stream_parity import extract_facts  # noqa: E402

GATEWAY = os.getenv("JUDGE_GATEWAY", "http://127.0.0.1:8326/v1")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _creds import judge_key  # noqa: E402

GKEY = judge_key()
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")
SYS = ("你是安全生产领域的答案评审员。按四维各打 1-5 分：accuracy=事实/数值/条款准确性；"
       "evidence=可核对依据；concrete=可执行程度；safety=不确定时是否提示核实。\n"
       "只输出 JSON：{\"accuracy\":n,\"evidence\":n,\"concrete\":n,\"safety\":n}")
DIMS = ("accuracy", "evidence", "concrete", "safety")
CLARIFY_MARK = "您的问题可能涉及两类场景"


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


def run_arm(skip, runs):
    os.environ["SAFETYMIND_CLARIFY_SKIP_FACTUAL"] = "1" if skip else "0"
    os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
    # 控温：两臂差异要能归因给"是否跳过澄清"，而不是生成随机性（教训见 R37）
    os.environ["SAFETYMIND_TEMPERATURE"] = "0"
    import api.main as m
    importlib.reload(m)
    from fastapi.testclient import TestClient
    rows = []
    with TestClient(m.app) as c:
        c.get("/health")
        for tag, q in QS:
            for k in range(runs):
                r = c.post("/chat", json={"message": q, "user_id": f"clr-{int(skip)}-{tag}-{k}"})
                b = r.json() if r.status_code == 200 else {}
                ans = b.get("response") or ""
                rows.append({"arm": "skip" if skip else "base", "tag": tag, "k": k, "q": q,
                             "ans": ans, "clarified": CLARIFY_MARK in ans,
                             "facts": sorted(extract_facts(ans)),
                             "ms": round(b.get("total_ms") or 0, 0)})
                print(f"[{rows[-1]['arm']:4s} {tag:10s} #{k}] {len(ans):5d}字 "
                      f"clarify={rows[-1]['clarified']} facts={len(rows[-1]['facts'])}", flush=True)
    return rows


def summarize(rows, arm):
    grp = [r for r in rows if r["arm"] == arm]
    per_q = {}
    for tag in {r["tag"] for r in grp}:
        rs = [r for r in grp if r["tag"] == tag]
        union = set().union(*[set(r["facts"]) for r in rs]) if rs else set()
        cov = [len(set(r["facts"])) / len(union) if union else 0.0 for r in rs]
        per_q[tag] = {"coverage": round(statistics.mean(cov), 3),
                      "clarify_rate": round(sum(1 for r in rs if r["clarified"]) / len(rs), 2)}
    return {"arm": arm,
            "clarify_rate": round(sum(1 for r in grp if r["clarified"]) / len(grp), 3),
            "coverage_mean": round(statistics.mean([v["coverage"] for v in per_q.values()]), 3),
            "per_q": per_q}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    rows = []
    rows += run_arm(False, args.runs)
    rows += run_arm(True, args.runs)

    with httpx.Client(timeout=300) as jc:
        for r in rows:
            if r["ans"] and not r["clarified"]:
                sc = judge(jc, r["q"], r["ans"])
                if sc:
                    r["score"] = {d: float(sc.get(d) or 0) for d in DIMS}

    print("\n=== 澄清 vs 跳过（事实型问题）===")
    base = summarize(rows, "base")
    skip = summarize(rows, "skip")
    for s in (base, skip):
        print(f"  {s['arm']:4s} 澄清率={s['clarify_rate']:.0%} "
              f"事实coverage={s['coverage_mean']:.2f}")

    pairs = []
    for tag, _ in QS:
        for k in range(args.runs):
            a = next((r for r in rows if r["arm"] == "skip" and r["tag"] == tag
                      and r["k"] == k and r.get("score")), None)
            b = next((r for r in rows if r["arm"] == "base" and r["tag"] == tag
                      and r["k"] == k and r.get("score")), None)
            if a and b:
                pairs.append((a, b))

    verdict = "insufficient"
    if pairs:
        diff = {d: round(statistics.mean([a["score"][d] - b["score"][d] for a, b in pairs]), 3)
                for d in DIMS}
        print(f"  配对 {len(pairs)} 组：四维差(skip-base) {diff}")
        ok = (skip["clarify_rate"] <= base["clarify_rate"] * 0.5
              and skip["coverage_mean"] >= base["coverage_mean"] - 0.05
              and diff["accuracy"] >= -0.2)
        verdict = "adopt_skip" if ok else "keep_base"
        print(f"  决策：{verdict}")
    else:
        print("  可配对样本不足（基线侧多为澄清、无答案可评）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"clarify_factual_ab_{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps({"verdict": verdict, "base": base, "skip": skip,
                             "rows": [{k: v for k, v in r.items() if k != "ans"} for r in rows]},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {p}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
