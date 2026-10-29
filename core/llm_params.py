# -*- coding: utf-8 -*-
"""LLM 请求参数的统一构造：集中处理「思考块」控制。

## 为什么要这个模块（2026-10-25 闭环实测结论）

现象：真实端点（bigmodel / glm-4.6）下，常规问答只有 **0 字正文**，用户看到的是
「（注意：本次回答因达到长度上限被截断，未完整生成。）」。紧急问答正常（走零 LLM 模板）。

排查过程（每一步都有实测数据，见 scratch/sm-loop/runs/）：
  1. 先怀疑 max_tokens 太小 → 从 1024 提到 4096：**截断率不变**（1024/2048 都是 3/3，
     3072/4096 还有 2/3），thinking 长度等比增长（1024→2.7k、4096→11.3k 字符），
     正文始终 0 字。**假设被推翻**。
  2. 再怀疑 thinking 不受控 → 用原生 HTTP 绕过 SDK 直接发 thinking 参数，n=3 重复：
     - `thinking: {"type":"disabled"}` → **3/3 end_turn，正文 845/826/875 字，延迟 11-14s，思考 0 字**
     - `thinking: {"type":"enabled","budget_tokens":512}` → 3/3 有正文，但思考 1.3~2.8k，延迟 27-30s
     - 不发 thinking 参数（现状）→ 1/3 截断成空回答，其余思考 1.8~2.3k，延迟 28s
  结论：根因是**思考块不受控地吃掉输出预算**，不是预算大小问题；默认关闭思考即可，
  既拿到正文又把延迟砍掉 60%。

## 兼容性设计（关键）

- Anthropic 官方模型默认思考是关闭的，**不认识 `thinking` 参数也不会报错**（多余字段被忽略）。
- 但有些兼容端点会严格校验入参并 400。所以这里不是无脑加字段，而是：
  1. 默认按 `SAFETYMIND_THINKING` 环境变量决定（默认 `off`）；
  2. 只有开关为 on 或显式传 budget 时才带 `thinking`；
  3. 端点若因该字段报错，调用方捕获后重试一次不带该字段的请求（见 `without_thinking`）。
- anthropic==0.40.0 的 `messages.create` 签名里没有 `thinking`，但**有 `extra_body`**，
  实测用它传可以被服务端接受（原生 HTTP 同参数 200）。

## 环境变量

- `SAFETYMIND_THINKING`：`off`（默认）/ `on` / `budget:<n>` / `omit`
  - `off`（默认）→ 显式发 `{"type":"disabled"}`。**这是实测唯一能解决截断的取值**：
    该端点默认开启思考，仅"不传该字段"并不能关闭（n=3 仍 1/3 截断）。
  - `on`      → `{"type":"enabled"}`
  - `budget:512` → `{"type":"enabled","budget_tokens":512}`
  - `omit`    → 完全不发该字段（给会严格校验入参的端点用的最后手段；此时思考行为回到端点默认）

端点若不接受该字段，`ToolExecutor._create` 会自动去掉重试一次，不会让对话失败。
"""
import os
from typing import Any, Dict, Optional

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def thinking_param() -> Optional[Dict[str, Any]]:
    """返回应传给 Anthropic messages.create 的 thinking 字段；None 表示不带该字段。

    默认（off）返回显式 disabled —— 实测这是唯一能让正文稳定产出、且把延迟从 28s
    降到 11s 的取值（该兼容端点默认开启思考，不发字段等于放任思考吃光预算）。
    """
    raw = (os.getenv("SAFETYMIND_THINKING", "off") or "").strip().lower()
    if raw == "omit":
        return None
    if raw in ("", "off") or raw in _FALSE:
        return {"type": "disabled"}
    if raw in ("on",) or raw in _TRUE:
        return {"type": "enabled"}
    if raw.startswith("budget:"):
        try:
            n = int(raw.split(":", 1)[1])
        except (IndexError, ValueError):
            return {"type": "disabled"}
        return {"type": "enabled", "budget_tokens": max(0, n)}
    return {"type": "disabled"}


