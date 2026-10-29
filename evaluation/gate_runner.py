# 门禁接线（纯 stdlib / 死 LLM 可跑）：evaluation/gate_runner.py
# 把分层漏斗的归因结果（macro_distribution 的 RED 失败计数）喂给 Release Gate 的
# red_failures 参数，形成"M2 归因 → M4 门禁"的代码级闭环（此前仅有文档约定，无实际调用链）。
from typing import Any, Dict, Optional, Sequence

from evaluation.attribution import macro_distribution
from evaluation.release_gate import GateRecord, evaluate_gate, save_gate_record


def red_failures_from(verdicts: Sequence[Any]) -> int:
    """由一批 RubricVerdict 的归因结果取 RED 层失败数（无 RED 违规返回 0）。"""
    macro = macro_distribution(verdicts)
    return int(macro.get("RED", {}).get("fail", 0))


def run_gate_from_verdicts(
    verdicts: Sequence[Any],
    metrics: Dict[str, float],
    thresholds: Dict[str, Any],
    *,
    path: Optional[str] = None,
) -> GateRecord:
    """按漏斗 RED 失败数评估门禁：任一 RED 违规即强制阻断。

    metrics/thresholds 为通用指标口径（形如 {"intent_acc": 0.95}, {"intent_acc": {"op": "gte", "value": 0.90}}）；
    path 非空则把判定记录追加落盘（JSONL）。
    """
    rec = evaluate_gate(metrics, thresholds, red_failures=red_failures_from(verdicts))
    if path:
        save_gate_record(rec, path)
    return rec
