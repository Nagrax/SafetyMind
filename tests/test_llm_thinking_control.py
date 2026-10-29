# -*- coding: utf-8 -*-
# 思考块控制的回归测试（闭环 2026-10-25 第 1 轮发现的真实缺陷）。
#
# 缺陷现象：真实端点下常规问答只有 0 字正文，用户看到「（注意：本次回答因达到长度上限被截断）」。
# 根因（实测 n=3，见 scratch/sm-loop/runs/thinking_control_strict.json）：
#   思考块不受控地吃掉输出预算，把 max_tokens 从 1024 提到 4096 并不能解决。
#   thinking=disabled 时 0/3 截断、延迟 11~14s；不发该参数时 1/3 截断、延迟 28s。
#
# 覆盖：
#   1. SAFETYMIND_THINKING=off（默认）→ 请求不带 thinking 字段（最兼容，不惹严格校验端点）
#   2. SAFETYMIND_THINKING=on        → 经 extra_body 带上 thinking
#   3. SAFETYMIND_THINKING=budget:512 → 带 budget_tokens=512
#   4. 端点因 thinking 字段报错 → 自动去掉该字段重试一次并成功（不把兼容问题变成对话失败）
#   5. 思考吃光预算（正文为空 + stop_reason=max_tokens）→ 必须有明确失败文案，
#      绝不能只回一句"被截断"而没有内容（这是用户实际看到的坏体验）
# 用法: python tests/test_llm_thinking_control.py
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class FakeMessages:
    """记录每次 create 的 kwargs；可选按 order 抛错来模拟端点拒绝。"""

    def __init__(self, script, raise_on=()):
        self._script = list(script)
        self.calls = []
        self._raise_on = list(raise_on)   # [(call_index, exception)]

    async def create(self, **kwargs):
        idx = len(self.calls)
        self.calls.append(kwargs)
        for at, exc in self._raise_on:
            if idx == at:
                raise exc
        if not self._script:
            raise AssertionError("假客户端脚本已耗尽")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script, raise_on=()):
        self.messages = FakeMessages(script, raise_on)
        self.calls = self.messages.calls


