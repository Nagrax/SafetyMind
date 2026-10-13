# 员工/专家解耦（OCTO 式）测试：python tests/test_employee_agent.py
# 覆盖：员工层装配等价（ExpertTask.messages 与解耦前各专家自拼逐字节一致、
#       结构化 MemoryContext + RAG 知识块统一装配、文本 context 兜底、
#       MemoryContext 渲染失败降级不阻塞）、CRITICAL 模板先于澄清（铁律 3）、
#       低置信澄清、单专家契约、升级关键词、并行块格式（[agent - 主处理/辅助处理]
#       不变 + 任务包单次装配共享）、专家失败降级 General、专家不注入背景
#       （system 仅自身提示词+动态 Skills）、routing_score 惩罚回路与性能选优、
#       OrchestratorResult 契约字段、run() 意图补全（评测器直调同款）。
# 全部不依赖真实 LLM（意图识别走死端点离线回退，专家 LLM 用桩替换）。
import asyncio
import json
import os
import sys
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEAD = "http://127.0.0.1:9"
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def make_orch():
    from agents.agent_orchestrator import AgentOrchestrator
    return AgentOrchestrator(api_key="dummy", base_url=DEAD, model="dummy")


def stub_llm(agent, fn):
    """替换专家实例的 _call_llm（实例属性遮蔽，handle/统计路径保持真实）。"""
    async def fake(task):
        return fn(task)
    agent._call_llm = fake


def legacy_messages(context, entities, message):
    """解耦前 BaseAgent._call_llm 的装配算法（等价重构的黄金参照）。"""
    msgs = []
    if context:
        msgs.append({"role": "user", "content": f"[背景信息]\n{context}"})
        msgs.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
    if entities:
        msgs.append({"role": "user", "content": f"[结构化实体]\n{json.dumps(entities, ensure_ascii=False)}"})
        msgs.append({"role": "assistant", "content": "好的，我会结合这些结构化实体处理。"})
    msgs.append({"role": "user", "content": message})
    return msgs


def make_memory_context():
    from memory.conversation_memory import MemoryContext, Message, MsgRole
    return MemoryContext(
        recent_messages=[Message(role=MsgRole.USER, content="上次说的反应器还有异响"),
                         Message(role=MsgRole.ASSISTANT, content="已建议停机检查")],
        relevant_history=["3 月曾上报 R-101 振动超标隐患"],
        user_profile={"duty": "操作工", "site": "一车间"},
        summary="员工在跟进反应器异常",
    )


# ── 1. 员工层装配等价 ─────────────────────────────────────────────────────────

def t1_assembly_equivalence():
    from agents.agent_orchestrator import Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    orch = make_orch()
    mem = make_memory_context()
    knowledge = "[知识库检索] 动火作业前需进行气体分析"
    entities = {"equipment_id": ["R-101"], "work_type": ["动火"]}
    req = Request(message="动火作业票怎么办理", user_id="u", conv_id="c",
                  context="旧链路文本", memory_context=mem, knowledge=knowledge,
                  entities=entities, intent=IntentCategory.WORK_PERMIT,
                  urgency=UrgencyLevel.MEDIUM, intent_confidence=0.9)
    task = orch._employee.assemble_task(req)

    expected_context = mem.to_prompt_text() + "\n\n" + knowledge
    ok = (task.context == expected_context
          and task.messages == legacy_messages(expected_context, entities, "动火作业票怎么办理")
          and task.request_id == req.request_id and task.entities == entities)
    record("装配等价：结构化记忆+知识块拼装与旧链路逐字节一致", ok,
           f"context={task.context[:50]!r} messages={task.messages}")

    # 文本 context 兜底（评测器/CLI 直调同款：只传字符串）
    req2 = Request(message="你好", user_id="u", conv_id="c", context="[评测多轮历史]\nuser: 上次问题")
    task2 = orch._employee.assemble_task(req2)
    record("装配等价：无结构化记忆时文本 context 兜底", task2.messages == legacy_messages(
        "[评测多轮历史]\nuser: 上次问题", {}, "你好"))

    # 无上下文无实体 → 只剩用户消息（对抗测试 T1 链路同款）
    req3 = Request(message="救命", user_id="u", conv_id="c")
    task3 = orch._employee.assemble_task(req3)
    record("装配等价：空上下文退化为单条用户消息",
           task3.messages == [{"role": "user", "content": "救命"}])


