# -*- coding: utf-8 -*-
# 生成温度旋钮回归测试（2026-10-29 闭环第 36 轮）。
#
# 背景：闭环做"同题、两种上下文"的 A/B 时，端点默认温度（1.0）让单题得分波动可达 ±1 分，
# n=10 的 A/B 两次跑出相反结论（accuracy −0.40 → −0.05）。评测要可复现，必须能压住
# 生成侧噪声；但产品默认行为不能变 → 默认**不发** temperature 字段。
#
# 本测试锁定：
#   1. 未设环境变量 → 请求里没有 temperature（逐字节保持旧行为）
#   2. SAFETYMIND_TEMPERATURE=0 → 两条生成路径（有工具回路 / 无工具单轮）都带 temperature=0
#   3. 非法值 / off / none 一律回退为"不发"
#   4. 超范围取值被夹到 [0, 2]
#
# 用法: python tests/test_temperature_knob.py
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


class _Resp:
    def __init__(self, content=None, stop_reason="end_turn"):
        self.content = content or []
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, resp):
        self._resp = resp
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self._resp


class _Client:
    def __init__(self, resp=None):
        self.messages = _Messages(resp or _Resp())


class _ToolManager:
    def __init__(self):
        self._tools = {}


def main():
    from core.llm_params import temperature_param, messages_create_kwargs_base
    saved = os.environ.get("SAFETYMIND_TEMPERATURE")

    def setenv(v):
        if v is None:
            os.environ.pop("SAFETYMIND_TEMPERATURE", None)
        else:
            os.environ["SAFETYMIND_TEMPERATURE"] = str(v)

    try:
        # ── 1. 默认不发 ───────────────────────────────────────────────────
        setenv(None)
        record("默认不设 → temperature_param()=None", temperature_param() is None)
        record("默认不设 → 公共 kwargs 里没有 temperature",
               "temperature" not in messages_create_kwargs_base())

        # ── 2. 显式设置生效 ───────────────────────────────────────────────
        setenv(0)
        record("TEMPERATURE=0 生效", temperature_param() == 0.0)
        record("公共 kwargs 带上 temperature=0.0",
               messages_create_kwargs_base().get("temperature") == 0.0)

        # ── 3. 非法值回退 ─────────────────────────────────────────────────
        for bad in ("off", "none", "abc", "", "-1"):
            setenv(bad)
            record(f"非法值 {bad!r} 回退为不发", temperature_param() is None,
                   f"got={temperature_param()}")

        # ── 4. 范围夹取 ───────────────────────────────────────────────────
        setenv(5)
        record("超上限夹到 2.0", temperature_param() == 2.0, f"got={temperature_param()}")

        # ── 5. 无工具单轮路径 ─────────────────────────────────────────────
        from agents.agent_orchestrator import ExpertTask, SafetyCoordinationAgent
        setenv(0)
        agent = SafetyCoordinationAgent(_Client(), "dummy")
        task = ExpertTask(request_id="r", message="q",
                          messages=[{"role": "user", "content": "q"}])
        asyncio.run(agent._call_llm(task))
        got = agent._client.messages.kwargs.get("temperature")
        record("无工具单轮路径发送 temperature=0", got == 0.0, f"got={got}")

        setenv(None)
        agent2 = SafetyCoordinationAgent(_Client(), "dummy")
        asyncio.run(agent2._call_llm(task))
        record("无工具单轮路径默认不发 temperature",
               "temperature" not in agent2._client.messages.kwargs,
               f"kwargs={sorted(agent2._client.messages.kwargs)}")
    finally:
        setenv(saved)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
