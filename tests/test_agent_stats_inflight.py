# -*- coding: utf-8 -*-
# Agent 统计口径回归测试（2026-10-29 闭环第 34 轮）。
#
# 修复依据（真机日志实测）：
#   跑真实 /chat 时 Monitor 每 10s 刷
#     [ERROR] compliance_0 的 agent_success_rate = 0.000，阈值 0.9
#   而服务其实正常出答案。根因：success_rate = success / total，而 total 在**调用开始**
#   就 +1、success 在返回时才 +1——单次生成 60~120s 时，整个在途期间成功率被算成 0。
#   更糟的是同一错误值经 _routing_penalty 反过来压该 Agent 的路由评分
#   （success_rate < 0.90 直接扣分），等于用"在途"惩罚"正在干活"的 Agent。
#
# 本测试锁定：
#   1. 只有统计到已完成样本才算成功率；没有完成样本 → 1.0（不虚报）
#   2. 在途（total 已计数、尚未返回）不进入成功率分母
#   3. 成功/失败计数在 handle() 的两条路径上都正确
#   4. 在途不会降低路由评分（Monitor 惩罚仍按原语义生效）
#   5. get_stats() 增加 completed/failed/in_flight（additive，旧键位不变）
#
# 用法: python tests/test_agent_stats_inflight.py
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


def main():
    from agents.agent_orchestrator import (AgentStats, AgentType, ExpertTask,
                                           SafetyCoordinationAgent)

    # ── 1. 纯统计口径 ─────────────────────────────────────────────────────
    s = AgentStats(total=1)                      # 已受理、尚未返回
    record("在途调用不计入成功率分母（无完成样本→1.0）",
           s.success_rate == 1.0 and s.in_flight == 1 and s.completed == 0,
           f"rate={s.success_rate} inflight={s.in_flight}")
    s = AgentStats(total=2, success=1, failed=1)
    record("成功1/失败1 → 成功率 0.5、在途 0",
           s.success_rate == 0.5 and s.in_flight == 0, f"rate={s.success_rate}")
    s = AgentStats(total=3, success=2, failed=0)  # 2 完成 + 1 在途
    record("2 成功 + 1 在途 → 成功率仍 1.0",
           s.success_rate == 1.0 and s.in_flight == 1, f"rate={s.success_rate}")
    s = AgentStats(total=1, success=0, failed=1)
    record("调用真失败 → 成功率 0.0（不是被在途掩盖）",
           s.success_rate == 0.0 and s.completed == 1, f"rate={s.success_rate}")
    record("avg_ms 只按已完成调用摊",
           AgentStats(total=2, success=1, failed=0, total_ms=100.0).avg_ms == 100.0)

    # ── 2. handle() 两条路径的计数 ────────────────────────────────────────
    agent = SafetyCoordinationAgent(None, "dummy")   # client=None：只测统计，不真调模型
    task = ExpertTask(request_id="r1", message="你好", messages=[{"role": "user", "content": "你好"}])

    async def fake_ok(t):
        return "好的"
    agent._call_llm = fake_ok
    r = asyncio.run(agent.handle(task))
    record("handle 成功路径：success+1 且 rate=1.0",
           r.success is True and agent.stats.success == 1 and agent.stats.failed == 0
           and agent.stats.success_rate == 1.0,
           f"stats={agent.stats}")

    async def fake_boom(t):
        raise RuntimeError("端点 502")
    agent._call_llm = fake_boom
    r2 = asyncio.run(agent.handle(task))
    record("handle 失败路径：failed+1、成功率 0.5",
           r2.success is False and agent.stats.failed == 1
           and agent.stats.success_rate == 0.5 and agent.stats.in_flight == 0,
           f"stats={agent.stats}")

    # ── 3. 在途不压路由评分 ──────────────────────────────────────────────
    idle = AgentStats(total=0)
    inflight = AgentStats(total=1)                 # 一个请求正在跑
    record("在途调用的路由评分不低于空闲（不被误判为失败而降权）",
           inflight.routing_score() >= idle.routing_score() - 1e-9,
           f"inflight={inflight.routing_score():.3f} idle={idle.routing_score():.3f}")
    penalized = AgentStats(total=1, monitor_penalty=0.5)
    record("Monitor 惩罚仍按原语义降低路由评分",
           penalized.routing_score() < idle.routing_score(),
           f"penalized={penalized.routing_score():.3f}")

    # ── 4. get_stats additive 契约 ────────────────────────────────────────
    from agents.agent_orchestrator import AgentOrchestrator
    # 只验证键位：构造最小 orchestrator（不启动任何外部连接）
    try:
        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        orch._pool = {AgentType.GENERAL: [SafetyCoordinationAgent(None, "dummy")]}
        orch._pool[AgentType.GENERAL][0].stats = AgentStats(total=2, success=1, failed=1)
        st = orch.get_stats()["general_0"]
        record("get_stats 暴露 completed/failed/in_flight 且旧键位不变",
               {"total", "success_rate", "avg_ms", "monitor_penalty", "routing_score",
                "completed", "failed", "in_flight"} <= set(st)
               and st["completed"] == 2 and st["failed"] == 1 and st["in_flight"] == 0
               and st["success_rate"] == 0.5,
               f"keys={sorted(st)}")
    except Exception as ex:
        record("get_stats 暴露 completed/failed/in_flight 且旧键位不变", False, repr(ex))

    # ── 5. 工具统计同一口径（ToolStats）──────────────────────────────────
    from mcp.tool_manager import ToolStats
    t = ToolStats(total=1)                        # 慢工具在途
    record("工具在途不计入成功率分母",
           t.success_rate == 1.0 and t.in_flight == 1, f"rate={t.success_rate}")
    t = ToolStats(total=3, success=2, failed=1)   # 2 成功 1 失败 0 在途
    record("工具 2 成功 1 失败 → 0.667",
           abs(t.success_rate - 2 / 3) < 1e-6 and t.in_flight == 0,
           f"rate={t.success_rate}")
    record("工具 avg_latency 只按已完成摊",
           ToolStats(total=2, success=1, total_latency_ms=50.0).avg_latency_ms == 50.0)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
