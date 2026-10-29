# -*- coding: utf-8 -*-
# 工具回路"过程话术当答案"回归测试（2026-10-29 闭环第 35 轮）。
#
# 依据（真实 /chat 产物实测）：20 条回答里有 2 条只有 21/25 字，内容是
#   "我再补充检索安全阀校验周期的具体依据条款。" / "第一轮检索未直接命中该条款，我再做一次针对性确认。"
# ——模型在**同一轮里既说要查、又请求了工具**，随后预算耗尽，这段过程话术被当成最终答复
# 交给用户。它不是答案，却看起来像答案，比明确说"没答出来"更糟。
#
# 修法：与工具请求同时出现的文字标记为 interim，预算耗尽时改交证据摘要（不额外调模型，
# 保持 rounds=1 时"不再请求模型"的既有契约）。
#
# 本测试锁定：
#   1. 过程话术不被当答案交付（改交证据摘要 + 如实标注未整理）
#   2. 真正的终答原样返回，不被改写、不拼证据
#   3. 过程话术且没有任何工具证据 → 如实说没答出来
#   4. 不额外发起 LLM 调用（client 调用次数不变）
#
# 用法: python tests/test_tool_interim_text.py
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


class FakeToolManager:
    """真实 MCPToolManager + 一个假工具：走真实的注册/白名单/执行路径。"""

    def __init__(self):
        from mcp.tool_manager import MCPToolManager, Tool

        async def handler(params, context):
            return {"命中条款": "GB 30871-2022 第 7.2 条：特级动火作业票有效期不超过 8h"}

        self._mgr = MCPToolManager(api_key="test-key")
        self._mgr.register(Tool(
            name="knowledge_search", description="假知识库检索", handler=handler,
            schema={"type": "object", "properties": {"query": {"type": "string"}}},
            cache_ttl=0.0))

    def __getattr__(self, item):
        return getattr(self._mgr, item)


def run_case(responses, max_rounds=1, allowlist=()):
    from core.tool_execution import ToolExecutor
    client = FakeClient(responses)
    ex = ToolExecutor(client, "m", FakeToolManager(), allowlist=list(allowlist),
                      max_rounds=max_rounds)
    res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))
    return res, client


def main():
    # ── 1. 过程话术 + 工具结果 → 交证据，不交话术 ────────────────────────
    res, client = run_case(
        [Resp([text_block("我再补充检索安全阀校验周期的具体依据条款。"),
               tool_block("t1", "knowledge_search", {"query": "安全阀校验周期"})])],
        max_rounds=1, allowlist=("knowledge_search",))
    record("过程话术不被当成答案交付",
           "我再补充检索" not in res.text,
           f"text={res.text[:60]!r}")
    record("改为交付已取到的工具证据",
           "GB 30871-2022" in res.text,
           f"text={res.text[:80]!r}")
    record("如实标注未整理",
           "未能完成最终整理" in res.text, f"text={res.text[:40]!r}")
    record("不额外发起 LLM 调用（保持轮次契约）", client.calls == 1, f"calls={client.calls}")
    record("终止原因与降级标记不变",
           res.terminated == "budget_rounds" and res.degraded is True,
           f"term={res.terminated} degraded={res.degraded}")

    # ── 2. 真终答原样返回 ────────────────────────────────────────────────
    res, _ = run_case([Resp([text_block("动火票分特级/一级/二级，有效期 8h/24h/72h。")])],
                      max_rounds=1, allowlist=())
    record("真终答原样返回（不拼证据、不改写）",
           res.text == "动火票分特级/一级/二级，有效期 8h/24h/72h。",
           f"text={res.text!r}")

    # ── 3. 过程话术且无工具证据 → 如实说没答出来 ────────────────────────
    res, _ = run_case(
        [Resp([text_block("我再确认一下。"),
               tool_block("t2", "not_in_allowlist", {})])],
        max_rounds=1, allowlist=())
    record("过程话术且无证据 → 如实说明未获得终答",
           "我再确认一下" not in res.text and "未获得可用" in res.text,
           f"text={res.text[:60]!r}")

    # ── 4. 有终答的老路径不受影响（degraded 路径之外）──────────────────────
    res, _ = run_case([Resp([text_block("第一轮结论：需按 GB 30871 判定。")])],
                      max_rounds=3, allowlist=())
    record("正常单轮终答 terminated=end_turn",
           res.terminated == "end_turn" and res.degraded is False,
           f"term={res.terminated}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
