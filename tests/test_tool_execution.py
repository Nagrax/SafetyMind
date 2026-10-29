# -*- coding: utf-8 -*-
# P0-A 真实工具执行回路测试：core/tool_execution.py 的 ToolExecutor 与
# BaseAgent/AgentOrchestrator 工具接线（假 LLM 客户端 + 真实 MCPToolManager，
# 不联网、不下模型）。
# 覆盖：schema 暴露 → tool_use 执行（经 ToolManager）→ 匹配 tool_use_id 回填
#       tool_result → 继续调用 → end_turn 终答；只读白名单越权拒绝（不执行）；
#       工具失败/超时/熔断的有界纠正与 degraded 如实标记（降级说明不当证据）；
#       max_calls 双预算；max_tokens 截断与成功终答区分；服务端不支持 tools
#       时降级为无工具单轮；BaseAgent 不接工具时行为与改造前逐字节一致；
#       接工具时走工具回路；run_parallel 汇总 tool_trace（主在前辅在后）。
# 用法: python tests/test_tool_execution.py
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEAD = "http://127.0.0.1:9"
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ── 假基础设施：脚本化 LLM 客户端 + 真实 MCPToolManager ──────────────────────

class FakeMessages:
    """按脚本顺序返回响应；记录每次调用 kwargs。fail_with_tools 模拟服务端
    不支持 tools（带 tools 参数时抛错，不带时正常返回）。"""

    def __init__(self, script, fail_with_tools=None):
        self._script = list(script)
        self.calls = []
        self._fail_with_tools = fail_with_tools

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_with_tools is not None and kwargs.get("tools"):
            raise self._fail_with_tools
        if not self._script:
            raise AssertionError("假客户端脚本已耗尽（疑似超出预期轮数）")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script, fail_with_tools=None):
        self.messages = FakeMessages(script, fail_with_tools)
        self.calls = self.messages.calls


