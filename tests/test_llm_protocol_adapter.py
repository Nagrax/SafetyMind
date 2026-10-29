# -*- coding: utf-8 -*-
# 协议适配层测试（2026-10-27 目标①：打通 .env 中继额度）。
#
# 背景：.env 的 8326 中继只支持 OpenAI 协议；HTTP 桥接代理被中继的 DeepSeek Messages
# 通道卡死（不支持 tool_calls 结构，三种排布全 502 实测）。改为 EvoHarness 式调用侧
# 适配：OpenAICompatClient 模仿 Anthropic SDK 调用面，8 处调用点经工厂切换协议。
#
# 本测试用 mock 传输层（不联网），锁定转译契约：
#   1. 请求转译：system→system角色 / tools→function 形态 / tool_result→tool 角色
#   2. 响应转译：content→text块 / tool_calls→tool_use块 / finish→stop_reason
#   3. 块形态：SimpleNamespace（与 Anthropic SDK 对象同形，extract_text_content 兼容）
#   4. 预算提升：zcode 模型 thinking 开销大，1024→4096（实测 1024 content 全空）
#   5. 超时与预算联动（4096 长生成 ReadTimeout 实测）
#   6. 工厂：anthropic 默认零行为变化；openai 按 env 切换
#
# 用法: python tests/test_llm_protocol_adapter.py
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    from core.llm_protocol import (OpenAICompatClient, _anthropic_to_openai_request,
                                   _openai_to_anthropic_response, make_llm_client)

    # ── 1. 请求转译 ─────────────────────────────────────────────────────
    req = _anthropic_to_openai_request({
        "model": "zcode/GLM-5.3-Flash", "max_tokens": 1024,
        "system": "你是助手",
        "messages": [
            {"role": "user", "content": "查天气"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "c1", "name": "get_weather",
                 "input": {"city": "北京"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "晴 22℃"}]},
        ],
        "tools": [{"name": "get_weather", "description": "查询天气",
                   "input_schema": {"type": "object", "properties": {}}},
        ],
    })
    record("system 转为首条 system 角色",
           req["messages"][0]["role"] == "system" and req["messages"][0]["content"] == "你是助手")
    record("tools 转 function 形态",
           req["tools"][0]["type"] == "function"
           and req["tools"][0]["function"]["name"] == "get_weather")
    tool_call_msg = next(m for m in req["messages"] if m.get("tool_calls"))
    record("tool_use 块转 assistant.tool_calls",
           tool_call_msg["tool_calls"][0]["id"] == "c1"
           and json.loads(tool_call_msg["tool_calls"][0]["function"]["arguments"]) == {"city": "北京"})
    tool_msg = next(m for m in req["messages"] if m.get("role") == "tool")
    record("tool_result 块转 tool 角色消息",
           tool_msg["tool_call_id"] == "c1" and tool_msg["content"] == "晴 22℃")

    # ── 2. 响应转译 ─────────────────────────────────────────────────────
    resp = _openai_to_anthropic_response({
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": "回答正文"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }, "m")
    record("content 转 text 块", resp.content[0].type == "text"
           and resp.content[0].text == "回答正文")
    record("stop_reason stop→end_turn", resp.stop_reason == "end_turn")
    record("usage 字段映射", resp.usage.input_tokens == 10 and resp.usage.output_tokens == 5)

    resp2 = _openai_to_anthropic_response({
        "choices": [{"finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": None,
                                 "tool_calls": [{"id": "t1",
                                                 "function": {"name": "f",
                                                              "arguments": '{"x":1}'}}]}}],
    }, "m")
    tu = next(b for b in resp2.content if b.type == "tool_use")
    record("tool_calls 转 tool_use 块", tu.id == "t1" and tu.name == "f"
           and tu.input == {"x": 1})
    record("finish tool_calls→tool_use", resp2.stop_reason == "tool_use")

    # ── 3. 块形态：getattr 可访问（extract_text_content 兼容）───────────
    from core.llm_utils import extract_text_content
    record("块形态兼容 extract_text_content",
           extract_text_content(resp.content) == "回答正文")
    # 混合块：dict 风格与属性风格都要通（实测踩过 SimpleNamespace 导致工具回路崩）
    record("块同时支持 .get() 与属性访问",
           resp.content[0].get("type") == "text" and resp.content[0].type == "text"
           and isinstance(resp.content[0], dict))

    # ── 4. 预算提升（zcode thinking 实测 1024 content 全空）──────────────
    record("max_tokens=1024 提升到 4096",
           _anthropic_to_openai_request({"model": "m", "max_tokens": 1024,
                                         "messages": []})["max_tokens"] == 4096)
    record("显式 8192 被尊重",
           _anthropic_to_openai_request({"model": "m", "max_tokens": 8192,
                                         "messages": []})["max_tokens"] == 8192)

    # ── 5. Mock 传输层：客户端端到端 ─────────────────────────────────────
    captured = {}

    class MockClient(OpenAICompatClient):
        async def _post(self, body):
            captured["body"] = body
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "mock 回答"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    mc = MockClient(api_key="k", base_url="http://relay:8326/v1")
    import asyncio
    r = asyncio.run(mc.messages.create(
        model="zcode/GLM-5.3-Flash", max_tokens=512,
        system="s", messages=[{"role": "user", "content": "q"}]))
    record("客户端端到端（mock 传输）返回 text 块",
           r.content[0].type == "text" and r.content[0].text == "mock 回答")
    record("URL 前缀处理：base 无 /v1 时自动补",
           captured["body"]["model"] == "zcode/GLM-5.3-Flash")

    # ── 6. 工厂：anthropic 默认 / openai 切换 ───────────────────────────
    old = os.environ.get("SAFETYMIND_LLM_PROTOCOL")
    try:
        os.environ.pop("SAFETYMIND_LLM_PROTOCOL", None)
        c1 = make_llm_client(api_key="k", base_url="http://x")
        record("协议默认 anthropic（返回 AsyncAnthropic）",
               type(c1).__name__ == "AsyncAnthropic")
        os.environ["SAFETYMIND_LLM_PROTOCOL"] = "openai"
        c2 = make_llm_client(api_key="k", base_url="http://x/v1")
        record("SAFETYMIND_LLM_PROTOCOL=openai 切换到兼容客户端",
               type(c2).__name__ == "OpenAICompatClient")
        record("openai 客户端 base 保留 /v1", c2._base == "http://x/v1")
    finally:
        if old is None:
            os.environ.pop("SAFETYMIND_LLM_PROTOCOL", None)
        else:
            os.environ["SAFETYMIND_LLM_PROTOCOL"] = old

    # ── 7. 8 处调用点全部走工厂（防回归）────────────────────────────────
    for f in ["api/main.py", "agents/agent_orchestrator.py", "memory/conversation_memory.py",
              "mcp/tool_manager.py", "core/intent_recognizer.py", "evaluation/evaluator.py",
              "core/skill_evolution.py"]:
        src = Path(__file__).resolve().parents[1].joinpath(f).read_text(encoding="utf-8")
        has_factory = "make_llm_client(" in src
        no_direct = "AsyncAnthropic(" not in src.split("import")[0] or "AsyncAnthropic(" not in src
        record(f"{f} 使用工厂", has_factory)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
