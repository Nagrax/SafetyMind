"""
亮点：评测输入清洗模块（纯 stdlib，无 LLM，可在"死 LLM"环境测试）

核心问题：端到端评测把用户输入（含多轮历史、工具返回、并行 Agent 输出）
直接送进 LLM-as-Judge 或意图识别器时，夹杂的框架内部标记会被 judge 误读：
  - 工具调用 JSON / fenced block 会被当成"用户真的在问工具调用"
  - 工具接口内部提示词 block（[system]…[/system] 等）会被当成业务内容
  - 并行 Agent 的输出标签块会被当成答案主体
  - 超长输入若被静默丢弃，评测会得到"空结果"假象，掩盖真实退化

本模块提供确定性的清洗与归一化：
  strip_noise      剔除干扰片段并计数
  precheck         长度预检：超长截断并显式标记，绝不静默返回空结果
  estimate_tokens  粗略估算 token 量（用于 prompt/预算控制）
  align_semantics  用户口语 → 系统枚举类别的确定性对齐（无 LLM）
  filter_samples   按字段精确过滤评测样本

全部为纯函数 / 无副作用。
"""
import math
import os
import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Sequence, Tuple

# 单条输入进入 judge/意图识别前允许的最大字符数（可用环境变量覆盖）
MAX_INPUT_CHARS = int(os.getenv("SAFETYMIND_EVAL_MAX_INPUT_CHARS", "8000"))


# ── token 估算 ────────────────────────────────────────────────────────────────