def resp(blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


def text_block(text):
    return {"type": "text", "text": text}


def tool_block(tool_use_id, name, params):
    return {"type": "tool_use", "id": tool_use_id, "name": name, "input": params}


def make_tool(name, handler, schema=None, **kwargs):
    from mcp.tool_manager import Tool
    return Tool(
        name=name,
        description=f"假工具 {name}",
        handler=handler,
        schema=schema or {"type": "object", "properties": {"query": {"type": "string"}}},
        **kwargs,
    )


def make_manager(*tools):
    from mcp.tool_manager import MCPToolManager
    tm = MCPToolManager(api_key="test-key")
    for tool in tools:
        tm.register(tool)
    return tm


def tool_result_of(call_kwargs):
    """取某次 create 调用消息里最后一条 tool_result 块（无则返回 {}）。"""
    messages = call_kwargs.get("messages") or []
    if not messages:
        return {}
    content = messages[-1].get("content")
    if not isinstance(content, list) or not content:
        return {}
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return block
    return {}


# ── 1. 端到端影子链路：真调用 ToolManager + 匹配 ID 回填 + end_turn 终答 ─────

def t1_end_to_end_tool_loop():
    from core.tool_execution import ToolExecutor

    seen = []

    async def handler(params, context):
        seen.append((params, context))
        return [{"title": "动火作业安全规程", "content": "动火前必须进行气体分析", "score": 0.91}]

    tm = make_manager(make_tool(
        "knowledge_search", handler,
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        cache_ttl=0.0))
    client = FakeClient([
        resp([tool_block("tu_1", "knowledge_search", {"query": "动火作业气体分析"})]),
        resp([text_block("根据《动火作业安全规程》，动火前必须进行气体分析。")]),
    ])
    ex = ToolExecutor(client, "fake-model", tm,
                      allowlist=["knowledge_search"],
                      exec_context={"user_id": "u1"})
    res = asyncio.run(ex.run(system="sys", messages=[{"role": "user", "content": "动火前要做什么"}]))
    rec = res.tool_trace[0] if res.tool_trace else None

    record("工具被真实执行（ToolManager 收到参数与上下文）",
           len(seen) == 1 and seen[0][0] == {"query": "动火作业气体分析"}
           and seen[0][1] == {"user_id": "u1"},
           f"seen={seen}")
    record("tool_schemas 按 allowlist 暴露 name/description/input_schema",
           [s["name"] for s in ex.tool_schemas()] == ["knowledge_search"]
           and ex.tool_schemas()[0]["input_schema"] == tm._tools["knowledge_search"].schema,
           f"schemas={ex.tool_schemas()}")
    record("tool_result 以匹配 tool_use_id 回填且非错误、携带工具结果",
           tool_result_of(client.calls[1]).get("tool_use_id") == "tu_1"
           and tool_result_of(client.calls[1]).get("is_error") is False
           and "动火作业安全规程" in str(tool_result_of(client.calls[1]).get("content")),
           f"tr={tool_result_of(client.calls[1])}")
    record("assistant 消息回填了 tool_use 块（协议完整）",
           client.calls[1]["messages"][-2].get("role") == "assistant"
           and any(b.get("type") == "tool_use" for b in client.calls[1]["messages"][-2]["content"]))
    record("终答为 end_turn 且非降级、text 即最终回答",
           res.terminated == "end_turn" and res.degraded is False and res.rounds == 2
           and res.calls == 1 and res.text == "根据《动火作业安全规程》，动火前必须进行气体分析。",
           f"term={res.terminated} rounds={res.rounds} text={res.text!r}")
    record("tool_trace 记录工具名/参数/成功/耗时/结果预览",
           rec is not None and rec.name == "knowledge_search"
           and rec.params == {"query": "动火作业气体分析"} and rec.ok is True
           and rec.error is None and isinstance(rec.latency_ms, float) and rec.latency_ms >= 0
           and "气体分析" in rec.result_preview,
           f"rec={rec}")


# ── 2. 只读白名单：越权请求不执行 + is_error 回填 + trace ok=False ───────────

def t2_allowlist_enforced():
    from core.tool_execution import ToolExecutor

    executed = []

    async def handler(params, context):
        executed.append(params)
        return {"ok": True}

    tm = make_manager(
        make_tool("knowledge_search", handler),
        make_tool("ledger_checklist", handler),
    )
    client = FakeClient([
        resp([tool_block("tu_w", "ledger_checklist", {"query": "写入台账"})]),
        resp([text_block("仅以只读工具可提供的信息作答。")]),
    ])
    ex = ToolExecutor(client, "m", tm, allowlist=["knowledge_search"])
    res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))
    rec = res.tool_trace[0] if res.tool_trace else None
    tr = tool_result_of(client.calls[1])

    record("白名单外工具不被执行（handler 零调用）", executed == [], f"executed={executed}")
    record("白名单外工具回填 is_error=True", tr.get("tool_use_id") == "tu_w" and tr.get("is_error") is True,
           f"tr={tr}")
    record("白名单外工具 trace 记 ok=False 且原因明确",
           rec is not None and rec.ok is False and rec.name == "ledger_checklist"
           and "白名单" in (rec.error or ""),
           f"rec={rec}")
    record("白名单外工具仍能给出可用终答", res.terminated == "end_turn"
           and res.text == "仅以只读工具可提供的信息作答。")


# ── 3. 工具失败/超时/熔断：有界纠正 + degraded 如实标记 + 降级说明不当证据 ────

def _failure_case(make_failing_tool):
    from core.tool_execution import ToolExecutor

    tm = make_manager(make_failing_tool())
    client = FakeClient([
        resp([tool_block("tu_f", "knowledge_search", {"query": "泄漏应急"})]),
        resp([text_block("未检索到可用证据，无法给出结论，建议转人工核实。")]),
    ])
    ex = ToolExecutor(client, "m", tm, allowlist=["knowledge_search"])
    res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "泄漏怎么办"}]))
    return client, res


def t3_tool_failure_bounded_and_degraded():
    async def boom(params, context):
        raise RuntimeError("模拟工具异常")

    client, res = _failure_case(lambda: make_tool("knowledge_search", boom, fallback=None))
    rec = res.tool_trace[0] if res.tool_trace else None
    tr = tool_result_of(client.calls[1])

    record("工具异常：bounded 纠正（恰好 2 轮，不无限循环）",
           len(client.calls) == 2 and res.rounds == 2,
           f"calls={len(client.calls)} rounds={res.rounds}")
    record("工具异常：trace ok=False + 错误原因 + degraded=True",
           rec is not None and rec.ok is False and rec.error and res.degraded is True
           and res.terminated == "end_turn",
           f"rec={rec} degraded={res.degraded} term={res.terminated}")
    record("工具失败回填 is_error 且错误文本进入 tool_result（不伪造证据）",
           tr.get("is_error") is True and "异常" in str(tr.get("content"))
           and "动火" not in str(tr.get("content")),
           f"tr={tr}")


