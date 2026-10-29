# -*- coding: utf-8 -*-
# 输出预算旋钮回归测试（2026-10-29 闭环第 35 轮）。
#
# 依据：真实 A/B 产物里 20 条回答有 3 条以"（注意：本次回答因达到长度上限被截断…）"结尾
# （15%，且全在判定/分级类问题）；被截断的正文里还残留模型自己的作答提纲。要判断
# "调大输出预算能不能消掉截断"，必须先能**不改代码**地改预算 → SAFETYMIND_MAX_TOKENS。
#
# 本测试锁定：
#   1. 不设环境变量时仍是 1024（逐字节保持旧行为，不悄悄抬高成本）
#   2. 环境变量生效（ToolExecutor 与无工具单轮路径都读）
#   3. 显式传参优先于环境变量（调用方仍可精确控制）
#   4. 非法取值回退 1024，不抛异常
#
# 用法: python tests/test_max_tokens_budget.py
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


class _FakeMessages:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        class _R:
            stop_reason = "end_turn"
            content = []
        return _R()


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


def main():
    from core.tool_execution import ToolExecutor

    saved = os.environ.get("SAFETYMIND_MAX_TOKENS")

    def setenv(v):
        if v is None:
            os.environ.pop("SAFETYMIND_MAX_TOKENS", None)
        else:
            os.environ["SAFETYMIND_MAX_TOKENS"] = str(v)

    try:
        # ── 1. 默认不变 ──────────────────────────────────────────────────
        setenv(None)
        ex = ToolExecutor(client=None, model="m", tool_manager=None, allowlist=[])
        record("未设环境变量 → 默认 1024（旧行为）", ex._max_tokens == 1024,
               f"got={ex._max_tokens}")

        # ── 2. 环境变量生效 ──────────────────────────────────────────────
        setenv(2048)
        ex = ToolExecutor(client=None, model="m", tool_manager=None, allowlist=[])
        record("SAFETYMIND_MAX_TOKENS=2048 → 执行器读到 2048", ex._max_tokens == 2048,
               f"got={ex._max_tokens}")

        # ── 3. 显式传参优先 ──────────────────────────────────────────────
        ex = ToolExecutor(client=None, model="m", tool_manager=None, allowlist=[],
                          max_tokens=512)
        record("显式 max_tokens 优先于环境变量", ex._max_tokens == 512,
               f"got={ex._max_tokens}")

        # ── 4. 非法取值回退 ──────────────────────────────────────────────
        setenv("not-a-number")
        ex = ToolExecutor(client=None, model="m", tool_manager=None, allowlist=[])
        record("非法环境变量回退 1024 且不抛异常", ex._max_tokens == 1024,
               f"got={ex._max_tokens}")

        # ── 5. 无工具单轮路径同样读环境变量 ──────────────────────────────
        from agents.agent_orchestrator import ExpertTask, SafetyCoordinationAgent
        setenv(3072)
        agent = SafetyCoordinationAgent(_FakeClient(), "dummy")
        task = ExpertTask(request_id="r", message="你好",
                          messages=[{"role": "user", "content": "你好"}])
        asyncio.run(agent._call_llm(task))
        got = agent._client.messages.kwargs.get("max_tokens")
        record("无工具单轮路径读到 3072（不再是硬编码 1024）", got == 3072, f"got={got}")

        setenv(None)
        agent2 = SafetyCoordinationAgent(_FakeClient(), "dummy")
        asyncio.run(agent2._call_llm(task))
        got2 = agent2._client.messages.kwargs.get("max_tokens")
        record("无工具单轮路径默认为 1024", got2 == 1024, f"got={got2}")
    finally:
        setenv(saved)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
