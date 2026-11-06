# -*- coding: utf-8 -*-
"""真实评测：/chat 的**答复失败率**与失败原因分类。

## 为什么

多轮评测里反复看到"答案不是有效回答"：工具预算耗尽文案、"我再查一下"这类过程话术、
"抱歉，处理您的请求时出现问题"。它们散落在各次 A/B 里，从没被当成一个指标测过。
对安全员来说，"问了没答"比"答得慢"严重得多。

## 方法

24 条真实问题走生产 `/chat`（默认温度、关回复缓存），按文案规则分类：

| 类别 | 判据 |
|---|---|
| ok | 非空、≥40 字、不含任何失败标记 |
| budget_exhausted | 含"工具调用未在预算内完成"或"未能完成最终整理" |
| interim_text | <40 字且像过程话术（我再/我来/稍等/查一下…） |
| agent_error | 含"处理您的请求时出现问题" |
| clarify | 含"您的问题可能涉及两类场景" |
| truncated | 含"被截断" |
| empty | <10 字 |

## 决策规则

- 失败率 ≥ 10% → 按**最高频类别**立项修复，修完用同脚本复测
- < 5% → 记为可接受，不再投入

用法: <venv python> benchmarks/eval_answer_failure_rate.py [--n 24]
产物: benchmarks/results/answer_failure_rate_<ts>.json
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

from eval_reasoning_ab import QUESTIONS  # noqa: E402

MARKERS = {
    "budget_exhausted": ("工具调用未在预算内完成", "未能完成最终整理"),
    "agent_error": ("处理您的请求时出现问题",),
    "clarify": ("您的问题可能涉及两类场景",),
    "truncated": ("被截断",),
}
INTERIM_HINTS = ("我再", "我来", "稍等", "查一下", "补充检索", "做一次", "确认一下")


def classify(ans: str) -> str:
    a = (ans or "").strip()
    for cat, keys in MARKERS.items():
        if any(k in a for k in keys):
            return cat
    if len(a) < 10:
        return "empty"
    if len(a) < 40 and any(h in a for h in INTERIM_HINTS):
        return "interim_text"
    if len(a) < 40:
        return "short"
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    args = ap.parse_args()

    from fastapi.testclient import TestClient
    import api.main as m

    os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
    qs = [(sub, q) for _, sub, q in QUESTIONS[:args.n]]
    rows = []
    with TestClient(m.app) as c:
        c.get("/health")
        for i, (sub, q) in enumerate(qs):
            t = time.perf_counter()
            r = c.post("/chat", json={"message": q, "user_id": f"fail-{i}"})
            ms = (time.perf_counter() - t) * 1000
            b = r.json() if r.status_code == 200 else {}
            ans = b.get("response") or ""
            cat = classify(ans) if r.status_code == 200 else "http_error"
            rows.append({"i": i, "sub": sub, "q": q, "code": r.status_code,
                         "cat": cat, "len": len(ans), "ms": round(ms, 0),
                         "head": ans[:40]})
            print(f"[{i+1}/{len(qs)}] {sub:9s} {cat:16s} {len(ans):5d}字 {ms:6.0f}ms "
                  f"{ans[:22]!r}", flush=True)

    ok = [r for r in rows if r["cat"] == "ok"]
    bad = [r for r in rows if r["cat"] != "ok"]
    by_cat = {}
    for r in bad:
        by_cat[r["cat"]] = by_cat.get(r["cat"], 0) + 1
    rate = len(bad) / len(rows) if rows else 0
    print("\n=== 答复失败率 ===")
    print(f"  题数 {len(rows)}；有效回答 {len(ok)}；失败 {len(bad)}（{rate:.1%}）")
    print(f"  分类：{by_cat or '无'}")
    print(f"  延迟中位：ok={statistics.median([r['ms'] for r in ok]) if ok else 0:.0f}ms")
    verdict = "fail" if rate >= 0.10 else ("watch" if rate >= 0.05 else "ok")
    print(f"  决策：{verdict}（≥10% 立项修复；<5% 可接受）")
    if by_cat:
        top = max(by_cat.items(), key=lambda kv: kv[1])
        print(f"  最高频失败类别：{top[0]}（{top[1]} 条）→ 下一轮修复目标")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"answer_failure_rate_{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps({"verdict": verdict, "rate": round(rate, 4),
                             "by_cat": by_cat, "rows": rows}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"\nwritten {p}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