def t3b_tool_timeout_degraded():
    async def slow(params, context):
        await asyncio.sleep(0.3)
        return [{"title": "不该被取到", "content": "x"}]

    client, res = _failure_case(lambda: make_tool(
        "knowledge_search", slow, timeout_s=0.05, fallback=None))
    rec = res.tool_trace[0] if res.tool_trace else None
    tr = tool_result_of(client.calls[1])

    record("工具超时：trace ok=False、错误为超时、degraded=True",
           rec is not None and rec.ok is False and "超时" in (rec.error or "")
           and res.degraded is True,
           f"rec={rec} degraded={res.degraded}")
    record("工具超时：不把降级说明当检索证据（is_error 且无伪造结果）",
           tr.get("is_error") is True and "不该被取到" not in str(tr.get("content")),
           f"tr={tr}")


# ── 4. 双预算：max_calls=1 时连续请求两个工具 → 只执行 1 次 ──────────────────

def t4_call_budget():
    from core.tool_execution import ToolExecutor

    executed = []

    async def handler(params, context):
        executed.append(params)
        return {"items": []}

    tm = make_manager(make_tool("knowledge_search", handler), make_tool("resource_search", handler))
    client = FakeClient([
        resp([tool_block("t1", "knowledge_search", {"query": "a"}),
              tool_block("t2", "resource_search", {"query": "b"})]),
    ])
    ex = ToolExecutor(client, "m", tm,
                      allowlist=["knowledge_search", "resource_search"], max_calls=1)
    res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    record("max_calls=1：只执行 1 次工具调用", len(executed) == 1 and res.calls == 1,
           f"executed={executed} calls={res.calls}")
    record("max_calls 超限即 terminating=budget_calls 且 degraded=True",
           res.terminated == "budget_calls" and res.degraded is True,
           f"term={res.terminated} degraded={res.degraded}")
    record("超预算而未执行的调用同样进 trace（可追溯 ok=False）",
           len(res.tool_trace) == 2 and res.tool_trace[1].ok is False
           and "预算" in (res.tool_trace[1].error or ""),
           f"trace={res.tool_trace}")
    record("max_calls 超限后不再请求模型（恰好 1 轮）", len(client.calls) == 1, f"calls={len(client.calls)}")


def t4b_round_budget():
    from core.tool_execution import ToolExecutor

    async def handler(params, context):
        return {"items": [1]}

    tm = make_manager(make_tool("knowledge_search", handler))
    client = FakeClient([resp([tool_block("t1", "knowledge_search", {"query": "a"})])])
    ex = ToolExecutor(client, "m", tm, allowlist=["knowledge_search"], max_rounds=1)
    res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    record("max_rounds=1：执行工具后不能再取得终答 → budget_rounds + degraded",
           res.terminated == "budget_rounds" and res.degraded is True and res.calls == 1
           and res.rounds == 1 and len(client.calls) == 1,
           f"term={res.terminated} rounds={res.rounds} degraded={res.degraded}")
    record("budget_rounds 以可用文本收尾（不抛异常、不伪装成功终答）",
           bool(res.text) and not (res.terminated == "end_turn" and res.degraded is False),
           f"text={res.text!r}")


# ── 5. max_tokens 截断：degraded 且与成功终答区分 ─────────────────────────────
def t5_max_tokens_is_not_success():
    from core.tool_execution import ToolExecutor

    tm = make_manager()
    client = FakeClient([resp([text_block("部分回答")], stop_reason="max_tokens")])
    ex = ToolExecutor(client, "m", tm, allowlist=[])
    res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    record("max_tokens 截断：degraded=True 且在 text 里如实说明",
           res.degraded is True and "截断" in res.text and res.text.startswith("部分回答"),
           f"text={res.text!r} degraded={res.degraded}")
    record("max_tokens 截断与成功终答区分（不判为 end_turn 成功）",
           not (res.terminated == "end_turn" and res.degraded is False),
           f"term={res.terminated}")


# ── 6. 服务端不支持 tools：降级为无工具单轮，仍返回可用文本 ───────────────────

