# -*- coding: utf-8 -*-
# 模型病理兜底回归测试（2026-10-29 闭环第 38 轮）。
#
# 背景：这条中继上有一类模型（zcode/GLM-5.3-Flash、aggregate/glm-5-3-flash、
# minimax/MiniMax-M3.1-Flash）不响应 reasoning_effort / thinking，预算全被思考吃掉、
# 正文恒为 0 字；调大预算无效（reasoning 等比增长）。旧行为会把 reasoning 尾巴当正文
# 交给用户——用户看到的是模型的内心独白。唯一有效处置是换模型。
#
# 本测试锁定：
#   1. 配置 SAFETYMIND_FALLBACK_MODEL 时：病理响应 → 换模型重试一次并返回可用正文
#   2. 未配置时：不重试；病理响应正文为空（不泄漏内心独白），stop_reason 如实标 max_tokens
#   3. 备用模型也病理：只重试一次，仍返回空正文（不无限重试）
#   4. 正常响应：一次调用、内容不变（旧行为不受影响）
#
# 用法: python tests/test_model_fallback.py
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def starved(reasoning="我先分析一下这个问题的判定标准……"):
    return {"choices": [{"finish_reason": "length",
                         "message": {"content": "", "reasoning_content": reasoning}}]}


def ok_resp(text="动火票分特级/一级/二级，有效期 8/24/72 小时。依据 GB 30871。"):
    return {"choices": [{"finish_reason": "stop",
                         "message": {"content": text, "reasoning_content": ""}}]}


def make_client(responses):
    """构造 OpenAICompatClient，并把 _post 换成按脚本返回的桩（记录每次请求体）。"""
    from core.llm_protocol import OpenAICompatClient
    client = OpenAICompatClient(api_key="k", base_url="http://127.0.0.1:9/v1")
    calls = []

    async def fake_post(body):
        calls.append(dict(body))
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    client._post = fake_post
    return client, calls


def text_of(resp):
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def main():
    saved = os.environ.get("SAFETYMIND_FALLBACK_MODEL")

    def set_fallback(v):
        if v is None:
            os.environ.pop("SAFETYMIND_FALLBACK_MODEL", None)
        else:
            os.environ["SAFETYMIND_FALLBACK_MODEL"] = v

    try:
        # ── 1. 配置备用模型 → 病理响应触发换模型 ─────────────────────────
        set_fallback("raccoon/sn-deepseek-v4-1-flash-1017")
        client, calls = make_client([starved(), ok_resp()])
        resp = asyncio.run(client.messages.create(
            model="zcode/GLM-5.3-Flash", max_tokens=4096,
            messages=[{"role": "user", "content": "动火票有效期"}]))
        record("病理响应触发备用模型重试（共 2 次调用）", len(calls) == 2, f"calls={len(calls)}")
        record("重试请求确实换了模型",
               len(calls) == 2 and calls[1]["model"] == "raccoon/sn-deepseek-v4-1-flash-1017",
               f"model={calls[1]['model'] if len(calls) > 1 else None}")
        record("返回备用模型的可用正文", "GB 30871" in text_of(resp), f"text={text_of(resp)[:40]!r}")
        record("stop_reason 为 end_turn", resp.stop_reason == "end_turn", resp.stop_reason)

        # ── 2. 未配置备用模型 → 不重试、正文为空（不泄漏内心独白）─────────
        set_fallback(None)
        client, calls = make_client([starved("我在想这个问题应该怎么答……")])
        resp = asyncio.run(client.messages.create(
            model="zcode/GLM-5.3-Flash", max_tokens=4096,
            messages=[{"role": "user", "content": "动火票有效期"}]))
        record("未配置备用模型时只调用一次", len(calls) == 1, f"calls={len(calls)}")
        record("病理响应不泄漏 reasoning 作为正文",
               text_of(resp) == "" and "我在想" not in text_of(resp),
               f"text={text_of(resp)!r}")
        record("病理响应 stop_reason 如实标 max_tokens",
               resp.stop_reason == "max_tokens", resp.stop_reason)

        # ── 3. 备用模型也病理 → 只重试一次，仍空 ─────────────────────────
        set_fallback("backup/model")
        client, calls = make_client([starved(), starved("备用的思考过程……")])
        resp = asyncio.run(client.messages.create(
            model="zcode/GLM-5.3-Flash", max_tokens=4096,
            messages=[{"role": "user", "content": "q"}]))
        record("备用模型也病理时只重试一次（不递归）", len(calls) == 2, f"calls={len(calls)}")
        record("两级都病理时正文仍为空（不泄漏）", text_of(resp) == "", f"text={text_of(resp)!r}")

        # ── 4. 正常响应不受影响 ─────────────────────────────────────────
        set_fallback("backup/model")
        client, calls = make_client([ok_resp("正常答案")])
        resp = asyncio.run(client.messages.create(
            model="zcode/GLM-5.3-Flash", max_tokens=4096,
            messages=[{"role": "user", "content": "q"}]))
        record("正常响应只调用一次（不触发兜底）", len(calls) == 1, f"calls={len(calls)}")
        record("正常响应内容不变", text_of(resp) == "正常答案", f"text={text_of(resp)!r}")
    finally:
        set_fallback(saved)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
