# -*- coding: utf-8 -*-
"""OpenAI 兼容客户端（EvoHarness 式调用侧协议适配，2026-10-27）。

## 解决什么

`.env` 中继（127.0.0.1:8326）只支持 OpenAI 协议，而 SafetyMind 全部 LLM 调用点
都写死了 Anthropic SDK（`AsyncAnthropic.messages.create`）。两套额度互不相通：
DeepSeek 账户额度（中继，无窗口限制）给不了 SafetyMind 用。

上一版尝试 HTTP 桥接代理（anthropic_bridge.mjs），实测被中继的 DeepSeek Messages
通道卡死：它**完全不支持 OpenAI tool_calls 结构**（三种排布全 502），只支持纯文本
对话。走"文本化工具"翻译可行但要在桥接器里解析模型文本——契约脆弱。

## 本版方案（EvoHarness 式：调用侧适配，零桥接进程）

`OpenAICompatClient` **模仿 Anthropic SDK 的调用面**（`messages.create(**kwargs)`、
返回 `.content` 块列表 / `.stop_reason` / `.usage`），内部用 httpx 调 OpenAI 端点。
8 处 `AsyncAnthropic(...)` 调用点只需在构造处替换，业务代码零改动——
与 EvoHarness `Agent.use_openai` 分流的设计同构（`_chat_openai` vs `_chat_anthropic`）。

工具回路转译（Anthropic tools ↔ OpenAI tools 双向）：
  请求侧: tools[{name,description,input_schema}] → tools[{type:function,function:{}}]
          system: str → messages[0] system 角色
          tool_result 块（user 消息内）→ role:'tool' 消息
  响应侧: message.content(str) → [{type:text,text}]
          message.tool_calls → [{type:tool_use,id,name,input}]
          finish_reason tool_calls/stop/length → tool_use/end_turn/max_tokens

## 关键实测结论（写死在注释里，防重犯）

  1. `zcode/GLM-5.3-Flash` 在 max_tokens=256 时 content 全空（thinking 吃光预算，
     finish=length），**1024 预算即正常直出 JSON**——上一版"zcode 不行"是预算误判。
  2. DeepSeek Messages（中继）不支持 tool_calls 结构，只支持纯文本对话；
     zcode/GLM-5.3-Flash 走 OpenAI tools 结构正常。**选 zcode 模型**。
  3. reasoning_content 与 content 是两个字段：正文在 content；reasoning 是思考，
     不应当正文透传（会让"内心独白"出现在用户回答里）。

## 启用方式

    SAFETYMIND_LLM_PROTOCOL=openai   # 默认 anthropic（零行为变化）
    ANTHROPIC_BASE_URL=http://127.0.0.1:8326/v1   # 复用现有 env 键
    ANTHROPIC_MODEL=zcode/GLM-5.3-Flash

## 与 EvoHarness 的差异（如实说明）

EvoHarness 在 Agent 内维护 `_anthropic_messages`/`_openai_messages` 双栈并支持
互相迁移；SafetyMind 的调用是无状态的 `messages.create(**kwargs)`，不需要消息栈——
适配收敛在"请求转译 + 响应转译"两个纯函数上，面更小。
"""
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


def _simple_namespace(**kw):
    from types import SimpleNamespace
    return SimpleNamespace(**kw)


