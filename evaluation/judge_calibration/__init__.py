# -*- coding: utf-8 -*-
"""LLM Judge 可信度校准与偏差治理

背景：LLMJudge（见 evaluation/evaluator.py）用 LLM 给 Agent 响应打分，
可规模化但自身也带偏差——位置偏差、冗长偏差、结构化输出不稳定。
本模块提供一组**纯函数**度量与校正工具，用来给 judge 做可信度校准：

  1. cohens_kappa      —— 两个评分类序列的一致性（Cohen's Kappa）
  2. golden_kappa      —— 程序化判定 vs LLM judge 的一致率（非人标口径）
  3. swap_consistency  —— 交换选项顺序后的翻转率（位置偏差）
  4. length_controlled —— 按长度残差回归后的通过率（冗长偏差校正）
  5. validate_structured —— 结构化输出三层兜底（抽取/校验/归一化）

设计约束：零第三方依赖，纯 stdlib；所有"需要评判器"的地方都接受注入的
Callable，测试可传确定性 stub，无需真实 LLM（dead-LLM 可跑）。
"""
import json
import statistics
from collections import Counter
from typing import Any, Callable, Dict, Sequence

__all__ = [
    "cohens_kappa",
    "golden_kappa",
    "swap_consistency",
    "length_controlled",
    "validate_structured",
    "PASS_THRESHOLD",
    "REQUIRED_DIMENSIONS",
]

# 校准内部的"连续分→二值"阈值，仅用于本模块的通过率统计（length_controlled 等）；
# 与主链路 evaluator.EndToEndEvaluator.PASS_THRESHOLD=0.75 是不同用途，二者无需一致。
PASS_THRESHOLD = 0.5

# 结构化输出要求的四个评分维度
REQUIRED_DIMENSIONS = ("relevance", "accuracy", "completeness", "helpfulness")


# ── 1. Cohen's Kappa ─────────────────────────────────────────────────────────

def cohens_kappa(a: Sequence[int], b: Sequence[int]) -> float:
    """两个等长评分类序列的 Cohen's Kappa。

    kappa = (po - pe) / (1 - pe)
      po: 观测一致率；pe: 随机期望一致率（按边际分布估计）
    - po == pe（观测与期望一致，信息量为 0）
    - pe >= 1（退化，1 - pe 为分母 0）
    - 样本不足（有效配对数 < 2）或两序列长度不等
    以上情形一律返回 0.0，绝不抛异常。
    """
    try:
        if len(a) != len(b):
            return 0.0
        pairs = list(zip(a, b))
    except TypeError:
        return 0.0
    n = len(pairs)
    if n < 2:
        return 0.0

    agree = sum(1 for x, y in pairs if x == y)
    po = agree / n

    a_counts = Counter(x for x, _ in pairs)
    b_counts = Counter(y for _, y in pairs)
    pe = sum((a_counts[c] / n) * (b_counts[c] / n) for c in set(a_counts) | set(b_counts))

    if pe >= 1.0 - 1e-12 or abs(po - pe) < 1e-12:
        return 0.0
    return (po - pe) / (1.0 - pe)


# ── 2. 程序化判定 vs LLM Judge 的一致率（非人标口径）──────────────────────────

def golden_kappa(
    program_verdicts: Sequence[bool],
    judge_verdicts: Sequence[bool],
) -> Dict[str, float]:
    """程序化判定与 LLM judge 判定的一致率 + Kappa。

    口径：这里的"金标"是**程序化判定**（如规则/断言产出），不是人工标注；
    因此返回的是"程序化 vs judge"的吻合度，用来发现 judge 与硬规则的系统性背离。

    返回 {"kappa": x, "agreement": x, "n": int}
    """
    n = min(len(program_verdicts), len(judge_verdicts))
    if n == 0:
        return {"kappa": 0.0, "agreement": 0.0, "n": 0}

    prog = [1 if bool(x) else 0 for x in program_verdicts[:n]]
    judg = [1 if bool(x) else 0 for x in judge_verdicts[:n]]
    agreement = sum(1 for x, y in zip(prog, judg) if x == y) / n

    return {
        "kappa": cohens_kappa(prog, judg),
        "agreement": agreement,
        "n": n,
    }


# ── 3. 位置偏差：交换一致性 ──────────────────────────────────────────────────

def _normalize_verdict(value: Any) -> str:
    return str(value).strip().lower()


def swap_consistency(
    judge_fn: Callable[[str, str], str],
    pairs: Sequence[Sequence[str]],
) -> Dict[str, float]:
    """位置偏差检测：对每对 (a, b) 正反各评一次，统计结论翻转率。

    理想 judge 对 (a, b) 与 (b, a) 应给出等价结论；"总选第一个"这类
    位置偏向的 judge 会把结论翻转。judge_fn 抛异常也计为一次翻转
    （不可复现 = 不一致）。

    返回 {"flip_rate": x, "n": int}
    """
    n = len(pairs)
    if n == 0:
        return {"flip_rate": 0.0, "n": 0}

    flips = 0
    for pair in pairs:
        a = str(pair[0])
        b = str(pair[1])
        try:
            forward = judge_fn(a, b)
            backward = judge_fn(b, a)
        except Exception:
            flips += 1
            continue
        if _normalize_verdict(forward) != _normalize_verdict(backward):
            flips += 1

    return {"flip_rate": flips / n, "n": n}


# ── 4. 冗长偏差校正 ──────────────────────────────────────────────────────────