def resp(blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


def text_block(t):
    return {"type": "text", "text": t}


def thinking_block(t):
    return {"type": "thinking", "thinking": t}


def make_manager():
    from mcp.tool_manager import MCPToolManager
    return MCPToolManager(api_key="dummy", base_url="http://127.0.0.1:9", model="dummy")


def with_env(value):
    """上下文：临时设置 SAFETYMIND_THINKING。"""
    import os

    class _Ctx:
        def __enter__(self):
            self._old = os.environ.get("SAFETYMIND_THINKING")
            if value is None:
                os.environ.pop("SAFETYMIND_THINKING", None)
            else:
                os.environ["SAFETYMIND_THINKING"] = value

        def __exit__(self, *a):
            if self._old is None:
                os.environ.pop("SAFETYMIND_THINKING", None)
            else:
                os.environ["SAFETYMIND_THINKING"] = self._old

    return _Ctx()


# ── 1. 默认显式关闭思考（实测唯一有效）──────────────────────────────────────
def t1_default_off_omits_field():
    from core.tool_execution import ToolExecutor

    with with_env(None):
        client = FakeClient([resp([text_block("默认回答")])])
        ex = ToolExecutor(client, "m", make_manager(), allowlist=[])
        res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    eb = client.calls[0].get("extra_body") or {}
    record("默认（SAFETYMIND_THINKING 未设）显式发 thinking=disabled（实测唯一有效）",
           (eb.get("thinking") or {}).get("type") == "disabled",
           f"extra_body={eb}")
    record("默认路径仍能拿到正文", res.text == "默认回答" and not res.degraded,
           f"text={res.text!r} degraded={res.degraded}")


# ── 1b. omit 模式：完全不发字段（给严格校验入参的端点留出口）─────────────────
def t1b_omit_sends_no_field():
    from core.tool_execution import ToolExecutor

    with with_env("omit"):
        client = FakeClient([resp([text_block("省略回答")])])
        ex = ToolExecutor(client, "m", make_manager(), allowlist=[])
        asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    eb = client.calls[0].get("extra_body") or {}
    record("SAFETYMIND_THINKING=omit 时完全不发 thinking 字段",
           "thinking" not in eb,
           f"extra_body={eb}")


# ── 2/3. 显式开启时带上 thinking 与预算 ─────────────────────────────────────
def t2_on_adds_thinking():
    from core.tool_execution import ToolExecutor

    with with_env("on"):
        client = FakeClient([resp([text_block("带思考的回答")])])
        ex = ToolExecutor(client, "m", make_manager(), allowlist=[])
        asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    eb = client.calls[0].get("extra_body") or {}
    record("SAFETYMIND_THINKING=on 时请求经 extra_body 带上 thinking",
           eb.get("thinking") == {"type": "enabled"}, f"extra_body={eb}")


def t3_budget_adds_budget_tokens():
    from core.tool_execution import ToolExecutor

    with with_env("budget:512"):
        client = FakeClient([resp([text_block("预算回答")])])
        ex = ToolExecutor(client, "m", make_manager(), allowlist=[])
        asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    eb = client.calls[0].get("extra_body") or {}
    tp = eb.get("thinking") or {}
    record("SAFETYMIND_THINKING=budget:512 时带 budget_tokens=512",
           tp.get("type") == "enabled" and tp.get("budget_tokens") == 512, f"thinking={tp}")


# ── 4. 端点拒绝 thinking 字段 → 自动去掉重试（兼容性兜底）──────────────────
def t4_unsupported_thinking_retries_without_it():
    from core.tool_execution import ToolExecutor

    boom = TypeError("unexpected keyword argument 'thinking'")
    with with_env("on"):
        client = FakeClient([resp([text_block("去掉思考后的回答")])], raise_on=[(0, boom)])
        ex = ToolExecutor(client, "m", make_manager(), allowlist=[])
        res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    first = (client.calls[0].get("extra_body") or {})
    second = (client.calls[1].get("extra_body") if len(client.calls) > 1 else {}) or {}
    record("端点拒绝 thinking：第二次请求不再带该字段",
           "thinking" in first and "thinking" not in second,
           f"first={first} second={second} calls={len(client.calls)}")
    record("端点拒绝 thinking：去掉后仍拿到正文（兼容问题不变成对话失败）",
           res.text == "去掉思考后的回答" and len(client.calls) == 2,
           f"text={res.text!r} calls={len(client.calls)}")


# ── 5. 思考吃光预算（正文为空）→ 必须给出明确、可读的失败文案 ───────────────
def t5_empty_text_on_truncation_is_explicit():
    from core.tool_execution import ToolExecutor

    with with_env(None):
        # 只有 thinking 块、stop_reason=max_tokens：正是本轮实测到的坏情形
        client = FakeClient([resp([thinking_block("长篇思考" * 100)], stop_reason="max_tokens")])
        ex = ToolExecutor(client, "m", make_manager(), allowlist=[])
        res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    record("思考吃光预算：terminated=max_tokens 且 degraded=True",
           res.terminated == "max_tokens" and res.degraded is True,
           f"term={res.terminated} degraded={res.degraded}")
    record("思考吃光预算：正文非空且明确说明未生成答案（不能只回一句'被截断'）",
           len(res.text) > 20 and "未能生成" in res.text and "截断" not in res.text.split("抱歉")[0],
           f"text={res.text!r}")


# ── 6. 环境变量解析单元测试 ────────────────────────────────────────────────
def t6_env_parsing():
    from core.llm_params import thinking_param, is_thinking_unsupported_error

    with with_env(None):
        record("thinking_param 默认 disabled", (thinking_param() or {}).get("type") == "disabled", f"{thinking_param()}")
    with with_env("off"):
        record("thinking_param off → disabled", (thinking_param() or {}).get("type") == "disabled", f"{thinking_param()}")
    with with_env("on"):
        record("thinking_param on → enabled", thinking_param() == {"type": "enabled"},
               f"{thinking_param()}")
    with with_env("budget:0"):
        tp = thinking_param() or {}
        record("thinking_param budget:0 → budget_tokens=0",
               tp.get("budget_tokens") == 0, f"{tp}")
    with with_env("budget:bad"):
        record("thinking_param 非法预算 → disabled（不抛错）", (thinking_param() or {}).get("type") == "disabled",
               f"{thinking_param()}")
    record("is_thinking_unsupported_error 识别 TypeError",
           is_thinking_unsupported_error(TypeError("unexpected keyword argument 'thinking'")))
    record("is_thinking_unsupported_error 不误判普通超时",
           not is_thinking_unsupported_error(TimeoutError("connection timed out")))


def main():
    t1_default_off_omits_field()
    t1b_omit_sends_no_field()
    t2_on_adds_thinking()
    t3_budget_adds_budget_tokens()
    t4_unsupported_thinking_retries_without_it()
    t5_empty_text_on_truncation_is_explicit()
    t6_env_parsing()

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