def t2_memory_render_failure_degrades():
    from agents.agent_orchestrator import Request

    class BadMem:
        def to_prompt_text(self):
            raise RuntimeError("boom")

    orch = make_orch()
    req = Request(message="隐患上报", user_id="u", conv_id="c",
                  context="兜底文本上下文", memory_context=BadMem())
    task = orch._employee.assemble_task(req)
    record("MemoryContext 渲染失败降级回文本上下文（不阻塞）",
           task.messages[0]["content"] == "[背景信息]\n兜底文本上下文",
           f"messages={task.messages}")


# ── 2. 路由决策（CRITICAL 先于澄清，铁律 3）───────────────────────────────────

def t3_critical_precedes_clarification():
    from agents.agent_orchestrator import AgentType, Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    os.environ["SAFETYMIND_CRITICAL_TEMPLATE"] = "1"
    orch = make_orch()
    called = []
    for agents in orch._pool.values():
        for a in agents:
            stub_llm(a, lambda t: called.append(t))

    # "被困/救命"是 CRITICAL 关键词但不在意图词表：LLM 死时意图=OTHER 低置信，
    # 必须返回应急模板而不是澄清追问（与 tests/test_adversarial.py T1 同构）。
    req = Request(message="这边有个人被困在反应釜里了快救命", user_id="t", conv_id="t1",
                  intent=IntentCategory.OTHER, intent_confidence=0.1,
                  urgency=UrgencyLevel.CRITICAL)
    r = asyncio.run(orch.run(req))
    ok = ("紧急事态" in r.response and "不能确定" not in r.response
          and r.escalated is True and r.primary_agent == AgentType.ESCALATION
          and "CRITICAL" in r.routing_reason and r.routing_confidence == 1.0
          and not called)
    record("CRITICAL 模板先于澄清追问（专家零调用，零 LLM 依赖）", ok,
           f"resp={r.response[:40]!r} called={len(called)}")


def t4_clarification_path():
    from agents.agent_orchestrator import AgentType, Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    orch = make_orch()
    req = Request(message="帮我看看这个事情", user_id="u", conv_id="c",
                  intent=IntentCategory.OTHER, intent_confidence=0.2,
                  urgency=UrgencyLevel.MEDIUM)
    r = asyncio.run(orch.run(req))
    record("低置信 OTHER 意图走澄清追问",
           "不能确定" in r.response and r.primary_agent == AgentType.GENERAL
           and r.escalated is False and "低置信度" in r.routing_reason
           and r.routing_confidence == 0.2)


# ── 3. 单专家执行与升级 ───────────────────────────────────────────────────────

def t5_single_expert_contract():
    from agents.agent_orchestrator import AgentType, Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    orch = make_orch()
    stub_llm(orch._pool[AgentType.EQUIPMENT][0], lambda t: "已按预案处置超温报警")

    req = Request(message="反应器超温报警", user_id="u", conv_id="c",
                  intent=IntentCategory.EQUIPMENT_ALARM, intent_group="equipment",
                  urgency=UrgencyLevel.MEDIUM, intent_confidence=0.9)
    r = asyncio.run(orch.run(req))
    ok = (r.response == "已按预案处置超温报警"
          and r.agent_type == AgentType.EQUIPMENT
          and r.primary_agent == AgentType.EQUIPMENT
          and r.agent_types == [AgentType.EQUIPMENT]
          and r.supporting_agents == [] and r.escalated is False
          and "intent=equipment_alarm" in r.routing_reason
          and "primary=equipment" in r.routing_reason
          and r.request_id == req.request_id)
    record("单专家契约：字段与路由理由与解耦前一致", ok,
           f"resp={r.response!r} reason={r.routing_reason}")


def t6_expert_suggests_escalation():
    from agents.agent_orchestrator import AgentType, Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    orch = make_orch()
    stub_llm(orch._pool[AgentType.EQUIPMENT][0], lambda t: "该问题需转人工处理")

    req = Request(message="反应器超温报警", user_id="u", conv_id="c",
                  intent=IntentCategory.EQUIPMENT_ALARM, urgency=UrgencyLevel.MEDIUM,
                  intent_confidence=0.9)
    r = asyncio.run(orch.run(req))
    record("专家输出建议升级 → escalated=True", r.escalated is True)


