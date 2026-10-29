"""风险分层评测的校准/冻结流程（S-06 数据集纪律 + 阈值标定）。

解决的问题：阈值不能"看完冻结集结果再挑"，也不能用一次运行的通过率冒充发布结论。

纪律（落成代码，不靠口头约定）：
  1. **阈值只在 calibration 集上选定**（复用 release_gate.select_thresholds，非校准集直接报错）；
  2. **frozen 集只评估不调参**（复用 release_gate.evaluate_frozen，拒绝回选）；
  3. 评测**经真实应用入口**执行（复用 e2e_runner.run_e2e_async），证据/工具轨迹来自实际执行；
  4. 报告每类混淆、关键漏检、误升级探针与覆盖率/精度曲线；标签来源如实标注
     （本用例集 label_source=synthetic，**不是领域人标**）；
  5. 失败运行不晋升基线（promote_baseline 默认不晋升）。

本模块不真调模型：chat_fn 可注入；真实模型校准由调用方在允许的窗口内执行。
"""
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from evaluation.e2e_runner import run_e2e_async
from evaluation.release_gate import (
    CALIBRATION,
    FROZEN,
    coverage_precision_curve,
    evaluate_frozen,
    make_eval_set,
    per_class_summary,
    select_thresholds,
)

logger = logging.getLogger(__name__)

DEFAULT_CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "risk_cases.json")
VALID_SPLITS = (CALIBRATION, FROZEN)

DISCLAIMER = (
    "样本标签为合成标注（label_source=synthetic），非领域人标；阈值仅在 calibration 集上选定，"
    "frozen 集只评估不调参；本报告不承诺真实上线收益，也不代表已完成领域专家盲测。"
)


