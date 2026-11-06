# -*- coding: utf-8 -*-
# 工具预算耗尽"无正文"时的收尾调用回归测试（2026-11-01 闭环第 61 轮）。
#
# ## 缺陷（真实评测）
#
# 24 题答复失败率评测里出现 1 例："安全阀的校验周期是怎么规定的？超期有什么后果？"
# 跑完 3 轮工具后只回了 **23 个字**："工具调用未在预算内完成，未获得可用的最终回答。"
# —— 资料其实已经检到，只是模型没来得及组织正文。
#
# ## 修法
#
# 轮次预算耗尽且**没有任何文字**时，追加**一次**无工具收尾调用（提示"基于已有结果直接作答、
# 不要再请求工具"），拿到正文就用；失败则回落证据摘要/固定文案。
#
# ## 必须守住的既有契约（本测试同时锁定）
#
# - `max_rounds=1`：不再请求模型（旧测试锁定，这里显式断言）
# - `max_calls` 超限（budget_calls）：不再请求模型（旧测试锁定）
#
# 用法: python tests/test_tool_final_answer.py
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


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def text_block(t):
    return Block(type="text", text=t)


def tool_block(tid, name, inp):
    return Block(type="tool_use", id=tid, name=name, input=inp)


class Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.messages = self

    async def create(self, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


class FakeTM:
    def __init__(self):
        from mcp.tool_manager import MCPToolManager, Tool

        async def handler(params, context):
            return {"命中条款": "GB 30871-2022：动火作业票有效期不超过 8h"}
        self._mgr = MCPToolManager(api_key="t")
        self._mgr.register(Tool(name="knowledge_search", description="假检索",
                                handler=handler, cache_ttl=0.0,
                                schema={"type": "object", "properties": {"query": {"type": "string"}}}))

    def __getattr__(self, item):
        return getattr(self._mgr, item)


def run_case(responses, max_rounds=2):
    from core.tool_execution import ToolExecutor
    client = FakeClient(responses)
    ex = ToolExecutor(client, "m", FakeTM(), allowlist=["knowledge_search"],
                      max_rounds=max_rounds)
    res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))
    return res, client


def main():
    # ── 1. 轮次耗尽且无文字 → 追加一次收尾调用并采用其正文 ──────────────
    res, client = run_case([
        Resp([tool_block("t1", "knowledge_search", {"query": "安全阀校验周期"})]),
        Resp([tool_block("t2", "knowledge_search", {"query": "安全阀校验周期"})]),
        Resp([text_block("安全阀一般每年至少校验一次，超期不得继续使用。")]),
    ], max_rounds=2)
    record("轮次耗尽且无正文时产出可用回答",
           "每年至少校验一次" in res.text, f"text={res.text[:40]!r}")
    record("只追加一次收尾调用（总调用 3 次）", client.calls == 3, f"calls={client.calls}")
    record("终止原因与降级标记保持不变",
           res.terminated == "budget_rounds" and res.degraded is True,
           f"term={res.terminated} degraded={res.degraded}")

    # ── 2. 收尾调用本身失败 → 回落固定文案，不抛异常 ─────────────────────
    res, client = run_case([
        Resp([tool_block("t1", "knowledge_search", {"query": "x"})]),
        Resp([tool_block("t2", "knowledge_search", {"query": "x"})]),
    ], max_rounds=2)
    record("收尾调用无响应时不崩且仍有文本", bool(res.text), f"text={res.text[:30]!r}")

    # ── 3. max_rounds=1 契约：不再请求模型 ───────────────────────────────
    res, client = run_case([
        Resp([tool_block("t1", "knowledge_search", {"query": "x"})]),
    ], max_rounds=1)
    record("max_rounds=1 时绝不追加调用（契约不变）", client.calls == 1, f"calls={client.calls}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
