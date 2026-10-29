# 失败归因模块（纯 stdlib / 死 LLM 可跑）：evaluation/attribution.py
# 亮点：把 RubricVerdict 汇总成"层×状态"的宏观分布，并提供可复跑的正则计数，
#       用于定位失败集中在哪一层、以及修复前后某类问题的收敛判断。
import re
from typing import Dict, Sequence

from evaluation.rubric import RubricVerdict


def macro_distribution(verdicts: Sequence[RubricVerdict]) -> Dict[str, Dict[str, int]]:
    """按层×状态汇总：key 用子项前缀（domain 前缀），value 为 {pass/fail/na: 计数}。

    口径：串行层状态取自各 LayerResult.status；RED 层单独计入 "RED"。
    同一前缀出现多条 verdict 时累加（同一 verdict 内同层只计一次）。
    """
    out: Dict[str, Dict[str, int]] = {}

    def bump(prefix: str, status: str) -> None:
        bucket = out.setdefault(prefix, {"pass": 0, "fail": 0, "na": 0})
        bucket[status] = bucket.get(status, 0) + 1

    for verdict in verdicts:
        prefix = _verdict_prefix(verdict)
        for layer in verdict.layers:
            bump(prefix, layer.status)
        # RED 恒评，固定归入 RED（不随 domain 前缀）
        bump("RED", verdict.red.status)
    return out


def micro_rule_scan(texts: Sequence[str], pattern: str) -> int:
    """可复跑正则检测：返回 texts 中命中 pattern 的条数。"""
    rx = re.compile(pattern, re.IGNORECASE)
    return sum(1 for t in texts if t and rx.search(str(t)))


def before_after_scan(pattern: str, before: Sequence[str], after: Sequence[str]) -> Dict[str, int]:
    """修复前后同类问题命中数对比，delta = before - after（>0 表示收敛）。"""
    b = micro_rule_scan(before, pattern)
    a = micro_rule_scan(after, pattern)
    return {"before": b, "after": a, "delta": b - a}


def _verdict_prefix(verdict: RubricVerdict) -> str:
    """从 verdict 的子项码反推 domain 前缀（如 "PERMIT-CTX-EVID" → "PERMIT"）。"""
    for layer in verdict.layers:
        for code in layer.subitems:
            prefix, _, _ = code.partition("-")
            return prefix or "GEN"
    for code in verdict.red.subitems:
        prefix, _, _ = code.partition("-")
        # RED 子项码固定 GEN 前缀；回退时用 GEN，不影响 RED 桶（RED 另计）
        return prefix or "GEN"
    return "GEN"