def load_cases(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取用例集（默认 evaluation/risk_cases.json，可被 SAFETYMIND_RISK_CASES_PATH 覆盖）。

    校验：id 唯一非空、message 非空、split ∈ {calibration, frozen}、label_source 必须显式给出。
    任何缺项直接报错（fail-loud：让"标签来源不明"没法蒙混过关）。
    """
    p = path or os.environ.get("SAFETYMIND_RISK_CASES_PATH") or DEFAULT_CASES_PATH
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"用例集 {p} 为空或结构不对")
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, c in enumerate(raw):
        if not isinstance(c, Mapping):
            raise TypeError(f"第 {i} 条 case 必须是映射")
        cid = str(c.get("id") or "").strip()
        if not cid:
            raise ValueError(f"第 {i} 条 case 缺 id")
        if cid in seen:
            raise ValueError(f"case id 重复：{cid}")
        seen.add(cid)
        msg = c.get("message")
        if not isinstance(msg, str) or not msg.strip():
            raise ValueError(f"case {cid} 缺 message（非空字符串）")
        split = str(c.get("split") or "")
        if split not in VALID_SPLITS:
            raise ValueError(f"case {cid} 的 split={split!r} 非法，只允许 {VALID_SPLITS}")
        src = str(c.get("label_source") or "").strip()
        if not src:
            raise ValueError(f"case {cid} 必须显式标注 label_source（如 synthetic / human）")
        out.append({"id": cid, "message": msg, "expect": dict(c.get("expect") or {}),
                    "split": split, "label_source": src})
    return out


def split_cases(cases: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按 label 拆成 (calibration, frozen)；两侧都不得为空（否则无法做阈值标定）。"""
    calib = [dict(c) for c in cases if str(c.get("split")) == CALIBRATION]
    frozen = [dict(c) for c in cases if str(c.get("split")) == FROZEN]
    if not calib:
        raise ValueError("校准集为空：阈值必须在 calibration 集上选定")
    if not frozen:
        raise ValueError("冻结集为空：没有未见样本就无法评估泛化")
    return calib, frozen


def _payload(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """只把 id/message/expect 交给 e2e_runner（它只认这三个字段）。"""
    return [{"id": c["id"], "message": c["message"], "expect": dict(c.get("expect") or {})}
            for c in rows]


def _escalation_probes(cases: Sequence[Mapping[str, Any]],
                       report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """误升级探针：期望"不升级"却被判升级的 case（S-01 要求记录误升级率）。"""
    trace_by_id = {str(t.get("id")): t for t in (report.get("cases") or [])}
    out: List[Dict[str, Any]] = []
    for c in cases:
        if c.get("expect", {}).get("escalated") is False:
            tr = trace_by_id.get(str(c["id"])) or {}
            out.append({
                "id": c["id"], "message": c["message"],
                "escalated": bool(tr.get("escalated")),
                "false_escalation": bool(tr.get("escalated")),
                "emergency_fastpath": bool(tr.get("emergency_fastpath")),
            })
    return out


async def calibrate(cases: Optional[Sequence[Mapping[str, Any]]] = None, *,
                    profile_name: Optional[str] = None,
                    chat_fn=None,
                    report_path: Optional[str] = None,
                    gate_path: Optional[str] = None) -> Dict[str, Any]:
    """按纪律执行一次校准 + 冻结评估，返回报告字典（可落盘）。

    顺序固定：先在校准集上选阈值 → 再用该阈值评冻结集（绝不用冻结集结果回选阈值）。
    """
    rows = list(cases) if cases is not None else load_cases()
    calib, frozen = split_cases(rows)
    profile = profile_name or "default"
    logger.info("校准开始：calibration=%d / frozen=%d profile=%s",
                len(calib), len(frozen), profile)

    calib_report = await run_e2e_async(_payload(calib), chat_fn=chat_fn, profile=profile)
    chosen = select_thresholds(make_eval_set("risk-calibration", CALIBRATION, calib_report),
                               profile_name=profile)

    frozen_report = await run_e2e_async(_payload(frozen), chat_fn=chat_fn, profile=profile)
    frozen_gate = evaluate_frozen(make_eval_set("risk-frozen", FROZEN, frozen_report),
                                  chosen, path=gate_path)

    label_sources = sorted({str(c.get("label_source")) for c in rows})
    report: Dict[str, Any] = {
        "generated_ts": datetime.now().isoformat(timespec="seconds"),
        "profile": chosen.get("profile"),
        "profile_version": chosen.get("profile_version"),
        "thresholds": dict(chosen.get("thresholds") or {}),
        "threshold_selected_on": dict(chosen.get("selected_on") or {}),
        "calibration": {
            "set": "risk-calibration", "kind": CALIBRATION, "n": len(calib),
            "pass_rate": calib_report.get("pass_rate"),
            "metrics": dict(calib_report.get("metrics") or {}),
            "confusion": per_class_summary(calib_report),
            "misses": list(calib_report.get("misses") or []),
            "red_failures": list(calib_report.get("red_failures") or []),
        },
        "frozen": {
            "set": "risk-frozen", "kind": FROZEN, "n": len(frozen),
            "pass_rate": frozen_report.get("pass_rate"),
            "metrics": dict(frozen_report.get("metrics") or {}),
            "confusion": per_class_summary(frozen_report),
            "misses": list(frozen_report.get("misses") or []),
            "red_failures": list(frozen_report.get("red_failures") or []),
            "latency": dict(frozen_report.get("latency") or {}),
        },
        "gate": {
            "blocked": list(frozen_gate.blocked),
            "passed": bool(frozen_gate.passed),
            "red_failures": list((frozen_gate.evidence or {}).get("red_failures") or []),
            "provenance": dict(frozen_gate.provenance or {}),
        },
        "coverage_precision_curve": coverage_precision_curve(frozen_report),
        "false_escalation_probes": _escalation_probes(rows, frozen_report),
        "label_sources": label_sources,
        "disclaimer": DISCLAIMER,
    }
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info("校准报告已写入 %s", report_path)
    return report
