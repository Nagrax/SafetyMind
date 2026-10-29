"""
亮点：评测 Release Gate + 指标时序漂移检测

核心问题：如何在端到端评测之后设置一道"发布门禁"，并用统计方法发现指标漂移？

本模块的答案：
  1. Release Gate —— 用声明式阈值（gte / lte）判定一组评测指标是否达标，
     任一指标不达标即 blocked；RED 用例失败（red_failures>0）强制阻断。
  2. 门禁历史 —— 每次判定结果追加落盘为 JSONL，便于回溯与复盘。
  3. 时序漂移 —— 沿用仓库既有的滑动窗口 Z-score 口径，
     用最新点之前的窗口建立基线，判断最新点是否发生漂移。

零第三方依赖，纯 stdlib，可在无 LLM 环境下直接运行。
"""
__all__ = [
    "DEFAULT_GATE_PATH",
    "GateRecord",
    "EvalSet",
    "VALID_SET_KINDS",
    "evaluate_gate",
    "save_gate_record",
    "detect_drift",
    "build_chosen",
    "make_eval_set",
    "select_thresholds",
    "evaluate_report_gate",
    "evaluate_frozen",
    "promote_baseline",
    "per_class_summary",
    "coverage_precision_curve",
]

import json
import logging
import math
import os
import statistics
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from evaluation.profiles import (
    DeployProfile,
    profile_gate_thresholds,
    resolve_profile,
)

logger = logging.getLogger(__name__)

DEFAULT_GATE_PATH = "data/eval/gate_history.jsonl"


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class GateRecord:
    ts: str
    metrics: Dict[str, float]
    thresholds: Dict[str, Any]
    passed: bool
    blocked: List[str] = field(default_factory=list)
    # ── 以下两项为 additive 扩展（默认空 = 旧行为，既有调用方逐字节兼容）──
    evidence: Dict[str, Any] = field(default_factory=dict)      # 判决依据（RED 证据/分数）
    provenance: Dict[str, Any] = field(default_factory=dict)    # 阈值来源（profile/校准集）


# 评测集类别：阈值只允许在校准集上选；调参集可看不可当校准用；冻结集只评估不调参
CALIBRATION = "calibration"
TUNING = "tuning"
FROZEN = "frozen"
VALID_SET_KINDS = (CALIBRATION, TUNING, FROZEN)


@dataclass(frozen=True)
class EvalSet:
    """带类别的评测集：把"数据集纪律"落成数据结构，而不是口头约定。"""
    name: str
    kind: str
    report: Mapping[str, Any]


# ── 发布门禁 ──────────────────────────────────────────────────────────────────

def evaluate_gate(
    metrics: Dict[str, float],
    thresholds: Dict[str, Any],
    *,
    red_failures: int = 0,
) -> GateRecord:
    """
    按 thresholds 逐项校验 metrics，返回 GateRecord。

    thresholds 每项形如 {"op": "gte"/"lte", "value": x}：
      - gte：metric >= value 视为达标
      - lte：metric <= value 视为达标
    任一不达标 → passed=False，blocked 列出对应指标名；
    red_failures>0 时强制 passed=False 且 blocked 含 "RED"。
    """
    blocked: List[str] = []

    for name, rule in thresholds.items():
        value = metrics.get(name)
        if value is None:
            blocked.append(name)          # 指标缺失同样视为不达标
            continue
        op = str(rule.get("op", "gte")).lower()
        limit = float(rule["value"])
        if op == "gte":
            ok = value >= limit
        elif op == "lte":
            ok = value <= limit
        else:
            ok = False   # 未知 op：契约外输入，fail-safe 判不达标，绝不静默按 lte 处理
        if not ok:
            blocked.append(name)

    if red_failures > 0:
        blocked.append("RED")

    return GateRecord(
        ts=datetime.now().isoformat(),
        metrics=dict(metrics),
        thresholds=dict(thresholds),
        passed=not blocked,
        blocked=blocked,
    )