_CJK_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x3400, 0x4DBF),   # 汉字扩展 A
    (0x4E00, 0x9FFF),   # 汉字基本区
    (0x3040, 0x30FF),   # 日文假名
    (0xF900, 0xFAFF),   # 兼容汉字
    (0xFF00, 0xFFEF),   # 全角字符
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 约 1.5 字/token，其余约 4 字符/token。"""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return int(math.ceil(cjk / 1.5 + other / 4.0))


# ── 干扰信号正则（可用 SAFETYMIND_EVAL_NOISE_PATTERNS 追加，按行分隔）──────────

_NOISE_PATTERNS_BASE: List[Tuple[str, "re.Pattern[str]"]] = [
    # ② 工具接口内部提示词 block
    ("system_bracket",  re.compile(r"\[system\][\s\S]*?\[/system\]", re.DOTALL)),
    ("system_angle",    re.compile(r"<system>[\s\S]*?</system>", re.DOTALL)),
    ("internal_prompt", re.compile(r"内部提示词[:：]?[^\n]*")),
    # ① 工具调用 fenced block（```tool_calls … ```）
    ("tool_fence",      re.compile(r"```[ \t]*tool[ _]?calls[ \t]*\n[\s\S]*?```", re.DOTALL)),
    # ① 工具调用内联 JSON（支持一层嵌套对象，如 "tool_calls": [{"name": "x"}]）
    ("tool_calls_json", re.compile(
        r'\{(?:[^{}]|\{[^{}]*\})*"tool_calls"(?:[^{}]|\{[^{}]*\})*\}', re.DOTALL)),
    # ③ 并行 Agent 输出标签块，形如 [equipment - 主处理]
    ("parallel_tag",    re.compile(r"\[[^\[\]]+?\s*-\s*[^\[\]]+?\]")),
]


def _load_extra_noise_patterns() -> List[Tuple[str, "re.Pattern[str]"]]:
    """从环境变量解析追加的干扰正则；非法正则静默忽略，不阻塞清洗主流程。"""
    raw = os.getenv("SAFETYMIND_EVAL_NOISE_PATTERNS", "")
    extra: List[Tuple[str, "re.Pattern[str]"]] = []
    for idx, line in enumerate(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            extra.append((f"env_{idx}", re.compile(line, re.DOTALL)))
        except re.error:
            continue
    return extra


_NOISE_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = (
    _NOISE_PATTERNS_BASE + _load_extra_noise_patterns()
)


def strip_noise(text: str) -> Tuple[str, int]:
    """剔除会被 judge 误读的干扰信号，返回 (清洗后文本, 剔除的片段数)。"""
    if not text:
        return "", 0
    if not isinstance(text, str):
        text = str(text)
    out = text
    removed = 0
    for _name, pat in _NOISE_PATTERNS:
        out, n = pat.subn("", out)
        removed += n
    if removed:
        # 折叠剔除产生的多余空行，避免留下空洞分隔
        out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip(), removed


# ── 长度预检 ──────────────────────────────────────────────────────────────────

@dataclass
class PreparedInput:
    text: str
    original_chars: int
    truncated: bool
    noise_removed: int
    notes: List[str]


def precheck(text: str, max_chars: int = MAX_INPUT_CHARS) -> PreparedInput:
    """
    长度预检：剔除噪声、超长截断并显式标记。

    核心目的：超长输入绝不静默返回空结果 —— 即使被截断也会在文末留下
    "[已截断，原长 N 字符]" 标记，保证下游评测不会把"丢输入"误读成"空回答"。
    空串 / None / 乱编码（\\udcff 之类代理字符）一律不崩溃，给出 notes。
    """
    notes: List[str] = []

    if text is None:
        return PreparedInput("", 0, False, 0, ["空输入（None）：无内容可清洗"])

    if not isinstance(text, str):
        text = str(text)
        notes.append("输入非字符串，已按 str() 转换")

    # 剔除非法代理字符（乱编码），避免后续 encode / json 失败
    sanitized = text.encode("utf-8", errors="ignore").decode("utf-8")
    if len(sanitized) != len(text):
        notes.append(f"已剔除 {len(text) - len(sanitized)} 个非法代理字符/乱码")

    original_chars = len(sanitized)
    if not sanitized:
        notes.append("空输入：无内容可清洗")
        return PreparedInput("", 0, False, 0, notes)

    cleaned, noise_removed = strip_noise(sanitized)
    if noise_removed:
        notes.append(f"已剔除 {noise_removed} 处干扰片段（工具调用/内部提示词/并行标签）")

    # 原始非空但清洗后为空（如整条均为干扰片段）：显式留标，绝不静默返回空结果
    if not cleaned:
        notes.append(f"清洗后无有效内容：原始 {original_chars} 字符全部为干扰片段")
        cleaned = f"[清洗后无有效内容，原长 {original_chars} 字符]"

    truncated = False
    if max_chars is not None and max_chars >= 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + f"\n[已截断，原长 {original_chars} 字符]"
        truncated = True
        notes.append(f"输入超长已截断：保留前 {max_chars} 字符，原长 {original_chars} 字符")

    return PreparedInput(cleaned, original_chars, truncated, noise_removed, notes)


# ── 语义对齐（确定性，无 LLM）────────────────────────────────────────────────

def align_semantics(
    text: str,
    categories: Sequence[str],
    aliases: Mapping[str, str] | None = None,
) -> str:
    """
    把用户口语确定性映射到系统枚举类别。

    用 aliases（口语 → 类别）做最长匹配替换：按别名长度降序依次替换，
    命中即改写，找不到任何别名时原样返回；目标类别不在 categories 中的
    别名会被跳过，保证对齐结果落在系统枚举内。
    """
    if not text:
        return "" if text is None else text
    if not aliases:
        return text

    cat_set = {c for c in categories} if categories else set()
    ordered = sorted(aliases.items(), key=lambda kv: len(kv[0]), reverse=True)

    out = text
    for alias, target in ordered:
        if not alias:
            continue
        if cat_set and target not in cat_set:
            continue
        out = out.replace(alias, target)
    return out


# ── 样本过滤 ──────────────────────────────────────────────────────────────────

def filter_samples(
    samples: Sequence[Mapping[str, Any]],
    *,
    intent: str | None = None,
    key: str = "intent",
) -> List[Mapping[str, Any]]:
    """按字段精确过滤样本，返回新列表（浅拷贝列表，不改动原对象）。"""
    if not samples:
        return []
    if intent is None:
        return [s for s in samples]
    return [s for s in samples if s is not None and s.get(key) == intent]