def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    vx = sum((x - mx) ** 2 for x in xs) / n
    vy = sum((y - my) ** 2 for y in ys) / n
    if vx <= 1e-12 or vy <= 1e-12:
        return 0.0
    return cov / (vx ** 0.5 * vy ** 0.5)


def length_controlled(
    scores: Sequence[float],
    lengths: Sequence[float],
) -> Dict[str, float]:
    """冗长偏差校正：去掉"回复越长分越高"的系统性加成后再算通过率。

    做法：对 (score, length) 做最小二乘线性回归，取残差加回均值得到
    校正分 corrected_i = score_i - beta * (length_i - mean_length)。
    若长度方差为 0，则 beta 不可估，校正分等同原分。

    返回 {"corr": x, "corrected_pass_rate": x, "raw_pass_rate": x}
      corr: score 与 length 的 Pearson 相关（正值 = 越长越易得高分）
    """
    n = min(len(scores), len(lengths))
    if n < 2:
        return {"corr": 0.0, "corrected_pass_rate": 0.0, "raw_pass_rate": 0.0}

    s = [float(x) for x in scores[:n]]
    ln = [float(x) for x in lengths[:n]]

    raw_pass_rate = sum(1 for x in s if x >= PASS_THRESHOLD) / n
    corr = _pearson(s, ln)

    var_l = statistics.pvariance(ln)
    if var_l <= 1e-12:
        corrected = list(s)
    else:
        mean_s = statistics.mean(s)
        mean_l = statistics.mean(ln)
        cov = sum((x - mean_s) * (y - mean_l) for x, y in zip(s, ln)) / n
        beta = cov / var_l
        corrected = [x - beta * (y - mean_l) for x, y in zip(s, ln)]

    corrected_pass_rate = sum(1 for x in corrected if x >= PASS_THRESHOLD) / n
    return {
        "corr": corr,
        "corrected_pass_rate": corrected_pass_rate,
        "raw_pass_rate": raw_pass_rate,
    }


# ── 5. 结构化输出三层兜底 ────────────────────────────────────────────────────

# 常见键名别名 → 规范维度名（标签归一化：键）
_KEY_ALIASES = {
    "relevancy": "relevance",
    "relevant": "relevance",
    "accuracy_score": "accuracy",
    "accurate": "accuracy",
    "completeness_score": "completeness",
    "complete": "completeness",
    "helpfulness_score": "helpfulness",
    "helpful": "helpfulness",
    "usefulness": "helpfulness",
    "utility": "helpfulness",
}

# 定性标签 → 数值（标签归一化：值）
_LABELS = {
    "excellent": 1.0, "high": 0.9, "good": 0.8,
    "medium": 0.6, "fair": 0.5, "low": 0.3, "poor": 0.2, "bad": 0.1,
    "优秀": 1.0, "高": 0.9, "好": 0.8, "中": 0.6, "低": 0.3, "差": 0.1,
}


def _fail(stage: str) -> Dict[str, Any]:
    return {"ok": False, "values": {}, "stage_failed": stage}


def _extract_json_object(raw: Any) -> Any:
    """从任意文本中抽取第一段平衡的 JSON 对象；失败返回 None。"""
    if not isinstance(raw, str) or not raw:
        return None
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(raw)):
        ch = raw[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = raw[start:i + 1]
                try:
                    return json.loads(candidate)
                except Exception:
                    return None
    return None


def _to_unit(value: Any):
    """把单个维度值归一化为 [0, 1] 浮点；失败返回 (False, 0.0)。"""
    if isinstance(value, bool):
        return False, 0.0
    if isinstance(value, (int, float)):
        f = float(value)
        return (True, f) if 0.0 <= f <= 1.0 else (False, 0.0)
    if isinstance(value, str):
        s = value.strip().lower()
        if s.endswith("%"):
            try:
                f = float(s[:-1]) / 100.0
            except Exception:
                return False, 0.0
            return (True, f) if 0.0 <= f <= 1.0 else (False, 0.0)
        if s in _LABELS:
            return True, _LABELS[s]
        try:
            f = float(s)
        except Exception:
            return False, 0.0
        return (True, f) if 0.0 <= f <= 1.0 else (False, 0.0)
    return False, 0.0


def validate_structured(raw: Any) -> Dict[str, Any]:
    """结构化 judge 输出的三层兜底校验。

    ① 抽取：从文本（含 ```json 围栏）中抽出 JSON 对象
    ② 校验：必须是对象且含四个维度（键名经别名归一化后比对）
    ③ 归一化：值统一转换为 0~1 浮点（数字/百分号/定性标签），越界即拦截

    返回 {"ok": bool, "values": Dict[str, float], "stage_failed": str}
    非标输出（坏 JSON / 缺字段 / 超范围）一律 ok=False 并给出失败阶段，
    绝不静默放行。
    """
    # ① 抽取
    data = _extract_json_object(raw)
    if not isinstance(data, dict):
        return _fail("extract")

    # ② schema 校验：四个维度必须齐全，且值类型可归一化
    mapped: Dict[str, Any] = {}
    for key, value in data.items():
        canonical = _KEY_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        if canonical in REQUIRED_DIMENSIONS and canonical not in mapped:
            mapped[canonical] = value
    if any(dim not in mapped for dim in REQUIRED_DIMENSIONS):
        return _fail("schema")
    for value in mapped.values():
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return _fail("schema")

    # ③ 标签归一化：值 → [0, 1]
    values: Dict[str, float] = {}
    for dim in REQUIRED_DIMENSIONS:
        ok, unit = _to_unit(mapped[dim])
        if not ok:
            return _fail("normalize")
        values[dim] = unit

    return {"ok": True, "values": values, "stage_failed": ""}
