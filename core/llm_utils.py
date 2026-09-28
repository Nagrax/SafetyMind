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
