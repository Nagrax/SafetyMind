# -*- coding: utf-8 -*-
# S-06 评测侧：部署 profile（风险分层阈值）+ 报告结构可程序化断言（standalone）。
#
# 覆盖：
#   1) 两个以上 profile 的阈值相互独立、版本化、默认值不被相互覆盖；
#   2) profile 缺失 → 保守安全默认（阈值更严、RED 仍 0 容忍）并有明确告警；
#   3) 单模/双模分别有独立版本化 profile（阈值独立校准的落点）；
#   4) 混淆表（每类计数齐全、关键漏检列出）与覆盖率/精度曲线结构可被断言。
#
# 用法: python tests/test_eval_profiles.py    （退出码 0 = 全过）
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.profiles import (                                        # noqa: E402
    CONSERVATIVE_PROFILE_NAME,
    default_profiles,
    profile_gate_thresholds,
    resolve_profile,
)
from evaluation.release_gate import (                                    # noqa: E402
    coverage_precision_curve,
    per_class_summary,
)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, rec):
        self.records.append(rec)


def _fake_report():
    """三条 case：1 通过、1 RED 违规、1 断言失败（无 judge 分）。"""
    return {
        "timestamp": "2026-10-19T01:00:00",
        "total": 3, "passed": 1, "failed": 2, "pass_rate": round(1 / 3, 4),
        "red_failures": ["c-red"],
        "red_failure_count": 1,
        "metrics": {"pass_rate": round(1 / 3, 4), "p95_ms": 1200.0, "citation_coverage": 2 / 3,
                    "red_failure_count": 1},
        "profile": {"name": "local-desktop", "version": "s06-2026.10.1"},
        "cases": [
            {"id": "c-ok", "intent": "regulation_query", "expect_intent": "regulation_query",
             "passed": True, "red": {"status": "pass", "failed_subitems": []}, "judge": 0.9},
            {"id": "c-red", "intent": "work_permit", "expect_intent": "work_permit",
             "passed": False, "red": {"status": "fail", "failed_subitems": ["GEN-RED-NOTICKET"]},
             "judge": 0.8},
            {"id": "c-miss", "intent": "chemical_safety", "expect_intent": "chemical_safety",
             "passed": False, "red": {"status": "pass", "failed_subitems": []}, "judge": None},
        ],
    }