def t6_tools_unsupported_degrades():
    from core.tool_execution import ToolExecutor

    async def handler(params, context):
        return {"items": []}

    tm = make_manager(make_tool("knowledge_search", handler))
    client = FakeClient(
        [resp([text_block("无工具模式下的回答")])],
        fail_with_tools=TypeError("unexpected keyword argument 'tools'"))
    ex = ToolExecutor(client, "m", tm, allowlist=["knowledge_search"])
    res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))

    record("服务端不支持 tools：terminated=tools_unsupported 且 degraded=True",
           res.terminated == "tools_unsupported" and res.degraded is True,
           f"term={res.terminated} degraded={res.degraded}")
    record("服务端不支持 tools：仍返回可用文本（对话不失败）",
           res.text == "无工具模式下的回答" and res.calls == 0,
           f"text={res.text!r}")
    record("降级调用不带 tools 且沿用同一 system/messages",
           len(client.calls) == 2 and "tools" not in client.calls[1]
           and client.calls[1]["messages"] == [{"role": "user", "content": "q"}]
           and client.calls[1]["system"] == "s",
           f"second={client.calls[1]}")


def t6b_degrade_failure_is_error():
    from core.tool_execution import ToolExecutor

    async def handler(params, context):
        return {"items": []}

    tm = make_manager(make_tool("knowledge_search", handler))
    # 首轮带 tools 抛错触发降级；降级后的无工具调用也失败（脚本项即异常）。
    client = FakeClient([TypeError("无工具降级调用仍失败")],
                        fail_with_tools=TypeError("unexpected keyword argument 'tools'"))
    ex = ToolExecutor(client, "m", tm, allowlist=["knowledge_search"])
    res = asyncio.run(ex.run(system="s", messages=[{"role": "user", "content": "q"}]))
    record("无工具降级也失败时 terminated=error 且 degraded=True（不伪装成功）",
           res.terminated == "error" and res.degraded is True and bool(res.error),
           f"term={res.terminated} error={res.error}")


# ── 7. BaseAgent：不接工具时行为与改造前一致；接工具时走工具回路 ──────────────

def _expert_task():
    from agents.agent_orchestrator import ExpertTask
    return ExpertTask(request_id="r1", message="反应器超温报警",
                      messages=[{"role": "user", "content": "反应器超温报警"}])


def t7_base_agent_legacy_path_unchanged():
    from agents.agent_orchestrator import EquipmentSafetyAgent

    client = FakeClient([resp([text_block("旧路径回答")])])
    agent = EquipmentSafetyAgent(client, "m")          # tool_executor 默认 None
    out = asyncio.run(agent.handle(_expert_task()))

    kw = client.calls[0] if client.calls else {}
    record("tool_executor=None：只调用一次 create 且不传 tools（旧行为逐字节一致）",
           out.content == "旧路径回答" and out.success is True
           and len(client.calls) == 1 and "tools" not in kw
           and kw.get("messages") == [{"role": "user", "content": "反应器超温报警"}],
           f"calls={len(client.calls)} kw_keys={sorted(kw)}")
    record("tool_executor=None：不产生 tool_trace",
           list(out.tool_trace) == [] and list(agent._last_tool_trace) == [],
           f"trace={out.tool_trace}")


def t8_base_agent_tool_path():
    from agents.agent_orchestrator import EquipmentSafetyAgent
    from core.tool_execution import ToolExecutor

    async def handler(params, context):
        return [{"title": "反应器超温处置卡", "content": "立即降负荷并检查冷却水"}]

    tm = make_manager(make_tool("knowledge_search", handler))
    client = FakeClient([
        resp([tool_block("tu_e", "knowledge_search", {"query": "反应器超温处置"})]),
        resp([text_block("按处置卡：立即降负荷并检查冷却水。")]),
    ])
    ex = ToolExecutor(client, "m", tm, allowlist=["knowledge_search"])
    agent = EquipmentSafetyAgent(client, "m", None, ex)
    out = asyncio.run(agent.handle(_expert_task()))

    record("BaseAgent 接 executor：走工具回路（2 轮）并返回模型终答",
           len(client.calls) == 2 and out.content == "按处置卡：立即降负荷并检查冷却水。"
           and out.success is True,
           f"calls={len(client.calls)} content={out.content!r}")
    record("tool_trace 挂到 AgentResponse 与 agent._last_tool_trace",
           len(out.tool_trace) == 1 and out.tool_trace[0].name == "knowledge_search"
           and agent._last_tool_trace == out.tool_trace,
           f"trace={out.tool_trace}")


# ── 8. 员工层：run_parallel 汇总 tool_trace（主在前辅在后）───────────────────