def _anthropic_to_openai_request(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic messages.create kwargs → OpenAI chat/completions body。"""
    model = kwargs.get("model") or ""
    if model and "/" not in model:
        # 中继要求 provider/model 格式；provider 从 base_url 或 env 推导
        provider = os.getenv("SAFETYMIND_OPENAI_PROVIDER", "")
        if provider:
            model = f"{provider}/{model}"
    # 预算提升（2026-10-27 实测）：zcode/GLM-5.3-Flash 的思考占 ~1670 字符，
    # max_tokens=1024 时 finish=length 且 content 全空（冒烟 800/503 tokens 实测）；
    # 4096 才能稳定直出正文（completion_tokens=389 + reasoning 1670 字符）。
    # 中继无 thinking 开关，只能用预算换稳定。调用方传小值时提升到 4096，
    # 显式传 >=4096 则尊重调用方。
    # 2026-10-29 追加实测：对 zcode/GLM-5.3-Flash 来说**预算提升也救不回来**——
    #   4096 下 reasoning 约 10.4k 字、content 恒为 0（reasoning_effort=none 与
    #   thinking={"type":"disabled"} 都不被该模型消费）。所以"调大预算"只对**可控**的
    #   模型有意义；默认模型已被切到会遵守 reasoning_effort 的
    #   raccoon/sn-deepseek-v4-1-flash-1017（实测 6.4s / 990 字 / reasoning 0）。
    #   因此 SAFETYMIND_MAX_TOKENS 在本协议下只能**抬高**，不能压低到 4096 以下。
    requested = int(kwargs.get("max_tokens") or 0)
    out: Dict[str, Any] = {
        "model": model,
        "max_tokens": max(requested, 4096),
        "messages": [],
    }
    if kwargs.get("temperature") is not None:
        out["temperature"] = kwargs["temperature"]
    # ── 推理开关（2026-10-28 实测，本轮最大的单点收益）────────────────────
    # 中继上的推理模型（raccoon/deepseek、glm、zcode 系）会先用掉大量 token 做
    # reasoning_content，再开始写正文。实测 raccoon/sn-deepseek-v4-1-flash-1017：
    #   baseline(有推理)  隐患分级 36592ms，**正文 0 字**（4096 预算全被推理吃光）
    #   reasoning_effort=none  10792ms，1414 字 —— 快 3.4× 且**质量更好**
    # 四个用例 A/B（动火票/隐患分级/受限空间/台账要素）：
    #   延迟 2.4~4.8× 下降，正文更长，关键事实命中 8/14 → 11/14。
    # 原因：推理与正文共享 max_tokens 预算，推理越长正文越可能被截断。
    # OpenAI 官方参数 reasoning_effort 被中继透传（实测有效，reasoning=0）。
    # 可用 SAFETYMIND_OPENAI_REASONING_EFFORT 覆盖（none/low/medium/high）；
    # 设为空字符串则不发送该字段（回退旧行为）。
    _effort = os.getenv("SAFETYMIND_OPENAI_REASONING_EFFORT", "none").strip().lower()
    if _effort and _effort != "off":
        out["reasoning_effort"] = _effort
    if kwargs.get("system"):
        out["messages"].append({"role": "system", "content": kwargs["system"]})
    for m in (kwargs.get("messages") or []):
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            out["messages"].append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            out["messages"].append({"role": role, "content": str(content or "")})
            continue
        texts, tool_calls = [], []
        pending_results = []
        for b in content:
            btype = b.get("type") or ("text" if isinstance(b, str) else "")
            if btype == "text" or isinstance(b, str):
                texts.append(b if isinstance(b, str) else (b.get("text") or ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": b.get("id"),
                    "type": "function",
                    "function": {"name": b.get("name", ""),
                                 "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)},
                })
            elif btype == "tool_result":
                inner = b.get("content")
                if isinstance(inner, list):
                    inner = "\n".join(x.get("text", "") for x in inner if isinstance(x, dict))
                pending_results.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id"),
                    "content": str(inner or ""),
                })
            # thinking / 其它块：上游不消费，跳过
        if tool_calls:
            msg = {"role": "assistant", "tool_calls": tool_calls,
                   "content": ("\n".join(texts) if texts else None)}
            out["messages"].append(msg)
            out["messages"].extend(pending_results)
        elif pending_results:
            out["messages"].extend(pending_results)
        elif texts:
            out["messages"].append({"role": role, "content": "\n".join(texts)})
    tools = kwargs.get("tools")
    if tools:
        out["tools"] = [{
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        } for t in tools]
    return out


_STOP_MAP = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens",
             "function_call": "tool_use"}


class _Block(dict):
    """内容块：dict 子类 + 属性访问（同时满足 `.get()` 与 `getattr()`）。

    为什么不是 SimpleNamespace（实测踩过）：SafetyMind 里两种访问风格并存——
    `core/tool_execution._field()` 用 isinstance(dict) 分支，但下游仍有直接
    `block.get("input")` 的地方。返回 SimpleNamespace 后工具回路第 2 轮报
    `'types.SimpleNamespace' object has no attribute 'get'`，整条回路降级成
    "抱歉，处理您的请求时出现问题"。
    dict 子类让 isinstance(block, dict) 为真（走 .get 分支），
    __getattr__ 再提供属性访问（走 getattr 分支），两种风格都不会崩。
    """

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _block(**kw):
    """构造混合块（见 _Block 的说明）。"""
    return _Block(**kw)


def _openai_to_anthropic_response(data: Dict[str, Any], model: str):
    choice = (data.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    content: List[Any] = []
    direct = (msg.get("content") or "").strip()
    # reasoning 是思考不是正文（实测：当正文透传会泄漏内心独白）；无正文时兜底
    reasoning = (msg.get("reasoning_content") or "").strip()
    if direct:
        content.append(_block(type="text", text=direct))
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        try:
            input_data = json.loads(fn.get("arguments") or "{}")
        except Exception:
            input_data = {}
        content.append(_block(type="tool_use", id=tc.get("id"),
                              name=fn.get("name", ""), input=input_data))
    if not content and reasoning:
        # 思考吃光预算（finish_reason=length 且正文为空）：**绝不把内心独白当正文**。
        # 实测（2026-10-29）：zcode/GLM-5.3-Flash 在 4096 预算下 reasoning 10.4k 字、正文 0 字，
        # 旧兜底把 reasoning 尾巴当答案交给用户（428 字的"nswer in Chinese, concise but…"）。
        # 现在返回空正文，由上层如实提示"未能生成完整回答"；配置了 SAFETYMIND_FALLBACK_MODEL
        # 时，_maybe_fallback 会先换模型重试一次。
        if _STOP_MAP.get(choice.get("finish_reason")) == "max_tokens":
            content.append(_block(type="text", text=""))
        else:
            content.append(_block(type="text", text=reasoning[-400:]))
    if not content:
        content.append(_block(type="text", text=""))
    u = data.get("usage") or {}
    return _simple_namespace(
        id=data.get("id", ""),
        model=data.get("model", model),
        content=content,
        stop_reason=_STOP_MAP.get(choice.get("finish_reason"), "end_turn"),
        stop_sequence=None,
        usage=_simple_namespace(input_tokens=u.get("prompt_tokens", 0),
                                output_tokens=u.get("completion_tokens", 0)),
    )


class OpenAICompatClient:
    """模仿 AsyncAnthropic 调用面的 OpenAI 兼容客户端（EvoHarness 式调用侧适配）。

    用法（与 AsyncAnthropic 同形）：
        client = OpenAICompatClient(api_key=..., base_url="http://127.0.0.1:8326/v1")
        resp = await client.messages.create(model=..., system=..., messages=[...])
        # resp.content[0].text / resp.stop_reason / resp.usage.input_tokens
    """

    def __init__(self, api_key: str = "", base_url: str = "",
                 timeout: float = 60.0, max_retries: int = 1, **_ignored):
        self._base = (base_url or "").rstrip("/")
        self._key = api_key or ""
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._max_retry_wait = float(os.getenv("SAFETYMIND_OPENAI_RETRY_WAIT", "20"))

    async def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = self._base + "/chat/completions"
        headers = {"content-type": "application/json"}
        if self._key:
            headers["authorization"] = "Bearer " + self._key
        # 超时与预算联动：4096 预算 + 推理模型的 thinking 段实测 60s+（ReadTimeout 实测），
        # 固定 60s 超时会掐掉合法长生成。按 max_tokens 线性放大，下限 90s。
        timeout = max(self._timeout, 90.0,
                      (body.get("max_tokens") or 0) / 4096 * 120.0)
        last_err: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.post(url, headers=headers, json=body)
                if r.status_code == 429:
                    # 中继限流：二次方退避（铁律 11 口径）
                    await __import__("asyncio").sleep(min(self._max_retry_wait,
                                                          2.0 * (attempt + 1) ** 2))
                    last_err = RuntimeError("HTTP 429")
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                return r.json()
            except httpx.HTTPError as ex:
                last_err = ex
                if attempt < self._max_retries:
                    await __import__("asyncio").sleep(1.0)
        raise last_err or RuntimeError("openai request failed")

    async def _post_stream(self, body: Dict[str, Any]):
        """流式请求：逐块产出 delta 文本（OpenAI SSE 形态）。

        为什么（2026-10-28 闭环）：关推理后单次生成仍需 10-16 秒，用户在这段时间
        看不到任何进展。流式把**首字延迟（TTFT）**从"全时长"降到 1-2 秒，
        对"安全员快速查一条规程"的体感是决定性差别（总时长不变，等待感消失）。

        只解析 `choices[0].delta.content`；`reasoning_content` 的增量**不输出**
        （那是思考，不是答案——见 _openai_to_anthropic_response 的同类判断）。
        上游不接受 stream 参数时降级为非流式单块产出，保证调用方永远有输出。
        """
        url = self._base + "/chat/completions"
        headers = {"content-type": "application/json"}
        if self._key:
            headers["authorization"] = "Bearer " + self._key
        timeout = max(self._timeout, 90.0,
                      (body.get("max_tokens") or 0) / 4096 * 120.0)
        payload = dict(body)
        payload["stream"] = True
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream("POST", url, headers=headers, json=payload) as r:
                    if r.status_code >= 400:
                        text = (await r.aread()).decode("utf-8", "ignore")
                        raise RuntimeError(f"HTTP {r.status_code}: {text[:300]}")
                    async for line in r.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk in ("", "[DONE]"):
                            continue
                        try:
                            data = json.loads(chunk)
                        except Exception:
                            continue
                        for ch in (data.get("choices") or []):
                            delta = (ch.get("delta") or {}).get("content")
                            if delta:
                                yield delta
        except RuntimeError:
            raise
        except Exception as ex:
            # 流式中断：降级为非流式，保证有输出（宁可慢也不能空）
            data = await self._post(body)
            resp = _openai_to_anthropic_response(data, body.get("model", ""))
            text = "".join(b.text for b in resp.content
                           if getattr(b, "type", "") == "text")
            if text:
                yield text

    class _Messages:
        def __init__(self, client: "OpenAICompatClient"):
            self._client = client

        async def create(self, **kwargs):
            body = _anthropic_to_openai_request(kwargs)
            data = await self._client._post(body)
            data = await self._client._maybe_fallback(body, data)
            return _openai_to_anthropic_response(data, body.get("model", ""))

        def stream(self, **kwargs):
            """流式接口：返回 async generator（与 create 同参数）。"""
            body = _anthropic_to_openai_request(kwargs)
            return self._client._post_stream(body)

    async def _maybe_fallback(self, body: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """模型病理兜底：思考吃光预算（finish=length 且正文 0 字）时换备用模型重试**一次**。

        实测（2026-10-29）这条中继上有一类模型（zcode/GLM-5.3-Flash、aggregate/glm-5-3-flash、
        minimax/MiniMax-M3.1-Flash）不响应 reasoning_effort/thinking，预算全被思考吃掉、
        正文恒为空。靠调大预算救不回来（reasoning 会等比增长），唯一有效处置是换模型。

        配置 `SAFETYMIND_FALLBACK_MODEL`（例：`raccoon/sn-deepseek-v4-1-flash-1017`）后启用；
        不配则完全保持旧行为（由上层如实提示未能生成）。只重试一次，不递归。
        """
        fallback = (os.getenv("SAFETYMIND_FALLBACK_MODEL") or "").strip()
        if not fallback or fallback == body.get("model"):
            return data
        choice = ((data.get("choices") or [{}])[0] or {})
        msg = choice.get("message") or {}
        starved = (choice.get("finish_reason") == "length"
                   and not (msg.get("content") or "").strip()
                   and bool((msg.get("reasoning_content") or "").strip()))
        if not starved:
            return data
        logger.warning("模型 %s 正文为空且思考吃满预算，改用备用模型 %s 重试一次",
                       body.get("model"), fallback)
        retry_body = dict(body)
        retry_body["model"] = fallback
        try:
            return await self._post(retry_body)
        except Exception as ex:  # 备用模型也失败：保留原始响应，由上层如实提示
            logger.error("备用模型 %s 调用失败，保留原始（空正文）响应: %s", fallback, ex)
            return data

    @property
    def messages(self) -> "OpenAICompatClient._Messages":
        return OpenAICompatClient._Messages(self)

    # SafetyMind 的记忆/技能模块会调用 close()
    async def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def make_llm_client(api_key: str, base_url: str = "", model: str = "",
                    **kwargs):
    """按 SAFETYMIND_LLM_PROTOCOL 构造 LLM 客户端（工厂，EvoHarness use_openai 同思路）。

    - anthropic（默认）：AsyncAnthropic，行为与现状逐字节一致
    - openai：OpenAICompatClient（走中继 OpenAI 协议，消费 DeepSeek 额度）
      base_url 末尾的 /v1 会被规范化掉（本客户端自拼 /chat/completions）。
    """
    protocol = (os.getenv("SAFETYMIND_LLM_PROTOCOL", "anthropic") or "anthropic").strip().lower()
    if protocol == "openai":
        base = (base_url or "").strip().rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"   # OpenAI 兼容端点固定前缀（中继实测 /v1/chat/completions）
        return OpenAICompatClient(api_key=api_key, base_url=base, **kwargs)
    from anthropic import AsyncAnthropic
    kw = {"api_key": api_key}
    if base_url:
        kw["base_url"] = base_url
    return AsyncAnthropic(**kw, **kwargs)