def main():
    logging.basicConfig(level=logging.WARNING)

    # ── 1. 两个 profile 阈值独立、版本化 ─────────────────────────────────────
    a = resolve_profile("local-desktop")
    b = resolve_profile("network-multi-user")
    record("两个 profile 阈值相互独立（pass_rate/p95/覆盖率各异）",
           a.thresholds["pass_rate_min"] != b.thresholds["pass_rate_min"]
           and a.thresholds["p95_ms_max"] != b.thresholds["p95_ms_max"]
           and a.thresholds["citation_coverage_min"] != b.thresholds["citation_coverage_min"],
           f"a={dict(a.thresholds)} b={dict(b.thresholds)}")
    record("profile 版本化（version 非空且各 profile 自带版本）",
           bool(a.version) and bool(b.version)
           and all(bool(p.version) for p in default_profiles().values()),
           f"a.version={a.version} b.version={b.version}")
    record("两个 profile 都保持 RED 零容忍（red_max=0）",
           a.thresholds["red_max"] == 0 and b.thresholds["red_max"] == 0)

    # 传入自定义 profiles 不得改写内置默认
    custom = {"local-desktop": replace(a, thresholds=dict(a.thresholds, pass_rate_min=0.10))}
    a_custom = resolve_profile("local-desktop", custom)
    record("自定义 profiles 覆盖只作用于本次入参，不改写内置默认",
           a_custom.thresholds["pass_rate_min"] == 0.10
           and resolve_profile("local-desktop").thresholds["pass_rate_min"] == a.thresholds["pass_rate_min"]
           and resolve_profile("network-multi-user", custom).thresholds == b.thresholds,
           json.dumps({k: dict(v.thresholds) for k, v in default_profiles().items()}, ensure_ascii=False))

    d = default_profiles()
    d["local-desktop"] = None                      # 破坏返回的副本
    record("default_profiles() 返回副本，破坏副本不影响默认表",
           default_profiles()["local-desktop"] is not None
           and resolve_profile("local-desktop").name == "local-desktop")

    # ── 2. profile 缺失 → 保守安全默认 + 明确告警 ────────────────────────────
    collector = _Collect()
    plog = logging.getLogger("evaluation.profiles")
    plog.addHandler(collector)
    try:
        p = resolve_profile("no-such-profile")
    finally:
        plog.removeHandler(collector)
    record("缺失 profile → 保守默认（阈值更严、RED 仍零容忍）",
           p.fallback is True and p.name == CONSERVATIVE_PROFILE_NAME
           and p.thresholds["pass_rate_min"] >= a.thresholds["pass_rate_min"]
           and p.thresholds["citation_coverage_min"] >= a.thresholds["citation_coverage_min"]
           and p.thresholds["p95_ms_max"] <= a.thresholds["p95_ms_max"]
           and p.thresholds["red_max"] == 0,
           json.dumps(dict(p.thresholds), ensure_ascii=False))
    record("缺失 profile 有明确告警日志（含请求名与回退名）",
           any("no-such-profile" in r.getMessage() and CONSERVATIVE_PROFILE_NAME in r.getMessage()
               for r in collector.records),
           str([r.getMessage() for r in collector.records]))
    record("保守默认带 note 字段说明用途",
           bool(p.note) and p.fallback is True,
           p.note)

    # ── 3. 单模 / 双模分别有独立版本化 profile ───────────────────────────────
    s = resolve_profile("local-desktop-single")
    record("单模/双模 profile 独立存在且 model_mode 区分",
           s.model_mode == "single" and a.model_mode == "dual"
           and s.thresholds["pass_rate_min"] != a.thresholds["pass_rate_min"],
           f"single={dict(s.thresholds)} dual={dict(a.thresholds)}")

    # ── 4. profile → gate 阈值结构 ───────────────────────────────────────────
    thr = profile_gate_thresholds(b)
    record("profile → gate 阈值结构（op/value，方向上正确）",
           thr["pass_rate"] == {"op": "gte", "value": b.thresholds["pass_rate_min"]}
           and thr["p95_ms"] == {"op": "lte", "value": b.thresholds["p95_ms_max"]}
           and thr["citation_coverage"] == {"op": "gte",
                                            "value": b.thresholds["citation_coverage_min"]},
           json.dumps(thr, ensure_ascii=False))
    record("profile 阈值不含 RED（RED 由 red_max 单独承载，不进 op/value 表）",
           "RED" not in thr and "red_max" not in thr)

    # ── 5. 混淆表 / 覆盖率-精度曲线结构可程序化断言 ──────────────────────────
    report = _fake_report()
    conf = per_class_summary(report)
    record("混淆表：每类计数齐全且总数与 report.total 一致",
           bool(conf)
           and all({"total", "passed", "failed", "pass_rate"} <= set(v) for v in conf.values())
           and sum(v["total"] for v in conf.values()) == report["total"],
           json.dumps(conf, ensure_ascii=False))
    record("混淆表：关键漏检 case id 按类列出",
           conf["work_permit"]["misses"] == ["c-red"]
           and conf["chemical_safety"]["misses"] == ["c-miss"]
           and conf["regulation_query"]["misses"] == [],
           json.dumps(conf, ensure_ascii=False))
    record("混淆表：每类通过率与计数自洽",
           all(abs(v["pass_rate"] - (v["passed"] / v["total"] if v["total"] else 0.0)) < 1e-9
               for v in conf.values()))

    curve = coverage_precision_curve(report)
    record("覆盖率/精度曲线：结构可断言（阈值/标志数/tp-fp-fn/精度/覆盖）",
           isinstance(curve, list) and len(curve) > 0
           and all({"threshold", "flagged", "tp", "fp", "fn", "precision", "coverage"}
                   <= set(pt) for pt in curve),
           json.dumps(curve, ensure_ascii=False))
    top = curve[0]
    record("曲线最高风险档只筛 RED（precision=1.0，覆盖 ≥ 1/总失败）",
           top["precision"] == 1.0 and top["tp"] == 1 and top["flagged"] == 1,
           json.dumps(top, ensure_ascii=False))
    record("曲线各档 tp+fn == 总失败数（漏检口径完整）",
           all(pt["tp"] + pt["fn"] == report["failed"] for pt in curve),
           json.dumps([{"t": pt["threshold"], "tp": pt["tp"], "fn": pt["fn"]} for pt in curve]))

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