def t9_parallel_trace_aggregation():
    from agents.agent_orchestrator import (AgentType, ComplianceEmergencyAgent, EmployeeAgent,
                                           EquipmentSafetyAgent, Request, RoutingDecision,
                                           SafetyCoordinationAgent)
    from core.intent_recognizer import IntentCategory, UrgencyLevel
    from core.tool_execution import ToolCallRecord

    def rec(tid, name):
        return ToolCallRecord(tool_use_id=tid, name=name, params={}, ok=True,
                              error=None, latency_ms=1.0, result_preview="")

    pool = {
        AgentType.GENERAL: [SafetyCoordinationAgent(None, "m")],
        AgentType.EQUIPMENT: [EquipmentSafetyAgent(None, "m")],
        AgentType.COMPLIANCE: [ComplianceEmergencyAgent(None, "m")],
        AgentType.ESCALATION: [],
    }
    primary, support = pool[AgentType.COMPLIANCE][0], pool[AgentType.EQUIPMENT][0]
    primary._last_tool_trace = [rec("p1", "ledger_checklist")]
    support._last_tool_trace = [rec("s1", "knowledge_search")]

    def stub(reply):
        async def fn(task):
            return reply
        return fn
    primary._call_llm = stub("票据办理完成")
    support._call_llm = stub("设备处置完成")

    employee = EmployeeAgent(pool)
    decision = RoutingDecision(primary_agent=AgentType.COMPLIANCE,
                               supporting_agents=[AgentType.EQUIPMENT], reason="r", confidence=0.9)
    req = Request(message="办理动火作业票，另外反应器超温报警了", user_id="u", conv_id="c",
                  intent=IntentCategory.WORK_PERMIT, urgency=UrgencyLevel.MEDIUM,
                  intent_confidence=0.9)
    result = asyncio.run(employee.run_parallel(req, decision))

    names = [t.name for t in result.tool_trace]
    record("run_parallel 汇总 tool_trace（主在前、辅在后）",
           names == ["ledger_checklist", "knowledge_search"], f"names={names}")
    record("并行响应块格式与既有字段不受影响",
           result.response == "[compliance - 主处理]\n票据办理完成\n\n[equipment - 辅助处理]\n设备处置完成",
           f"resp={result.response!r}")


# ── 9. 门面接线：构造函数与 set_tool_executor 写入全部专家 ────────────────────

def t10_orchestrator_wiring():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel
    from core.tool_execution import ToolExecutor

    async def handler(params, context):
        return [{"title": "超温处置卡", "content": "降负荷"}]

    tm = make_manager(make_tool("knowledge_search", handler))
    client = FakeClient([
        resp([tool_block("tu_o", "knowledge_search", {"query": "超温处置"})]),
        resp([text_block("依据处置卡：立即降负荷。")]),
    ])
    ex = ToolExecutor(client, "m", tm, allowlist=["knowledge_search"])

    orch = AgentOrchestrator(api_key="dummy", base_url=DEAD, model="dummy", tool_executor=ex)
    assigned = [a._tool_executor for agents in orch._pool.values() for a in agents]
    record("AgentOrchestrator(tool_executor=...) 写入池内全部专家",
           assigned == [ex] * 4, f"assigned={assigned}")

    orch.set_tool_executor(None)
    record("set_tool_executor 运行时替换（写进每个 agent）",
           all(a._tool_executor is None for agents in orch._pool.values() for a in agents))
    orch.set_tool_executor(ex)

    req = Request(message="反应器超温报警", user_id="u", conv_id="c",
                  intent=IntentCategory.EQUIPMENT_ALARM, urgency=UrgencyLevel.MEDIUM,
                  intent_confidence=0.9)
    result = asyncio.run(orch.run(req))
    record("门面 run() 经员工层→专家→工具回路，OrchestratorResult.tool_trace 可见",
           result.response == "依据处置卡：立即降负荷。"
           and [t.name for t in result.tool_trace] == ["knowledge_search"]
           and bool(orch.get_stats()),   # 统计回路不受影响
           f"resp={result.response!r} trace={[t.name for t in result.tool_trace]}")


def main():
    for fn in [t1_end_to_end_tool_loop, t2_allowlist_enforced,
               t3_tool_failure_bounded_and_degraded, t3b_tool_timeout_degraded,
               t4_call_budget, t4b_round_budget, t5_max_tokens_is_not_success,
               t6_tools_unsupported_degrades, t6b_degrade_failure_is_error,
               t7_base_agent_legacy_path_unchanged, t8_base_agent_tool_path,
               t9_parallel_trace_aggregation, t10_orchestrator_wiring]:
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {type(ex).__name__}: {ex}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} PASSED")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
