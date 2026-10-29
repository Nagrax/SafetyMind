"""
亮点：CI 反推样本量

核心问题：一个评测集要跑多少条样本，才能让准确率的置信区间足够窄？

本模块的答案：
  1. required_sample_size —— 用正态近似由"期望半宽"反推所需样本量，
     n = z^2 * p(1-p) / half_width^2（half_width 为比例，非百分点）。
  2. ci_half_width —— 由已知样本量正推置信区间的半宽（百分点）。

零第三方依赖，纯 stdlib；z 值由置信度经 NormalDist 反算，支持任意置信度。
"""
import math
import statistics


def _z_value(confidence: float) -> float:
    """按置信度取双侧正态分位 z 值（std lib，无第三方依赖）。"""
    common = {
        0.80: 1.2816,
        0.90: 1.6449,
        0.95: 1.9600,
        0.99: 2.5758,
    }
    if confidence in common:
        return common[confidence]
    return statistics.NormalDist().inv_cdf((1.0 + confidence) / 2.0)


def required_sample_size(
    p: float,
    half_width_pp: float,
    *,
    confidence: float = 0.95,
) -> int:
    """
    正态近似下，估计比例 p 达到 ±half_width_pp 个百分点半宽所需的样本量。

    half_width_pp 是"±多少个百分点"（例如 1 表示 ±1pp），
    n = ceil( z^2 * p(1-p) / (half_width_pp/100)^2 )。
    """
    if half_width_pp <= 0:
        raise ValueError("half_width_pp 必须为正的百分点数")
    p = min(max(float(p), 0.0), 1.0)
    z = _z_value(confidence)
    half = half_width_pp / 100.0
    n = (z * z) * p * (1.0 - p) / (half * half)
    return int(math.ceil(n))


def ci_half_width(
    p: float,
    n: int,
    *,
    confidence: float = 0.95,
) -> float:
    """
    由比例 p 与样本量 n 正推置信区间半宽，单位为百分点（±x pp）。

    half_width = z * sqrt(p(1-p)/n) * 100
    """
    if n <= 0:
        return float("inf")
    p = min(max(float(p), 0.0), 1.0)
    z = _z_value(confidence)
    return z * math.sqrt(p * (1.0 - p) / n) * 100.0
