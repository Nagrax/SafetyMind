# -*- coding: utf-8 -*-
"""LLM Judge 可信度校准模块测试（standalone）。

用法: python tests/test_judge_calibration.py
纯 stdlib，dead-LLM 可跑：所有"评判器"都是注入的确定性 stub。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.judge_calibration import (  # noqa: E402
    cohens_kappa,
    golden_kappa,
    length_controlled,
    swap_consistency,
    validate_structured,
)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    # ── 1. 完全一致 → kappa = 1 ──
    x = [0, 1, 1, 0, 1, 0]
    k = cohens_kappa(x, list(x))
    record("完全一致 kappa=1", abs(k - 1.0) < 1e-9, f"kappa={k}")

    # ── 2. 完全不一致 → kappa < 0 ──
    k = cohens_kappa([0, 0, 1, 1], [1, 1, 0, 0])
    record("完全不一致 kappa<0", k < 0 and abs(k + 1.0) < 1e-9, f"kappa={k}")

    # ── 3. 部分一致 → kappa 与手算值吻合 ──
    # a 3 个 0/3 个 1；b 2 个 0/4 个 1；一致 5 对 po=0.8333, pe=0.5 → kappa=2/3
    k = cohens_kappa([0, 0, 0, 1, 1, 1], [0, 0, 1, 1, 1, 1])
    record("部分一致 kappa 与手算吻合", abs(k - 2 / 3) < 1e-9, f"kappa={k}")

    # ── 4. 退化/异常输入不崩 ──
    try:
        cases = [
            cohens_kappa([], []),                    # 空
            cohens_kappa([1], [1]),                  # 样本不足
            cohens_kappa([1, 1, 1], [1, 1, 1]),      # pe=1 退化
            cohens_kappa([0, 1], [0]),               # 长度不等
            cohens_kappa([0, 0, 1, 1], [0, 1, 0, 1]),  # po=pe=0.5 退化 → 0
        ]
        record("退化输入不崩且返回 0.0", all(v == 0.0 for v in cases), str(cases))
    except Exception as ex:  # pragma: no cover
        record("退化输入不崩且返回 0.0", False, f"抛异常: {ex}")

    # ── 5. golden_kappa 口径（程序化判定 vs judge，非人标）──
    g = golden_kappa([True, True, False, False], [True, True, True, False])
    ok_g = (g["n"] == 4 and abs(g["agreement"] - 0.75) < 1e-9 and abs(g["kappa"] - 0.5) < 1e-9)
    record("golden_kappa 口径正确", ok_g, str(g))
    g_empty = golden_kappa([], [])
    record("golden_kappa 空输入 n=0 不崩",
           g_empty == {"kappa": 0.0, "agreement": 0.0, "n": 0}, str(g_empty))

    # ── 6. swap_consistency：位置偏差（总选第一个）翻转率 ──
    first_picker = lambda a, b: a          # noqa: E731  总选第一个
    pairs = [("选项甲", "选项乙"), ("A", "B"), ("左", "右"), ("1", "2")]
    r_bias = swap_consistency(first_picker, pairs)
    record("位置偏向 judge flip_rate 高",
           r_bias["n"] == 4 and r_bias["flip_rate"] == 1.0, str(r_bias))
    consistent = lambda a, b: "等价"       # noqa: E731  结论与顺序无关
    r_ok = swap_consistency(consistent, pairs)
    record("稳定 judge flip_rate=0",
           r_ok["n"] == 4 and r_ok["flip_rate"] == 0.0, str(r_ok))
    record("空 pairs 不崩", swap_consistency(first_picker, []) == {"flip_rate": 0.0, "n": 0})

    # ── 7. length_controlled：冗长偏差校正方向 ──
    # 短回复 0.2×5，长回复 0.6×5：原分下长回复通过（0.5 阈值），
    # 校正掉长度加成后所有回复归到 0.4 → 校正通过率应低于原始通过率
    scores = [0.2] * 5 + [0.6] * 5
    lengths = [1.0] * 5 + [100.0] * 5
    lc = length_controlled(scores, lengths)
    record("冗长偏差：corr 为正", lc["corr"] > 0.9, str(lc))
    record("冗长偏差：校正通过率被拉低",
           lc["corrected_pass_rate"] < lc["raw_pass_rate"]
           and abs(lc["raw_pass_rate"] - 0.5) < 1e-9
           and abs(lc["corrected_pass_rate"] - 0.0) < 1e-9, str(lc))
    lc_flat = length_controlled([0.6, 0.6, 0.6], [3.0, 3.0, 3.0])
    record("长度零方差时校正无副作用",
           lc_flat["corrected_pass_rate"] == lc_flat["raw_pass_rate"] == 1.0, str(lc_flat))

    # ── 8. validate_structured 三层兜底 ──
    good = '```json\n{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": "高"}\n```'
    v = validate_structured(good)
    record("合法 JSON 通过并归一化",
           v["ok"] and v["stage_failed"] == ""
           and abs(v["values"]["relevance"] - 0.9) < 1e-9
           and abs(v["values"]["helpfulness"] - 0.9) < 1e-9, str(v))

    bad_json = "{relevance: 0.9, accuracy: 0.8}"          # 非法 JSON（键未加引号）
    v = validate_structured(bad_json)
    record("坏 JSON 在 extract 阶段拦截",
           (not v["ok"]) and v["stage_failed"] == "extract", str(v))

    missing = '{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7}'
    v = validate_structured(missing)
    record("缺字段在 schema 阶段拦截",
           (not v["ok"]) and v["stage_failed"] == "schema", str(v))

    out_of_range = '{"relevance": 1.5, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.9}'
    v = validate_structured(out_of_range)
    record("超范围值在 normalize 阶段拦截",
           (not v["ok"]) and v["stage_failed"] == "normalize", str(v))

    bad_label = '{"relevance": "很好", "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.9}'
    v = validate_structured(bad_label)
    record("无法归一化的标签被拦截",
           (not v["ok"]) and v["stage_failed"] == "normalize", str(v))

    v = validate_structured("完全不是 JSON 的自由文本")
    record("无 JSON 文本在 extract 阶段拦截",
           (not v["ok"]) and v["stage_failed"] == "extract", str(v))

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
