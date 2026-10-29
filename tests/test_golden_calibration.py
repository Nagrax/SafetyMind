# -*- coding: utf-8 -*-
"""人标 golden 与 Judge 一致性（κ）+ 分歧裁决 测试（standalone）。

用法: python tests/test_golden_calibration.py
纯 stdlib，dead-LLM 可跑：judge 分为注入的确定性离线 stub（非真实 LLM 评审）。

覆盖：
  1) κ 计算正确性：手工可验小样本，含完全一致 κ=1 与完全相反 κ<0 两个极端；
  2) 缺 label_kind / labeler / note / 响应证据 → load_golden 直接报错（fail-loud）；
  3) 分歧清单逐条可核对；裁决留痕且不覆盖原始标签（pending / 非法 case_id 均显式处理）；
  4) report 分口径（project_golden 与 synthetic 独立成块，不合并）+ disclaimer + 不确定性区间；
  5) golden 文件本身的结构校验（case_id 唯一、label 合法、顶层 provenance/disclaimer 齐备）。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.golden_calibration import (  # noqa: E402
    DEFAULT_GOLDEN_PATH,
    adjudicate,
    bootstrap_kappa_ci,
    compare_with_judge,
    load_golden,
    report,
    wilson_interval,
)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# 离线 stub judge 分（非真实 LLM 评审；与 scratch 运行脚本一致）
JUDGE = {
    "cal-emg-leak": 0.92, "cal-emg-fire": 0.88, "cal-emg-negation": 0.80,
    "cal-cite-permit-validity": 0.90, "cal-cite-confined-detect": 0.86,
    "cal-tool-ethanol-cas": 0.84, "cal-tool-cas-lookup": 0.88,
    "cal-gap-unknown-cas": 0.82,          # judge 过松：golden=fail 而 judge=pass → 漏报
    "cal-greeting": 0.78, "cal-irrelevant": 0.55,   # judge 过严：golden=pass 而 judge=fail → 误报
    "frz-emg-spread": 0.90, "frz-emg-explosion": 0.86, "frz-drill-precision": 0.40,
    "frz-cite-hazard-frequency": 0.85, "frz-tool-methanol": 0.87,
    "frz-cite-ledger-scheme": 0.83, "real-citation": 0.91, "real-msds": 0.89,
}


def _mk_golden(path, labels, provenance="provenance-x", disclaimer="disclaimer-x"):
    payload = {"schema": 1, "provenance": provenance, "disclaimer": disclaimer, "labels": labels}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _entry(case_id, label="pass", **over):
    e = {"case_id": case_id, "message": "m-" + case_id, "label": label,
         "labeler": "tester", "label_kind": "project_golden",
         "rubric_rule_hit": "r", "note": "离线打桩响应",
         "response_excerpt": "resp"}
    e.update(over)
    return e


def test_kappa_extremes():
    # 完全一致（混合标签，避免退化 pe=1）→ κ=1
    golden = [_entry("a", "pass"), _entry("b", "fail"), _entry("c", "pass"),
              _entry("d", "fail"), _entry("e", "pass")]
    judge = {"a": 0.9, "b": 0.1, "c": 0.8, "d": 0.2, "e": 0.85}
    res = compare_with_judge(golden, judge, threshold=0.75)
    record("完全一致 → κ=1 且 accuracy=1",
           abs(res["kappa"] - 1.0) < 1e-9 and abs(res["accuracy"] - 1.0) < 1e-9
           and res["disagreements"] == [],
           json.dumps(res, ensure_ascii=False))

    # 完全相反 → κ<0（= -1）
    golden2 = [_entry("a", "fail"), _entry("b", "fail"), _entry("c", "pass"), _entry("d", "pass")]
    judge2 = {"a": 0.9, "b": 0.9, "c": 0.1, "d": 0.1}
    res2 = compare_with_judge(golden2, judge2, threshold=0.75)
    record("完全相反 → κ=-1",
           res2["kappa"] < 0 and abs(res2["kappa"] + 1.0) < 1e-9
           and res2["confusion"] == {"tp": 0, "fp": 2, "tn": 0, "fn": 2},
           json.dumps(res2, ensure_ascii=False))

    # 阈值可配：同一分数在低阈值下二值化结果改变
    res_lo = compare_with_judge(golden2, judge2, threshold=0.5)
    record("阈值可配（0.5 时 0.9→pass 仍 pass，0.1→fail 仍 fail，一致性不变）",
           res_lo["threshold"] == 0.5 and abs(res_lo["kappa"] + 1.0) < 1e-9)


def test_load_golden_structure():
    golden = load_golden()
    kinds = {g["label_kind"] for g in golden}
    record("load_golden 读取真实 golden 文件（18 条，含 case_id/labeler/label_kind）",
           len(golden) == 18 and all(g["case_id"] and g["labeler"] and g["label_kind"] for g in golden),
           f"n={len(golden)} kinds={kinds}")
    record("真实 golden 的 label_kind 均为 project_golden（domain_expert 尚未产出）",
           kinds == {"project_golden"}, str(kinds))
    record("每条 label ∈ {pass,fail} 且有响应证据（excerpt 或 fingerprint）",
           all(g["label"] in ("pass", "fail")
               and (g["response_excerpt"] or g["response_fingerprint"]) for g in golden))

    data = json.loads(Path(DEFAULT_GOLDEN_PATH).read_text(encoding="utf-8"))
    record("顶层 provenance/disclaimer 齐备且声明非领域专家",
           bool(data.get("provenance")) and bool(data.get("disclaimer"))
           and "领域专家" in data["disclaimer"] and "不能替代" in data["disclaimer"],
           f"disclaimer={data.get('disclaimer','')[:60]}")

    real_kinds = data.get("provenance", {}).get("label_kind_values")
    record("provenance 声明 label_kind 取值区分 project_golden 与 domain_expert",
           isinstance(real_kinds, list) and "project_golden" in real_kinds
           and "domain_expert" in real_kinds, str(real_kinds))


def test_load_golden_fail_loud():
    tmpdir = Path(tempfile.mkdtemp(prefix="golden_test_", dir="data"))

    def _raises(labels, name, expect_msg):
        p = tmpdir / f"{name}.json"
        _mk_golden(str(p), labels)
        try:
            load_golden(str(p))
            return False, "未报错"
        except ValueError as ex:
            return (expect_msg in str(ex)), str(ex)

    ok_kind, msg_kind = _raises([_entry("a", label_kind="")], "no_kind", "label_kind")
    record("缺 label_kind → ValueError", ok_kind, msg_kind)

    ok_lab, msg_lab = _raises([_entry("a", labeler="")], "no_labeler", "labeler")
    record("缺 labeler → ValueError", ok_lab, msg_lab)

    ok_note, msg_note = _raises([_entry("a", note="")], "no_note", "note")
    record("缺 note → ValueError", ok_note, msg_note)

    ok_ev, msg_ev = _raises([_entry("a", response_excerpt="", response_fingerprint="")],
                            "no_evidence", "响应证据")
    record("缺响应证据 → ValueError", ok_ev, msg_ev)

    ok_bad, msg_bad = _raises([_entry("a", label="maybe")], "bad_label", "label")
    record("非法 label → ValueError", ok_bad, msg_bad)

    ok_dup, msg_dup = _raises([_entry("a"), _entry("a")], "dup", "重复")
    record("case_id 重复 → ValueError", ok_dup, msg_dup)

    ok_badkind, msg_badkind = _raises([_entry("a", label_kind="expert")], "bad_kind", "label_kind")
    record("非法 label_kind → ValueError", ok_badkind, msg_badkind)

    p = tmpdir / "no_prov.json"
    p.write_text(json.dumps({"labels": [_entry("a")]}, ensure_ascii=False), encoding="utf-8")
    try:
        load_golden(str(p))
        ok_prov, msg_prov = False, "未报错"
    except ValueError as ex:
        ok_prov, msg_prov = ("provenance" in str(ex)), str(ex)
    record("缺顶层 provenance/disclaimer → ValueError", ok_prov, msg_prov)

    for f in tmpdir.glob("*"):
        f.unlink(missing_ok=True)
    tmpdir.rmdir()


def test_compare_real_file():
    golden = load_golden()
    res = compare_with_judge(golden, JUDGE, threshold=0.75)
    d_ids = {d["case_id"]: d for d in res["disagreements"]}
    record("真实 golden vs 离线 judge：n=18，two 分歧 且 κ 介于 0~1",
           res["n"] == 18 and res["n_missing"] == 0 and 0.0 < res["kappa"] < 1.0,
           f"kappa={res['kappa']} n={res['n']}")
    record("分歧清单逐条可核对：cal-gap-unknown-cas=judge_miss、cal-irrelevant=judge_false_alarm",
           len(res["disagreements"]) == 2
           and d_ids.get("cal-gap-unknown-cas", {}).get("disagreement_type") == "judge_miss"
           and d_ids.get("cal-gap-unknown-cas", {}).get("golden") == "fail"
           and d_ids.get("cal-irrelevant", {}).get("disagreement_type") == "judge_false_alarm"
           and d_ids.get("cal-irrelevant", {}).get("judge_value") == 0.55,
           json.dumps(res["disagreements"], ensure_ascii=False))
    record("混淆矩阵自洽（tp+fp+tn+fn == n）",
           sum(res["confusion"].values()) == res["n"], str(res["confusion"]))

    res_seq = compare_with_judge(golden, [JUDGE[g["case_id"]] for g in golden], threshold=0.75)
    record("judge_scores 支持等长序列入参（与 Mapping 结果一致）",
           abs(res_seq["kappa"] - res["kappa"]) < 1e-12
           and res_seq["confusion"] == res["confusion"])

    res_missing = compare_with_judge(golden, {"cal-emg-leak": 0.9}, threshold=0.75)
    record("judge 缺项记入 missing 且不参与统计",
           res_missing["n"] == 1 and res_missing["n_missing"] == 17, str(res_missing["n_missing"]))


def test_adjudicate_trace():
    golden = load_golden()
    cmp_res = compare_with_judge(golden, JUDGE, threshold=0.75)
    dis = cmp_res["disagreements"]

    decisions = {
        "cal-gap-unknown-cas": {"decided_by": "rubric-owner", "basis": "响应编造“第 5 条”，维持 golden fail",
                                "final_label": "fail", "write_back": False},
        "cal-irrelevant": {"decided_by": "rubric-owner", "basis": "答复礼貌拒绝且未越界，golden pass 正确",
                           "final_label": "pass", "write_back": True},
    }
    adj = adjudicate(dis, decisions)
    recs = {r["case_id"]: r for r in adj["records"]}
    record("裁决留痕：每条保留 original_label（不覆盖原始标签）",
           adj["original_preserved"] is True
           and recs["cal-gap-unknown-cas"]["original_label"] == "fail"
           and recs["cal-irrelevant"]["original_label"] == "pass"
           and all(r["original_label"] in ("pass", "fail") for r in adj["records"]),
           json.dumps(adj["records"], ensure_ascii=False))
    record("裁决记录含 decided_by/basis/final_label/judge/write_back",
           all(r["decided_by"] and r["basis"] and r["final_label"]
               and "judge_value" in r and "write_back" in r for r in adj["records"]))
    record("write_back 只在显式声明时置真（cal-irrelevant 可回写、cal-gap 不回写）",
           recs["cal-irrelevant"]["write_back"] is True
           and recs["cal-gap-unknown-cas"]["write_back"] is False)
    record("裁决不改写输入 golden（原始清单标签不变）",
           all(g["label"] in ("pass", "fail") for g in golden)
           and {d["case_id"]: d["golden"] for d in dis}
           == {r["case_id"]: r["original_label"] for r in adj["records"]})

    adj_part = adjudicate(dis, {"cal-gap-unknown-cas": decisions["cal-gap-unknown-cas"]})
    record("未裁决的分歧进入 pending（不被顺手改写）",
           adj_part["pending"] == ["cal-irrelevant"] and adj_part["adjudicated"] == 1,
           str(adj_part["pending"]))

    try:
        adjudicate(dis, {"not-a-disagreement": {"decided_by": "x", "basis": "y", "final_label": "pass"}})
        ok_unknown, msg_unknown = False, "未报错"
    except ValueError as ex:
        ok_unknown, msg_unknown = ("非分歧" in str(ex)), str(ex)
    record("裁决非分歧 case_id → ValueError", ok_unknown, msg_unknown)


def test_report_scopes_and_uncertainty():
    golden = load_golden()
    cmp_res = compare_with_judge(golden, JUDGE, threshold=0.75)
    decisions = {
        "cal-gap-unknown-cas": {"decided_by": "rubric-owner", "basis": "维持 fail", "final_label": "fail"},
        "cal-irrelevant": {"decided_by": "rubric-owner", "basis": "维持 pass", "final_label": "pass"},
    }
    adj = adjudicate(cmp_res["disagreements"], decisions)

    # synthetic 口径：从同一批用例的断言结果取（label_source=synthetic），与人标分开报告
    synthetic = [
        {"case_id": "cal-emg-leak", "message": "x", "label": "pass", "label_source": "synthetic"},
        {"case_id": "cal-gap-unknown-cas", "message": "x", "label": "fail", "label_source": "synthetic"},
    ]
    rep = report(golden, JUDGE, threshold=0.75, adjudications=adj, synthetic=synthetic,
                 bootstrap_n=500, seed=7)

    record("report 分口径：project_golden 独立成块，synthetic 独立成块（不合并）",
           "project_golden" in rep["scopes"] and rep["synthetic"] is not None
           and "combined" not in rep and "synthetic" not in rep["scopes"],
           json.dumps(sorted(rep["scopes"]), ensure_ascii=False))
    record("report 含 disclaimer 且明确非领域专家",
           isinstance(rep["disclaimer"], str) and "领域专家" in rep["disclaimer"])
    pg = rep["scopes"]["project_golden"]
    record("report 报不确定性：κ 的 bootstrap 区间 + 准确率 Wilson 区间 + n",
           pg["kappa_ci95"]["method"] == "percentile_bootstrap"
           and pg["kappa_ci95"]["n_resamples"] == 500
           and {"low", "high"} <= set(pg["accuracy_ci95"])
           and pg["n"] == 18, json.dumps(pg["kappa_ci95"], ensure_ascii=False))
    record("project_golden 与 synthetic 的 κ 各自独立（未知二者是否相等即证明未合并）",
           isinstance(pg["kappa"], float) and isinstance(rep["synthetic"]["kappa"], float)
           and rep["synthetic"]["n"] == 2)
    record("report 附带裁决留痕且 original_preserved=True",
           rep["adjudications"]["original_preserved"] is True
           and rep["adjudications"]["adjudicated"] == 2)

    # 区间边界单调（Wilson 区间 low<=accuracy<=high）
    record("Wilson 区间单调且 n=0 安全",
           (lambda ci, acc: ci["low"] <= acc <= ci["high"])(wilson_interval(14, 18), 14 / 18)
           and wilson_interval(0, 0) == {"low": 0.0, "high": 0.0})
    record("bootstrap κ 区间在样本不足时标记 degenerate",
           bootstrap_kappa_ci([("pass", "pass")])["degenerate"] is True)


def main():
    test_kappa_extremes()
    test_load_golden_structure()
    test_load_golden_fail_loud()
    test_compare_real_file()
    test_adjudicate_trace()
    test_report_scopes_and_uncertainty()

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