def intent_model_name(gen_model: str) -> str:
    """意图识别用哪个模型：`SAFETYMIND_INTENT_MODEL` 优先，未设置沿用生成模型。

    为什么允许分离（2026-10-29 实测，test2 n=150、BGE + 低置信回落同口径）：
      生成模型 raccoon/deepseek：准确率 0.980，低置信回落 14652ms
      意图模型 qwen3-8-flash   ：准确率 0.980，低置信回落  2347ms（−84%）
    低置信回落覆盖约 42.7% 请求，是应用层能拿到的最大延迟收益；而**生成**仍必须用强模型
    （换生成模型 accuracy −0.375 / safety −0.625，见 benchmarks/eval_model_swap_ab.py）。

    注意：api/main.py 与 agents/agent_orchestrator.py **两处**都要用它——
    真正跑 /chat 的是 orchestrator 里那个实例（2026-10-29 踩过：只改了一处，实测无效）。
    """
    return (os.getenv("SAFETYMIND_INTENT_MODEL") or "").strip() or gen_model


def temperature_param() -> Optional[float]:
    """生成本身的采样温度；None = 不发该字段（保持端点默认行为）。

    为什么需要（2026-10-29）：闭环做"同一问题两种上下文"的 A/B 时，端点默认温度（1.0）
    让单题得分波动可达 ±1 分，n=10 的 A/B 因此两次给出相反结论（accuracy −0.40 → −0.05）。
    评测要可复现，就必须能把生成侧噪声压下去；但**产品默认行为不能变**，所以这里默认
    返回 None（不发字段），只在显式设置 `SAFETYMIND_TEMPERATURE` 时才生效。

    取值：0~2 的浮点；非法值或空 → None。评测用 `0`。
    """
    raw = (os.getenv("SAFETYMIND_TEMPERATURE") or "").strip()
    if not raw or raw.lower() in ("off", "none", "default"):
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if v < 0:
        return None
    return min(2.0, v)


def answer_style_suffix() -> str:
    """附加在 system prompt 末尾的"作答风格"指令；空串 = 不改（默认）。

    为什么（2026-10-29 延迟分账）：`/chat` 非生成开销只占 8%，瓶颈全在生成侧；
    而生成耗时与输出长度近似线性（实测约 29ms/字）。所以"答案更短"是唯一不需要换模型
    就能改善延迟的杠杆——但它必须用质量 A/B 定价，不能凭感觉砍。

    取值：
      - 不设 / 空 / `default` → 空串（行为与改造前完全一致）
      - `brief` → 先结论、要点式、限长，禁止复述题目与展开背景
    """
    raw = (os.getenv("SAFETYMIND_ANSWER_STYLE") or "").strip().lower()
    if raw != "brief":
        return ""
    return ("\n\n[作答风格]\n"
            "先给结论（一两句），再给要点（最多 5 条，每条一行）。总长控制在 450 字以内；"
            "不要复述题目、不要展开背景知识、不要罗列与问题无关的条款。"
            "需要引用的依据用【依据N】标注；不确定处一句话说明需核实。")


def messages_create_kwargs_base() -> Dict[str, Any]:
    """所有 messages.create 都应带上的公共参数（思考与温度控制集中在这里）。"""
    kwargs: Dict[str, Any] = {}
    tp = thinking_param()
    if tp is not None:
        # anthropic>=0.40 通过 extra_body 透传非签名参数
        kwargs["extra_body"] = {"thinking": tp}
    t = temperature_param()
    if t is not None:
        kwargs["temperature"] = t
    return kwargs


def without_thinking(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """去掉 thinking 的副本：端点明确不支持该字段时用于重试一次。"""
    out = dict(kwargs)
    eb = out.get("extra_body")
    if isinstance(eb, dict) and "thinking" in eb:
        eb = {k: v for k, v in eb.items() if k != "thinking"}
        if eb:
            out["extra_body"] = eb
        else:
            out.pop("extra_body", None)
    return out


def is_thinking_unsupported_error(exc: Exception) -> bool:
    """判断异常是否是端点不接受 thinking 字段（用于决定要不要重试）。"""
    text = str(exc).lower()
    return "thinking" in text and any(
        k in text for k in ("unexpected", "unknown", "not supported", "unsupported", "extra fields"))
