# -*- coding: utf-8 -*-
# 多 Agent 协作路由回归测试（2026-10-26 闭环第 2 轮）。
#
# 修复的缺陷（实测证据见 scratch/sm-loop/runs/multi_agent_diagnosis.json）：
#   原实现辅助专家判据是 `score >= 0.45 and score >= primary_score * 0.55`，
#   而意图命中直接加 0.75、关键词最多再加 0.45，导致主专家 ~1.2、辅助上限 0.45，
#   **数学上永远不可能满足** → 12 条复合问题触发率 0%，并行多 Agent 是死代码。
#
# 新判据（三档，刻意保守）：
#   1) 意图本身就是该领域 → 协作
#   2) 命中 1 个强域标志词（"动火""联锁"这类无歧义词）→ 协作
#   3) 命中 >=2 个普通关键词 → 协作
#   单命中一个普通词（如只有"泄漏"）不拉第二个专家——过度并行让每次请求多一次完整 LLM 调用。
#
# 用法: python tests/test_multi_agent_collaboration.py
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


def make(req_cls):
    from agents.agent_orchestrator import AgentOrchestrator
    orch = AgentOrchestrator(api_key="dummy", base_url="http://127.0.0.1:9", model="dummy")
    return orch._employee, req_cls


def main():
    from agents.agent_orchestrator import Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    emp, _ = make(Request)

    def decision(msg, intent, urgency=UrgencyLevel.MEDIUM, entities=None):
        req = Request(message=msg, user_id="u", conv_id="c", intent=intent,
                      intent_confidence=0.9, urgency=urgency, entities=entities or {})
        return emp._route_decision(req)

    # ── 1. 典型复合问题必须触发协作（这是修复的核心目标）────────────────
    cases = [
        ("反应釜超温报警跳车了，需要动火检修，动火票怎么办？", IntentCategory.EQUIPMENT_ALARM, "compliance"),
        ("泵体泄漏，现场需要受限空间作业清理，安全措施有哪些？", IntentCategory.WORK_PERMIT, "equipment"),
        ("管道超压联锁动作，检修时需要办什么作业票？", IntentCategory.EQUIPMENT_ALARM, "compliance"),
    ]
    for msg, intent, expect in cases:
        d = decision(msg, intent)
        got = [a.value for a in d.supporting_agents]
        record(f"复合问题触发协作：{msg[:18]}…", expect in got,
               f"primary={d.primary_agent.value} supporting={got} 期望含 {expect}")

    # ── 2. 单一领域问题不得过度协作（避免延迟翻倍）──────────────────────
    singles = [
        ("动火作业票的有效期是怎么规定的？", IntentCategory.WORK_PERMIT),
        ("反应釜温度联锁频繁跳车，可能是什么原因？", IntentCategory.EQUIPMENT_ALARM),
        ("安全生产法对主要负责人职责怎么规定的？", IntentCategory.REGULATION_QUERY),
        ("你好", IntentCategory.GREETING),
    ]
    for msg, intent in singles:
        d = decision(msg, intent)
        got = [a.value for a in d.supporting_agents]
        record(f"单一领域不拉第二个专家：{msg[:18]}…", len(got) == 0,
               f"primary={d.primary_agent.value} supporting={got}")

    # ── 3. 辅助专家数量上限 2（延迟与完整性的显式权衡）──────────────────
    d = decision("反应釜超温联锁跳车，泵振动超标，需要动火作业票和受限空间作业，还要高处作业审批",
                 IntentCategory.EQUIPMENT_ALARM)
    record("辅助专家数量不超过 2（延迟上限）", len(d.supporting_agents) <= 2,
           f"supporting={[a.value for a in d.supporting_agents]}")

    # ── 4. GENERAL 不构成"专家协作"──────────────────────────────────────
    d = decision("车间噪声 88 分贝，需要给员工做哪些防护？", IntentCategory.GENERAL_CONSULT)
    record("GENERAL 不进入 supporting_agents", all(a.value != "general" for a in d.supporting_agents),
           f"supporting={[a.value for a in d.supporting_agents]}")

    # ── 5. CRITICAL 仍走升级短路（不得被协作逻辑改变）───────────────────
    d = decision("车间氨气泄漏有人中毒了，需要动火吗", IntentCategory.EMERGENCY_RESPONSE,
                 urgency=UrgencyLevel.CRITICAL)
    record("CRITICAL 优先升级路由，不进入协作分支",
           d.primary_agent.value == "escalation" and not d.supporting_agents,
           f"primary={d.primary_agent.value} supporting={[a.value for a in d.supporting_agents]}")

    # ── 6. multi_agent 属性语义正确 ─────────────────────────────────────
    d_multi = decision("管道超压联锁动作，检修时需要办什么作业票？", IntentCategory.EQUIPMENT_ALARM)
    d_single = decision("你好", IntentCategory.GREETING)
    record("multi_agent 属性与 supporting_agents 一致",
           d_multi.multi_agent is True and d_single.multi_agent is False,
           f"multi={d_multi.multi_agent} single={d_single.multi_agent}")

    # ── 7. agent_types 顺序：主在前、辅在后 ─────────────────────────────
    record("agent_types 主专家在前", d_multi.agent_types[0] == d_multi.primary_agent,
           f"agent_types={[a.value for a in d_multi.agent_types]}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