def t7_expert_failure_degrades_to_general():
    from agents.agent_orchestrator import AgentType, Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    orch = make_orch()

    async def boom(task):
        raise RuntimeError("LLM 端点死亡")
    orch._pool[AgentType.EQUIPMENT][0]._call_llm = boom
    stub_llm(orch._pool[AgentType.GENERAL][0], lambda t: "兜底响应")

    req = Request(message="反应器超温报警", user_id="u", conv_id="c",
                  intent=IntentCategory.EQUIPMENT_ALARM, urgency=UrgencyLevel.MEDIUM,
                  intent_confidence=0.9)
    r = asyncio.run(orch.run(req))
    ok = (r.response == "兜底响应"
          and r.agent_type == AgentType.GENERAL        # 实际执行者（响应来源）
          and r.primary_agent == AgentType.EQUIPMENT   # 路由决策不被降级改写
          and r.escalated is False)
    record("专属专家失败降级 General：响应来源与主处理字段保持旧语义", ok,
           f"resp={r.response!r} agent_type={r.agent_type} primary={r.primary_agent}")


# ── 4. 并行协作（块格式不变 + 任务包单次装配共享）─────────────────────────────

def t8_parallel_block_format():
    from agents.agent_orchestrator import AgentType, Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    orch = make_orch()
    seen_tasks = []
    seen = {"compliance": [], "equipment": []}

    def make_stub(key, reply):
        def fn(task):
            seen_tasks.append(task)
            seen[key].append(task)
            return reply
        return fn
    stub_llm(orch._pool[AgentType.COMPLIANCE][0], make_stub("compliance", "票据办理完成"))
    stub_llm(orch._pool[AgentType.EQUIPMENT][0], make_stub("equipment", "设备处置完成"))

    # 动火作业票（主）+ 反应器报警（辅，equipment_id 实体加分把 equipment 抬过支持阈值；
    # 注意 work_type 实体会给 compliance +0.2 导致辅助阈值不过，故只放 equipment_id）
    req = Request(message="办理动火作业票，另外反应器超温报警了", user_id="u", conv_id="c",
                  intent=IntentCategory.WORK_PERMIT, urgency=UrgencyLevel.MEDIUM,
                  intent_confidence=0.9,
                  entities={"equipment_id": ["R-101"]})
    r = asyncio.run(orch.run(req))
    expected = "[compliance - 主处理]\n票据办理完成\n\n[equipment - 辅助处理]\n设备处置完成"
    ok = (r.response == expected
          and r.primary_agent == AgentType.COMPLIANCE
          and r.supporting_agents == [AgentType.EQUIPMENT]
          and r.agent_types == [AgentType.COMPLIANCE, AgentType.EQUIPMENT]
          and r.escalated is False
          and len(seen_tasks) == 2 and seen_tasks[0] is seen_tasks[1]  # 员工装配一次、共享
          and seen["compliance"] and seen["equipment"])
    record("并行块格式不变 + 任务包单次装配全部专家共享", ok,
           f"resp={r.response!r} types={r.agent_types}")


# ── 5. 专家无状态：不注入背景，system 仅自身提示词+动态 Skills ─────────────────

def t9_expert_stateless_system_prompt():
    from agents.agent_orchestrator import AgentType, Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    class FakeMessages:
        def __init__(self, calls):
            self._calls = calls
        async def create(self, **kw):
            self._calls.append(kw)
            return SimpleNamespace(content=[{"type": "text", "text": "专家回复"}])

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.messages = FakeMessages(self.calls)

    class FakeSkills:
        def prompt_for(self, message, agent_type):
            return "反应器超温处置卡" if agent_type == "equipment" else ""

    orch = make_orch()
    fake_client = FakeClient()
    orch._pool[AgentType.EQUIPMENT][0]._client = fake_client
    orch.set_skill_manager(FakeSkills())

    req = Request(message="反应器超温报警", user_id="u", conv_id="c",
                  intent=IntentCategory.EQUIPMENT_ALARM, urgency=UrgencyLevel.MEDIUM,
                  intent_confidence=0.9)
    r = asyncio.run(orch.run(req))

    kw = fake_client.calls[0] if fake_client.calls else {}
    ok = (r.response == "专家回复"
          and "设备与工艺安全专家" in kw.get("system", "")
          and "[动态 Skills]" in kw.get("system", "")
          and "反应器超温处置卡" in kw.get("system", "")
          and kw.get("messages") == [{"role": "user", "content": "反应器超温报警"}])
    record("专家不注入背景：system=自身提示词+动态 Skills，messages 即员工层任务包", ok,
           f"system={kw.get('system', '')[:60]!r} messages={kw.get('messages')}")


