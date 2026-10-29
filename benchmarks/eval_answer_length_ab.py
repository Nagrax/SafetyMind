# -*- coding: utf-8 -*-
"""真实评测：答案长度约束的质量/延迟权衡（`SAFETYMIND_ANSWER_STYLE=brief`）。

## 为什么

延迟分账（`benchmarks/profile_stage_split.py`）显示非生成开销只占 8%，
瓶颈全在生成侧；实测生成耗时与输出长度近似线性（约 29ms/字）。
所以"答案更短"是唯一**不换模型**就能降延迟的杠杆——但它必须用质量定价，
不能凭感觉砍。

## 方法

- 12 条真实问题（fact / grading / writing / emergency 混合）
- 两口径：默认（不追加任何约束）vs `brief`（先结论 + 最多 5 条 + 450 字内）
- 同一进程内**交错**调用（按题号奇偶翻转顺序），压掉时段漂移
- 生成温度固定 0（`SAFETYMIND_TEMPERATURE=0`），关回复缓存
- 同一裁判双采样，四维 1-5 打分

## 决策规则（先定，再看数据）

采纳 brief 需**同时**满足：
  1. 字数中位下降 ≥ 30%
  2. 延迟 p50 下降 ≥ 15%
  3. 配对差 accuracy 与 safety 都不低于 −0.15
否则保持默认（并把"缩短无收益/有损"如实记录）。

用法: <venv python> benchmarks/eval_answer_length_ab.py --n 12
产物: benchmarks/results/answer_length_ab_<ts>.json
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

from eval_reasoning_ab import QUESTIONS  # noqa: E402

SYS = ("你是安全生产领域的答案评审员。给定问题与答案，按四个维度各打 1-5 分：\n"
       "accuracy=事实/数值/条款是否准确；evidence=是否给出可核对的依据或来源；\n"
       "concrete=是否给出可执行的具体步骤或数值；safety=不确定时是否提示核实而非编造。\n"
       "只输出 JSON：{\"accuracy\":n,\"evidence\":n,\"concrete\":n,\"safety\":n}")

DIMS = ("accuracy", "evidence", "concrete", "safety")


def judge(client, q, ans):
    payload = {"model": JUDGE_MODEL, "max_tokens": 600, "temperature": 0,
               "messages": [{"role": "system", "content": SYS},
                            {"role": "user", "content": f"问题：{q}\n\n答案：{ans[:4000]}"}]}
    for attempt in range(3):
        try:
            r = client.post(GATEWAY + "/chat/completions",
                            headers={"Authorization": "Bearer " + GKEY},
                            json=payload, timeout=300)
            if r.status_code >= 400:
                time.sleep(2)
                continue
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            m = re.search(r"\{[\s\S]*\}", txt)
            if not m:
                continue
            d = json.loads(m.group(0))
            return {k: float(d.get(k) or 0) for k in DIMS}
        except Exception:
            time.sleep(2)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--samples", type=int, default=2)
    args = ap.parse_args()

    from fastapi.testclient import TestClient
    import api.main as m

    pool = QUESTIONS[:24]
    picked, seen = [], {}
    for cat, sub, text in pool:
        if seen.get(sub, 0) >= 3:
            continue
        seen[sub] = seen.get(sub, 0) + 1
        picked.append((sub, text))
        if len(picked) >= args.n:
            break

    os.environ["SAFETYMIND_TEMPERATURE"] = "0"      # 生成侧可控（评测定价的前提）
    os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
    rows = []
    with TestClient(m.app) as c:
        c.get("/health")
        for i, (sub, q) in enumerate(picked):
            arms = ("brief", "default") if i % 2 == 0 else ("default", "brief")
            for arm in arms:
                if arm == "brief":
                    os.environ["SAFETYMIND_ANSWER_STYLE"] = "brief"
                else:
                    os.environ.pop("SAFETYMIND_ANSWER_STYLE", None)
                t = time.perf_counter()
                r = c.post("/chat", json={"message": q, "user_id": f"len-{arm}-{i}"})
                ms = (time.perf_counter() - t) * 1000
                ans = (r.json() or {}).get("response", "") if r.status_code == 200 else ""
                rows.append({"arm": arm, "i": i, "sub": sub, "q": q, "ans": ans,
                             "chars": len(ans), "ms": round(ms, 1)})
                print(f"[i={i+1} {arm:7s}] {sub:9s} {ms:6.0f}ms {len(ans):5d}字", flush=True)

    with httpx.Client(timeout=300) as jc:
        for r in rows:
            if not r["ans"]:
                continue
            got = [judge(jc, r["q"], r["ans"]) for _ in range(args.samples)]
            got = [g for g in got if g]
            if got:
                r["score"] = {d: round(statistics.mean([g[d] for g in got]), 3) for d in DIMS}
            print(f"  judge[{r['arm']:7s}] {r['sub']:9s} {r.get('score') or 'FAIL'}", flush=True)

    print("\n=== 长度约束 A/B（控温、交错）===")
    summary = {}
    for arm in ("brief", "default"):
        grp = [r for r in rows if r["arm"] == arm]
        summary[arm] = {
            "n": len(grp),
            "chars_p50": round(statistics.median([r["chars"] for r in grp]), 0),
            "ms_p50": round(statistics.median([r["ms"] for r in grp]), 0),
        }
        print(f"  {arm:7s}: 字数中位 {summary[arm]['chars_p50']:.0f}，"
              f"延迟 p50 {summary[arm]['ms_p50']:.0f}ms")

    pairs = []
    for i in range(len(picked)):
        b = next((r for r in rows if r["i"] == i and r["arm"] == "brief" and r.get("score")), None)
        d = next((r for r in rows if r["i"] == i and r["arm"] == "default" and r.get("score")), None)
        if b and d:
            pairs.append((b, d))
    verdict = "insufficient"
    if pairs and summary["default"]["chars_p50"] > 0:
        diff = {k: round(statistics.mean([a["score"][k] - b["score"][k] for a, b in pairs]), 3)
                for k in DIMS}
        chars_drop = 1 - summary["brief"]["chars_p50"] / summary["default"]["chars_p50"]
        ms_drop = 1 - summary["brief"]["ms_p50"] / summary["default"]["ms_p50"]
        print(f"  配对 {len(pairs)} 题：四维差(brief-default) {diff}")
        print(f"  字数下降 {chars_drop:.0%}，延迟下降 {ms_drop:.0%}")
        ok = (chars_drop >= 0.30 and ms_drop >= 0.15
              and diff["accuracy"] >= -0.15 and diff["safety"] >= -0.15)
        verdict = "adopt_brief" if ok else "keep_default"
        print(f"  决策：{verdict}（规则：字数↓≥30% 且 延迟↓≥15% 且 accuracy/safety 不低于 −0.15）")
    else:
        print("  样本不足，无法判定")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"answer_length_ab_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"verdict": verdict, "summary": summary, "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {out}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
