# -*- coding: utf-8 -*-
"""真实评测：输出预算（max_tokens）与"答案被截断"的关系。

## 为什么

A/B 产物里 20 条回答有 3 条以"（注意：本次回答因达到长度上限被截断，未完整生成。）"
结尾（15%），全部落在判定/分级类问题；被截断的正文里还残留模型自己的作答提纲。
上一轮曾验证过"1024→4096 对**思考型**截断无效"，但那是在 thinking=on 下；当前默认
thinking=off，1024 的预算是否够用从未测过。

## 方法

- 取 3 条已知会截断的判定/分级问题 + 1 条对照
- 三档预算：1024（现状）/ 2048 / 4096（`SAFETYMIND_MAX_TOKENS`，应用启动时读取）
- 逐条记录：是否截断、字数、耗时

## 决策规则（先定规则再看数据）

- 2048 能把截断率降到 0 且耗时增幅 ≤ 20% → 采纳 2048 为默认
- 2048 仍截断 → 说明是模型在长答案上的固有行为，改为"截断后二次续写"方案（另起一轮）
- 1024 本身不截断（与本轮样本矛盾）→ 记录样本差异，保持默认

用法: <venv python> benchmarks/eval_truncation_budget.py
产物: benchmarks/results/truncation_budget_<ts>.json
"""
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path("D:/claudecode/SafetyMind")
sys.path.insert(0, str(REPO))
OUT_DIR = REPO / "benchmarks" / "results"

QUESTIONS = [
    "安全阀超期未校验，应判定为一般事故隐患还是重大事故隐患？请给出判定依据与理由。",
    "某企业液氨储罐区围堰排水阀常开，是否构成重大隐患？请说明判定标准与处置要求。",
    "配电室未设置挡鼠板且电缆沟未封堵，按什么标准判定隐患等级？",
    "动火作业票的有效期是怎么规定的？",   # 对照：短答案，不该截断
]
TRUNC_MARK = "被截断"


def run_arm(budget):
    os.environ["SAFETYMIND_MAX_TOKENS"] = str(budget)
    os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
    # 模块级单例在 import 时就已确定预算，故每档预算必须重新导入应用
    for m in [m for m in list(sys.modules) if m == "api.main" or m.startswith("api.")]:
        del sys.modules[m]
    from fastapi.testclient import TestClient
    import api.main as m
    rows = []
    with TestClient(m.app) as c:
        c.get("/health")
        for i, q in enumerate(QUESTIONS):
            t = time.perf_counter()
            r = c.post("/chat", json={"message": q, "user_id": f"tb-{budget}-{i}"})
            ms = (time.perf_counter() - t) * 1000
            ans = (r.json() or {}).get("response", "") if r.status_code == 200 else ""
            rows.append({"budget": budget, "i": i, "q": q, "ms": round(ms, 1),
                         "chars": len(ans), "truncated": TRUNC_MARK in ans,
                         "code": r.status_code})
            print(f"[budget={budget}] {i+1}/{len(QUESTIONS)} {ms:.0f}ms "
                  f"{len(ans)}字 截断={TRUNC_MARK in ans}", flush=True)
    return rows


def main():
    rows = []
    for budget in (1024, 2048, 4096):
        rows.extend(run_arm(budget))

    print("\n=== 截断率 / 耗时 ===")
    summary = {}
    for budget in (1024, 2048, 4096):
        grp = [r for r in rows if r["budget"] == budget]
        tr = sum(1 for r in grp if r["truncated"])
        summary[budget] = {"n": len(grp), "truncated": tr,
                           "ms_p50": round(statistics.median([r["ms"] for r in grp]), 0),
                           "chars_p50": round(statistics.median([r["chars"] for r in grp]), 0)}
        print(f"  {budget}: 截断 {tr}/{len(grp)}，耗时中位 {summary[budget]['ms_p50']:.0f}ms，"
              f"字数中位 {summary[budget]['chars_p50']}")

    base = summary[1024]
    cand = summary[2048]
    if base["truncated"] > 0 and cand["truncated"] == 0 and cand["ms_p50"] <= base["ms_p50"] * 1.2:
        verdict = "adopt_2048"
    elif base["truncated"] > 0 and cand["truncated"] > 0:
        verdict = "need_continue_round"
    else:
        verdict = "keep_1024"
    print(f"  决策：{verdict}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"truncation_budget_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"verdict": verdict, "summary": summary, "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {out}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