# ── 6. 性能路由与监控惩罚回路 ─────────────────────────────────────────────────

def t10_routing_penalty_loop():
    from agents.agent_orchestrator import AgentType, Request, SafetyCoordinationAgent
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    orch = make_orch()
    orch._pool[AgentType.GENERAL].append(SafetyCoordinationAgent(None, "dummy"))
    tags = {"g0": [], "g1": []}

    def make(tag):
        def fn(task):
            tags[tag].append(task)
            return f"回复来自{tag}"
        return fn
    stub_llm(orch._pool[AgentType.GENERAL][0], make("g0"))
    stub_llm(orch._pool[AgentType.GENERAL][1], make("g1"))

    orch.update_routing_penalties({"general_0": 0.9})   # Monitor 反馈：general_0 降权
    stats = orch.get_stats()
    req = Request(message="你好", user_id="u", conv_id="c",
                  intent=IntentCategory.GREETING, urgency=UrgencyLevel.LOW,
                  intent_confidence=0.9)
    r = asyncio.run(orch.run(req))

    ok = (stats["general_0"]["monitor_penalty"] == 0.9
          and stats["general_1"]["monitor_penalty"] == 0.0
          and stats["general_0"]["routing_score"] < stats["general_1"]["routing_score"]
          and r.response == "回复来自g1" and len(tags["g0"]) == 0)
    record("routing_score 吃 Monitor 惩罚并驱动性能路由选优（get_stats 键位不变）", ok,
           f"stats={stats['general_0']} resp={r.response!r}")


# ── 7. 对外契约与评测器直调 ──────────────────────────────────────────────────

def t11_contract_fields():
    from agents.agent_orchestrator import OrchestratorResult
    names = [f.name for f in fields(OrchestratorResult)]
    expected = ["request_id", "response", "agent_type", "intent", "escalated",
                "latency_ms", "agent_types", "primary_agent", "supporting_agents",
                "routing_reason", "routing_confidence"]
    record("OrchestratorResult 契约字段原样保留", names == expected, f"got={names}")


def t12_run_fills_intent_like_evaluator():
    # 评测器用法（evaluation/evaluator.py:346-353）：只传 message/context/history，
    # 意图由 orchestrator.run 内部补全——死端点下走离线回退必须照常工作。
    os.environ.pop("SAFETYMIND_BGE", None)
    from agents.agent_orchestrator import AgentType, Request

    orch = make_orch()
    stub_llm(orch._pool[AgentType.COMPLIANCE][0], lambda t: "按动火作业票流程办理")
    req = Request(message="动火作业票怎么办理", user_id="eval_user", conv_id="eval_1",
                  context="[评测多轮历史]\nuser: 之前咨询过受限空间")
    r = asyncio.run(orch.run(req))
    ok = (r.intent is not None and r.intent.value == "work_permit"
          and req.intent_confidence > 0      # run() 回填到 Request（评测器同款约定）
          and r.response == "按动火作业票流程办理"
          and r.primary_agent == AgentType.COMPLIANCE)
    record("run() 意图补全（评测器直调同款）离线回退可用", ok,
           f"intent={r.intent} conf={req.intent_confidence} primary={r.primary_agent}")


def main():
    t1_assembly_equivalence()
    t2_memory_render_failure_degrades()
    t3_critical_precedes_clarification()
    t4_clarification_path()
    t5_single_expert_contract()
    t6_expert_suggests_escalation()
    t7_expert_failure_degrades_to_general()
    t8_parallel_block_format()
    t9_expert_stateless_system_prompt()
    t10_routing_penalty_loop()
    t11_contract_fields()
    t12_run_fills_intent_like_evaluator()
    failed = len(RESULTS) - sum(1 for x in RESULTS if x)
    print(f"\n{sum(1 for x in RESULTS if x)}/{len(RESULTS)} PASSED"
          + (f"，失败 {failed} 项" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
