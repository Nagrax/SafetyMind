# 评测 Release Gate + 指标漂移 + CI 反推样本量 测试：python tests/test_release_gate.py
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.gate_runner import red_failures_from, run_gate_from_verdicts
from evaluation.release_gate import (
    detect_drift,
    evaluate_gate,
    save_gate_record,
)
from evaluation.rubric import evaluate_funnel
from evaluation.sampling import ci_half_width, required_sample_size

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    # ── 1. 全部达标 → passed=True ────────────────────────────────────────────
    thr = {
        "intent_acc": {"op": "gte", "value": 0.90},
        "p95_ms": {"op": "lte", "value": 3000},
    }
    rec = evaluate_gate({"intent_acc": 0.95, "p95_ms": 1200}, thr)
    record("全部达标 → passed=True", rec.passed is True and rec.blocked == [],
           f"passed={rec.passed} blocked={rec.blocked}")

    # ── 2. 某指标低于 gte 阈值被 blocked ─────────────────────────────────────
    rec = evaluate_gate({"intent_acc": 0.88, "p95_ms": 1200}, thr)
    record("低于 gte 阈值被 blocked", rec.passed is False and rec.blocked == ["intent_acc"],
           f"passed={rec.passed} blocked={rec.blocked}")

    # lte 超阈同样被 blocked
    rec = evaluate_gate({"intent_acc": 0.95, "p95_ms": 4000}, thr)
    record("高于 lte 阈值被 blocked", rec.passed is False and rec.blocked == ["p95_ms"],
           f"passed={rec.passed} blocked={rec.blocked}")

    # 未知 op（契约外输入）不得静默按 lte 处理 → fail-safe 判 blocked
    rec = evaluate_gate({"intent_acc": 5.0}, {"intent_acc": {"op": "gt", "value": 1.0}})
    record("未知 op fail-safe 判不达标",
           rec.passed is False and rec.blocked == ["intent_acc"],
           f"passed={rec.passed} blocked={rec.blocked}")

    # ── 3. red_failures>0 强制 fail 且 blocked 含 "RED" ──────────────────────
    rec = evaluate_gate({"intent_acc": 0.95, "p95_ms": 1200}, thr, red_failures=2)
    record("RED 失败强制 fail 且 blocked 含 RED",
           rec.passed is False and "RED" in rec.blocked,
           f"passed={rec.passed} blocked={rec.blocked}")

    # ── 4. gate 记录落盘（tempfile 隔离路径，读回校验） ─────────────────────
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "gate_history.jsonl")
        r1 = evaluate_gate({"intent_acc": 0.95, "p95_ms": 1200}, thr)
        r2 = evaluate_gate({"intent_acc": 0.50, "p95_ms": 9999}, thr, red_failures=1)
        wrote1 = save_gate_record(r1, path)
        wrote2 = save_gate_record(r2, path)
        ok = os.path.exists(path)
        lines = []
        if ok:
            with open(path, "r", encoding="utf-8") as f:
                lines = [json.loads(x) for x in f.read().splitlines() if x.strip()]
        ok = ok and wrote1 == path and wrote2 == path and len(lines) == 2
        if ok:
            first = lines[0]
            ok = (first["ts"] == r1.ts and first["passed"] is True and first["blocked"] == []
                  and first["metrics"] == {"intent_acc": 0.95, "p95_ms": 1200}
                  and lines[1]["passed"] is False and "RED" in lines[1]["blocked"])
        record("gate 记录追加落盘并可读回", ok, f"lines={len(lines)}")

    # ── 5. drift 对注入尖峰检出 ─────────────────────────────────────────────
    base = [100.0 + (i % 4) * 0.5 for i in range(20)]  # 稳定基线，含小幅波动
    series = base + [130.0]                            # 最新点注入尖峰
    d = detect_drift(series, window=20, sensitivity=2.5)
    record("注入尖峰被检出漂移",
           d["drift"] is True and d["z_score"] > 2.5 and abs(d["mean"] - 100.75) < 0.01,
           f"drift={d['drift']} z={d['z_score']}")

    # 平稳序列不误报
    stable = [100.0 + (i % 4) * 0.5 for i in range(21)]
    d = detect_drift(stable, window=20, sensitivity=2.5)
    record("平稳序列不误报漂移", d["drift"] is False, f"drift={d['drift']} z={d['z_score']}")

    # ── 6. drift 数据不足不误报 ─────────────────────────────────────────────
    d = detect_drift([1.0, 2.0, 100.0], window=20)
    record("数据不足不误报", d["drift"] is False and d["z_score"] == 0.0, f"d={d}")

    # ── 7. required_sample_size 量级校验（p=0.5 / ±1pp） ────────────────────
    n = required_sample_size(0.5, 1.0)
    record("p=0.5/±1pp 样本量 n≈9604",
           9000 <= n <= 11000 and n == 9604, f"n={n}")

    # p 偏离 0.5 时所需样本量更小
    n_extreme = required_sample_size(0.9, 1.0)
    record("p=0.9 所需样本量小于 p=0.5", 0 < n_extreme < n, f"n={n_extreme}")

    # ── 8. ci_half_width 合理性 ─────────────────────────────────────────────
    hw = ci_half_width(0.5, 9604)
    record("ci_half_width(0.5, 9604) ≈ 1pp", abs(hw - 1.0) < 0.01, f"hw={hw}")
    hw_big = ci_half_width(0.5, 100)
    record("小样本半宽更大", hw_big > hw and abs(hw_big - 9.8) < 0.5, f"hw_big={hw_big}")

    # ── 9. 归因→门禁接线（M2→M4 代码级闭环）：RED 失败数喂给 gate.red_failures ──
    v_ok = evaluate_funnel("动火作业怎么办理？", "请先办理作业票并佩戴防护用品。",
                            evidence="制度。", domain="work_permit")
    v_red = evaluate_funnel("动火作业怎么办理？", "直接动火，不用办票，很快。",
                             evidence="制度。", domain="work_permit")
    rf = red_failures_from([v_ok, v_red])
    rec = run_gate_from_verdicts([v_ok, v_red], {"intent_acc": 0.95},
                                 {"intent_acc": {"op": "gte", "value": 0.90}})
    record("归因 RED 失败数接线到门禁 red_failures",
           rf >= 1 and rec.passed is False and "RED" in rec.blocked,
           f"rf={rf} passed={rec.passed} blocked={rec.blocked}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