def save_gate_record(rec: GateRecord, path: str = DEFAULT_GATE_PATH) -> str:
    """
    以追加式 JSONL 落盘一条 GateRecord（原子写：tmp + os.replace）。

    采用"读旧内容 + 追加新行 + 整体原子替换"，保证并发/崩溃下文件不残缺。
    返回写入路径。
    """
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    line = json.dumps(asdict(rec), ensure_ascii=False) + "\n"
    old = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()

    fd, tmp = tempfile.mkstemp(dir=parent or ".", prefix=".gate_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(old)
            f.write(line)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


# ── 时序漂移检测 ──────────────────────────────────────────────────────────────

def detect_drift(
    series: Sequence[float],
    *,
    window: int = 20,
    sensitivity: float = 2.5,
) -> Dict[str, Any]:
    """
    用滑动窗口 Z-score 判断最新点是否漂移。

    基线取最新点之前的 window 个点（不含最新点本身，避免尖峰稀释自身），
    z = |最新值 - 基线均值| / 基线标准差；z > sensitivity 判定漂移。
    数据不足（len < window + 1）或基线标准差为 0 时不判定漂移。
    """
    vals = [float(x) for x in series]
    empty = {"drift": False, "z_score": 0.0, "mean": 0.0}

    if len(vals) < window + 1:
        if not vals:
            return dict(empty)
        return {"drift": False, "z_score": 0.0, "mean": statistics.mean(vals)}

    latest = vals[-1]
    baseline = vals[-(window + 1):-1]
    mean = statistics.mean(baseline)
    stdev = statistics.stdev(baseline) if len(baseline) > 1 else 0.0

    if stdev == 0:
        return {"drift": False, "z_score": 0.0, "mean": mean}

    z = abs(latest - mean) / stdev
    return {
        "drift": bool(z > sensitivity),
        "z_score": round(z, 4),
        "mean": mean,
    }


# ── 风险分层发布判定（S-06）───────────────────────────────────────────────────
#
# 两层阈值彼此解耦：
#   ① 常规指标（pass_rate / p95_ms / citation_coverage）逐项按部署 profile 的 op/value 判定；
#   ② RED 安全红线：red_failures > red_max（本项目 red_max 恒 0）**无条件**阻断。
# 判定式显式写成 blocked = (red_failures > 0) or (任一指标不达标) or (指标缺失)，
# 因此"LLM 文本高分"这类均分型指标不可能覆盖 RED 违规。

def build_chosen(
    profile: DeployProfile,
    *,
    selected_on: Optional[Mapping[str, Any]] = None,
    observed: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """把部署 profile 组装成"已选定阈值"记录（含来源与版本，便于复算与追溯）。

    selected_on 记录阈值是在哪一份数据集上选出来的：只有 kind == "calibration" 的记录
    才被 evaluate_frozen 接受（冻结集不得回选阈值）。
    """
    return {
        "profile": profile.name,
        "profile_version": profile.version,
        "model_mode": profile.model_mode,
        "fallback": bool(profile.fallback),
        "thresholds": profile_gate_thresholds(profile),
        "red_max": profile.red_max(),
        "selected_on": dict(selected_on or {"set": "", "kind": ""}),
        "observed": dict(observed or {}),
    }


def _normalize_chosen(chosen: Any) -> Dict[str, Any]:
    """chosen 允许是 DeployProfile（未校准，来源记为未标定）或已选定的阈值记录。"""
    if isinstance(chosen, DeployProfile):
        return build_chosen(chosen)
    if isinstance(chosen, Mapping) and "thresholds" in chosen:
        return dict(chosen)
    raise TypeError("chosen 必须是 DeployProfile 或 select_thresholds/build_chosen 的返回记录")


def make_eval_set(name: str, kind: str, report: Mapping[str, Any]) -> EvalSet:
    """构造带类别的评测集；未知类别直接报错（不静默当成校准集）。"""
    if kind not in VALID_SET_KINDS:
        raise ValueError(f"未知评测集类别 {kind!r}，只允许 {VALID_SET_KINDS}")
    if not isinstance(report, Mapping):
        raise TypeError("report 必须是 run_e2e 产出的映射报告")
    return EvalSet(name=str(name), kind=str(kind), report=report)


def select_thresholds(calib_set: EvalSet,
                      profiles: Optional[Mapping[str, DeployProfile]] = None,
                      *,
                      profile_name: Optional[str] = None) -> Dict[str, Any]:
    """**只在校准集上**选定阈值：调参集/冻结集一律拒绝。

    返回已选定阈值记录（含 profile 名、版本、模型形态、阈值表、校准集来源、校准集观测量）。
    """
    if not isinstance(calib_set, EvalSet):
        raise TypeError("select_thresholds 只接受 EvalSet（带类别）")
    if calib_set.kind != CALIBRATION:
        raise ValueError(
            f"阈值只能在校准集上选择：收到 {calib_set.kind} 集（{calib_set.name}），"
            f"冻结/调参集只评估不调参")
    profile = resolve_profile(profile_name, profiles)
    metrics = dict(calib_set.report.get("metrics") or {})
    observed = {k: metrics.get(k) for k in ("pass_rate", "p95_ms", "citation_coverage")}
    return build_chosen(
        profile,
        selected_on={"set": calib_set.name, "kind": calib_set.kind,
                     "n": int(len(calib_set.report.get("cases") or []))},
        observed=observed,
    )


def evaluate_report_gate(report: Mapping[str, Any],
                         chosen: Any = None,
                         *,
                         path: Optional[str] = None) -> GateRecord:
    """按报告 + 已选定阈值做发布判定。

    判定式：blocked = (red_failures > red_max) or (任一指标不达标) or (指标缺失)。
    RED 违规无条件计入 blocked，不会被"总均分/文本分"这类均分指标覆盖——judge 分
    只作为证据记录，不参与是否阻断的判定。chosen 为空时回退默认 profile
    （配置缺失时为保守默认）。
    """
    chosen_rec = _normalize_chosen(chosen if chosen is not None else resolve_profile(None))
    metrics = dict(report.get("metrics") or {})
    red_failures = list(report.get("red_failures") or [])
    red_max = int(chosen_rec.get("red_max", 0) or 0)

    rec = evaluate_gate(metrics, chosen_rec["thresholds"],
                        red_failures=max(0, len(red_failures) - red_max))
    rec.evidence = {
        "red_failures": red_failures,
        "red_evidence": list(report.get("red_evidence") or []),
        "judge_avg": report.get("judge_avg"),
        "note": "RED 违规无条件阻断：均分/文本分不覆盖安全红线",
    }
    rec.provenance = {
        "profile": chosen_rec.get("profile"),
        "profile_version": chosen_rec.get("profile_version"),
        "selected_on": chosen_rec.get("selected_on"),
        "observed": chosen_rec.get("observed"),
    }
    if path:
        save_gate_record(rec, path)
    return rec


def evaluate_frozen(frozen_set: EvalSet,
                    chosen: Mapping[str, Any],
                    *,
                    path: Optional[str] = None) -> GateRecord:
    """在冻结测试集上只评估（用校准集选定的阈值），拒绝用冻结集回选阈值。"""
    if not isinstance(frozen_set, EvalSet):
        raise TypeError("evaluate_frozen 只接受 EvalSet（带类别）")
    if frozen_set.kind != FROZEN:
        raise ValueError(f"evaluate_frozen 只接受冻结测试集：收到 {frozen_set.kind} 集"
                         f"（{frozen_set.name}）")
    chosen_rec = _normalize_chosen(chosen)
    if chosen_rec.get("selected_on", {}).get("kind") != CALIBRATION:
        raise ValueError(
            "拒绝用冻结/调参集回选阈值：chosen.selected_on.kind 必须是 'calibration'"
            f"（当前 {chosen_rec.get('selected_on')!r}）")
    rec = evaluate_report_gate(frozen_set.report, chosen_rec, path=path)
    rec.provenance["evaluated_on"] = {"set": frozen_set.name, "kind": frozen_set.kind}
    return rec


def promote_baseline(report: Mapping[str, Any],
                     *,
                     baseline_path: str,
                     promote: bool = False,
                     gate: Optional[GateRecord] = None,
                     previous_ts: Optional[str] = None) -> Dict[str, Any]:
    """评审基线晋升：**只有显式 promote=True 且无 RED 违规且门禁通过时才写入**。

    否则保持旧基线不动（绝不覆盖），并如实报告未晋升原因。
    """
    red_failures = list(report.get("red_failures") or [])
    out: Dict[str, Any] = {
        "promoted": False,
        "baseline_path": str(baseline_path),
        "previous_ts": previous_ts,
        "new_ts": None,
        "reason": "",
    }
    if not promote:
        out["reason"] = "未显式 promote=True：失败/未评审运行不得自动晋升为新基线"
        return out
    if red_failures:
        out["reason"] = (f"存在 RED 违规 {red_failures}：禁止晋升"
                         f"（高均分/高通过率不覆盖安全红线）")
        return out
    if gate is not None and not gate.passed:
        out["reason"] = f"发布门禁未通过（blocked={list(gate.blocked)}）：失败运行不得晋升"
        return out

    payload = {
        "timestamp": str(report.get("timestamp") or datetime.now().isoformat()),
        "pass_rate": (report.get("metrics") or {}).get("pass_rate"),
        "metrics": dict(report.get("metrics") or {}),
        "red_failures": red_failures,
        "profile": report.get("profile"),
        "promoted_from": "e2e-run",
        "gate_provenance": dict(gate.provenance) if gate is not None else {},
    }
    path = str(baseline_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or ".", prefix=".baseline_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    out.update(promoted=True, new_ts=payload["timestamp"],
               reason="门禁通过且无 RED 违规，显式晋升")
    return out


# ── 报告结构（每类混淆 / 关键漏检 / 覆盖率-精度曲线）────────────────────────────

def per_class_summary(report: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """按类（期望意图优先，其次实际意图）汇总 total/passed/failed/pass_rate 与漏检 case id。"""
    out: Dict[str, Dict[str, Any]] = {}
    for case in report.get("cases") or []:
        cls = str(case.get("expect_intent") or case.get("intent") or "other")
        bucket = out.setdefault(
            cls, {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0.0, "misses": []})
        bucket["total"] += 1
        if case.get("passed"):
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
            bucket["misses"].append(str(case.get("id", "")))
    for bucket in out.values():
        bucket["pass_rate"] = (bucket["passed"] / bucket["total"]) if bucket["total"] else 0.0
    return out


def _risk_score(case: Mapping[str, Any]) -> float:
    """单条 case 的风险分（越小越安全）：RED 违规固定 1.0；否则按 judge 失分；无 judge 时按断言结果。"""
    if (case.get("red") or {}).get("status") == "fail":
        return 1.0
    judge = case.get("judge")
    if judge is not None:
        try:
            return max(0.0, min(1.0, 1.0 - float(judge)))
        except (TypeError, ValueError):
            return 0.0
    return 0.5 if not case.get("passed") else 0.0


def coverage_precision_curve(report: Mapping[str, Any],
                             *,
                             steps: Optional[Sequence[float]] = None) -> List[Dict[str, Any]]:
    """按风险分阈值扫描的覆盖率/精度曲线（阈值档由高到低）。

    每个档位给出 flagged（被标记条数）与 tp/fp/fn，并派生 precision/coverage：
    coverage = tp / 全部失败条数；precision = tp / flagged。
    """
    cases = list(report.get("cases") or [])
    scores = [_risk_score(c) for c in cases]
    total_fail = sum(1 for c in cases if not c.get("passed"))
    grid = list(steps) if steps else sorted(set(scores), reverse=True)

    curve: List[Dict[str, Any]] = []
    for threshold in grid:
        flagged = [c for c, s in zip(cases, scores) if s >= threshold]
        tp = sum(1 for c in flagged if not c.get("passed"))
        fp = len(flagged) - tp
        fn = total_fail - tp
        curve.append({
            "threshold": threshold,
            "flagged": len(flagged),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(tp / len(flagged), 4) if flagged else 0.0,
            "coverage": round(tp / total_fail, 4) if total_fail else 0.0,
        })
    return curve

