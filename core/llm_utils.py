"""LLM response helpers shared by Anthropic-compatible providers."""
import os
from typing import Any, Dict, Iterable, List


def llm_client_kwargs() -> Dict[str, Any]:
    """AsyncAnthropic 公共参数：统一超时与重试（所有 LLM 客户端实例化必须带上）。

    SDK 默认 timeout=600s：后端故障/限流时一次调用可挂数分钟，
    并把 /chat 并发闸门一起占死，后续请求全部排队超时（线上压测实测复现）。
    默认 60s 超时 + 1 次重试；SAFETYMIND_LLM_TIMEOUT_S / SAFETYMIND_LLM_MAX_RETRIES 可覆盖。
    """
    return {
        "timeout": float(os.getenv("SAFETYMIND_LLM_TIMEOUT_S", "60")),
        "max_retries": int(os.getenv("SAFETYMIND_LLM_MAX_RETRIES", "1")),
    }


def make_llm_client(api_key: str, base_url: str = "", model: str = "", **kwargs):
    """LLM 客户端工厂（2026-10-27）：按 SAFETYMIND_LLM_PROTOCOL 返回对应协议客户端。

    背景：.env 的 8326 中继只支持 OpenAI 协议，而全仓库 10 处调用点写死了
    AsyncAnthropic。HTTP 桥接代理方案被中继的 DeepSeek Messages 通道卡死
    （不支持 OpenAI tool_calls 结构，只支持纯文本对话，三种排布全 502 实测）。
    本工厂走 EvoHarness 式调用侧适配：core/llm_protocol.OpenAICompatClient
    模仿 Anthropic SDK 调用面（messages.create / .content / .stop_reason），
    各调用点把 `AsyncAnthropic(**kw, **llm_client_kwargs())` 换成本工厂即可，
    业务代码零改动。

    协议选择：SAFETYMIND_LLM_PROTOCOL=anthropic（默认，零行为变化）| openai。
    base_url / model 复用现有 ANTHROPIC_* 环境变量（.env 无需新增键）。
    """
    from core.llm_protocol import make_llm_client as _make
    return _make(api_key=api_key, base_url=base_url, model=model, **kwargs)


def extract_text_content(content: Iterable[Any]) -> str:
    """Return text blocks from Anthropic-style response content."""
    texts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and (block_type in (None, "text")):
            texts.append(text)

    return "\n".join(t for t in texts if t)
