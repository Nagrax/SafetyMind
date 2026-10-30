# -*- coding: utf-8 -*-
"""真实评测：同一问题重复问，关键事实（数值/标准号）的稳定性。

## 为什么

上一轮做端口一致性时发现：同一条链路两次生成，数值覆盖可以差很多
（"安全阀校验周期"一次给了 3年/5年/TSG11/TSG21，另一次一个数值都没给）。
对安全员来说，"同一个问题问两遍得到不同口径"比"答得慢"更伤信任。

## 方法

- 6 条**期望给出具体数值/标准号**的问题
- 每条跑 5 次（独立 user_id，关回复缓存，**不设温度**——测的就是生产默认行为）
- 用与端口一致性同一套抽取口径（数值+单位、标准号）抽事实
- 逐题统计：
  - `union`：5 次出现过的全部事实
  - `coverage`：每次覆盖 union 的比例（均值）
  - `always`：5 次都出现的事实（"稳定口径"）
  - `flaky`：时有时无的事实（"每次可能不同的口径"）

## 决策规则（先定）

- 逐题 coverage 均值 ≥ 0.7 且无 flaky 关键数值 → 稳定性合格
- coverage < 0.5 → 判为"事实不稳定"，记录具体 flaky 项，并提出缓解方向
  （如：事实类问题降温度 / 答案附数值来源 / 低置信时提示"以现行标准为准"）

用法: <venv python> benchmarks/eval_fact_stability.py [--runs 5]
产物: benchmarks/results/fact_stability_<ts>.json
"""
import argparse
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

from eval_stream_parity import extract_facts  # noqa: E402

QS = [
    ("动火票等级与有效期", "动火作业票分几个等级？各级有效期多久？依据哪个标准？"),
    ("受限空间气体检测", "受限空间作业的气体检测合格判定值分别是多少？"),
    ("安全阀校验周期", "安全阀的校验周期是怎么规定的？超期有什么后果？"),
    ("噪声职业接触限值", "噪声作业的职业接触限值是多少？依据哪个标准？"),
    ("甲醇闪点", "甲醇的闪点和爆炸极限分别是多少？"),
    ("高处作业分级", "高处作业分几级？各级的高度界限是多少？"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    from fastapi.testclient import TestClient
    import api.main as m

    os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
    rows = []
    with TestClient(m.app) as c:
        c.get("/health")
        for tag, q in QS:
            facts_runs, lens = [], []
            for k in range(args.runs):
                r = c.post("/chat", json={"message": q, "user_id": f"stab-{tag}-{k}"})
                ans = (r.json() or {}).get("response", "") if r.status_code == 200 else ""
                facts_runs.append(extract_facts(ans))
                lens.append(len(ans))
                time.sleep(0.2)
            union = set().union(*facts_runs) if facts_runs else set()
            always = set.intersection(*facts_runs) if facts_runs else set()
            flaky = union - always
            cov = [len(f) / len(union) for f in facts_runs] if union else [0.0]
            row = {
                "tag": tag, "q": q, "runs": args.runs,
                "union": sorted(union), "always": sorted(always), "flaky": sorted(flaky),
                "coverage_mean": round(statistics.mean(cov), 3),
                "union_size": len(union), "always_size": len(always),
                "flaky_size": len(flaky),
                "len_mean": round(statistics.mean(lens), 0),
            }
            rows.append(row)
            print(f"[{tag:10s}] union={len(union):2d} always={len(always):2d} "
                  f"flaky={len(flaky):2d} coverage={row['coverage_mean']:.2f} "
                  f"字数中位~{row['len_mean']:.0f}", flush=True)
            if flaky:
                print(f"    时有时无：{sorted(flaky)[:8]}", flush=True)

    print("\n=== 事实稳定性汇总 ===")
    ok = [r for r in rows if r["coverage_mean"] >= 0.7]
    bad = [r for r in rows if r["coverage_mean"] < 0.5]
    print(f"  题数 {len(rows)}；coverage≥0.7 的 {len(ok)} 题；<0.5 的 {len(bad)} 题")
    for r in rows:
        print(f"  {r['tag']:12s} coverage={r['coverage_mean']:.2f} "
              f"always={r['always_size']}/{r['union_size']} flaky={r['flaky_size']}")
    verdict = "stable" if not bad else ("unstable" if len(bad) >= 3 else "mixed")
    print(f"  决策：{verdict}（<0.5 记 3 题以上判 unstable）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"fact_stability_{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps({"verdict": verdict, "rows": rows}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"\nwritten {p}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
