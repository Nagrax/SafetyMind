# -*- coding: utf-8 -*-
# SM-31 上下文窗口与受权证据指针回归测试（2026-11-08）。
#
# 任务卡（任务清单.json id=SM-31，gated）：保留当前请求、最近关键轮、否决及关键
# SOP/条款证据；其余使用具备会话/主体边界的有界引用；展开沿用工具轮次预算；
# 禁止引用自动授权写操作；新策略独立开关（SAFETYMIND_CONTEXT_WINDOW，默认 off）。
#
# 锁定的契约：
#   t0 开关缺省/off：记忆块渲染与现行为逐字节一致（零退化护栏）
#   t1 窗口策略：摘要/画像/偏好卡/最近 N 轮全文保留；更早普通轮与跨会话相关历史
#      变成有界引用短行（不内联正文），块体积下降
#   t2 否决（veto/escalated）证据无视窗口永远全文保留
#   t3 指针访问矩阵：同主体同会话命中；跨主体/跨会话/未知/过期/篡改/读取失败
#      全部 fail-closed 且理由如实（不编造内容）
#   t4 自动授权写操作拒绝登记（fail-closed：库里不存在即永不可引用）
#   t5 展开走 ToolExecutor 白名单+双预算；权限拒绝与超预算如实回填
#   t6 per-expert 实际使用的 pointer_id 归集与落盘；数据缺失时诚实空结果
#   t7 handle() 接线：结果携带 evidence_pointer_uses
#   t8 窗口渲染异常回退旧渲染（绝不阻塞对话）
#   t9 scope 绑定：主体/会话来自服务端 contextvar（模型只传 pointer_id）
#
# 契约源：scratch/sm-loop/handoff/SM-31/run-wf/decision.md（规则先于实现冻结）
# 用法: python tests/test_context_window_pointers.py
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _env(key, value):
    """临时改环境变量并在用例结束后恢复（测试之间不串开关状态）。"""
    old = os.environ.get(key)

    def setv():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    class _C:
        def __enter__(self):
            setv()
            return old

        def __exit__(self, *a):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
    return _C()


def _mk_memctx():
    """真实 MemoryContext：10 轮长会话（每轮 ~130 字，贴近真实轮次体量）
    + 摘要/相关历史/画像/偏好卡。最旧一轮带 veto 标记（否决证据）。"""
    from memory.conversation_memory import MemoryContext, Message, MsgRole
    msgs = []
    for i in range(10):
        role = MsgRole.USER if i % 2 == 0 else MsgRole.ASSISTANT
        meta = {"veto": True} if i == 0 else {}   # 最旧一轮是否决证据
        content = (f"历史轮次{i}：反应釜{i}号在 3 月 {i + 1} 日超温跳车，联锁动作正常，"
                   f"操作工按应急卡完成降温降压处置，随后检修班更换测温元件并复校 DCS 通道，"
                   f"过程记录完整，含处置时间线、责任人与验收签字（完整轮次正文{i}）。")
        msgs.append(Message(role=role, content=content, metadata=meta))
    return MemoryContext(
        recent_messages=msgs,
        relevant_history=[
            "跨会话历史片段A：去年同装置泄漏处置的完整记录——从发现、报警、疏散到检修"
            "复产的完整时间线，涉及当班班组、值班调度、检修工单与复查结论，共约三百字"
            "的过程描述，其中包含当时的处置责任人与整改闭环证据，属于跨会话情景记忆。",
            "跨会话历史片段B：2025 年动火票审批退回的完整记录——退回原因、补正材料、"
            "重新审批与现场核查的过程记录，以及后续同类作业的注意事项沉淀，约三百字的"
            "过程描述，属于跨会话情景记忆。",
        ],
        user_profile={"岗位": "操作工", "常涉装置": "反应釜"},
        summary="会话摘要：用户正在跟进反应釜超温隐患的整改。",
        preference_cards=[{"title": "答复偏好", "content": "先给步骤再给依据"}],
    )


# ── t0 开关缺省：逐字节不变 ──────────────────────────────────────────────────

def t0_off_byte_identical():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType
    from agents.agent_orchestrator import Request as Req
    from core.intent_recognizer import IntentCategory, UrgencyLevel
    orch = AgentOrchestrator(api_key="dummy", base_url="http://127.0.0.1:9", model="dummy")
    emp = orch._employee
    mem = _mk_memctx()
    old_full = mem.to_prompt_text()

    for env_val in (None, "0"):
        with _env("SAFETYMIND_CONTEXT_WINDOW", env_val):
            req = Req(message="反应釜超温怎么办", user_id="u1", conv_id="c1",
                      intent=IntentCategory.EQUIPMENT_ALARM, intent_confidence=0.9,
                      urgency=UrgencyLevel.MEDIUM, entities={}, context="", knowledge="",
                      memory_context=mem)
            task = emp.assemble_task(req)   # 直接走 _memory_block（不经领域切片）
            bg = task.messages[0]["content"]
            record(f"开关={env_val or '缺省'}：记忆块与现行为逐字节一致",
                   bg == f"[背景信息]\n{old_full}",
                   f"len={len(bg)} expected_len={len(old_full) + 7}")
            record(f"开关={env_val or '缺省'}：不产生有界引用块", "[有界引用]" not in bg)


# ── t1 窗口策略：保留关键、其余指针化 ────────────────────────────────────────

def t1_window_keeps_key_pointerizes_rest():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType
    from agents.agent_orchestrator import Request as Req
    from core.intent_recognizer import IntentCategory, UrgencyLevel
    import memory.evidence_pointers as ep
    orch = AgentOrchestrator(api_key="dummy", base_url="http://127.0.0.1:9", model="dummy")
    emp = orch._employee
    mem = _mk_memctx()
    full = mem.to_prompt_text()

    with _env("SAFETYMIND_CONTEXT_WINDOW", "1"):
        store = ep.EvidencePointerStore()
        req = Req(message="反应釜超温怎么办", user_id="u1", conv_id="c1",
                  intent=IntentCategory.EQUIPMENT_ALARM, intent_confidence=0.9,
                  urgency=UrgencyLevel.MEDIUM, entities={}, context="", knowledge="",
                  memory_context=mem)
        text = ep.window_memory_prompt(mem, store=store, subject_id="u1", session_id="c1")
        # 保留：摘要/画像/偏好卡/最近 N 轮（默认 4）
        record("窗口内保留会话摘要", "会话摘要" in text)
        record("窗口内保留人员安全画像", "[人员安全画像]" in text)
        record("窗口内保留偏好卡片", "先给步骤再给依据" in text)
        recent_kept = all(f"历史轮次{i}" in text for i in (6, 7, 8, 9))
        record("窗口内保留最近 4 轮全文", recent_kept)
        # 指针化：更早普通轮与跨会话相关历史不内联正文
        record("更早普通轮不再内联正文", "历史轮次2" not in text and "历史轮次3" not in text)
        record("跨会话相关历史不再内联正文",
               "处置责任人与整改闭环证据" not in text and "重新审批与现场核查的过程记录" not in text)
        record("产生有界引用块", "[有界引用]" in text)
        n_pointers = text.count("ctx-")
        record("指针行数量覆盖 5 条普通旧轮（旧轮 6 条中 1 条是否决）+ 2 条相关历史",
               n_pointers >= 7, f"n_pointers={n_pointers}")
        # 指针已登记且可按同主体同会话解析（提取含 ctx- 前缀的完整指针 ID）
        first_pid = ("ctx-" + text.split("ctx-")[1].split()[0]) if "ctx-" in text else ""
        ok = store.resolve(first_pid, subject_id="u1", session_id="c1")
        record("登记的指针可被同主体同会话解析回全文",
               bool(ok.get("ok")) and "跨会话历史片段" in str(ok.get("content", "")),
               f"resolve={ok}")
        # 体积方向：窗口块严格小于全量块（token 中位数下降机制的必要条件）
        record("窗口块体积小于全量记忆块", len(text) < len(full),
               f"window={len(text)} full={len(full)}")
        # 装配链路集成：开关开启时 _memory_block 走窗口渲染
        ep.swap_for_test(store)
        try:
            task = emp.assemble_task(req)
            bg = task.messages[0]["content"]
            record("装配链路在开关下使用窗口渲染（含有界引用）",
                   "[有界引用]" in bg and "历史轮次2" not in bg)
            record("装配链路保留用户原始问题完整注入", task.messages[-1]["content"] == req.message)
        finally:
            ep.swap_for_test(None)


# ── t2 否决证据无视窗口 ──────────────────────────────────────────────────────

def t2_veto_always_kept():
    import memory.evidence_pointers as ep
    mem = _mk_memctx()
    with _env("SAFETYMIND_CONTEXT_WINDOW", "1"), _env("SAFETYMIND_CTX_WINDOW_RECENT", "1"):
        store = ep.EvidencePointerStore()
        text = ep.window_memory_prompt(mem, store=store, subject_id="u1", session_id="c1")
        record("否决轮（veto 标记）无视窗口全文保留",
               "历史轮次0：反应釜0号在 3 月 1 日超温跳车" in text, f"text={text[:200]}")
        # recent_keep=1 时第 8 轮（普通轮）应被指针化
        record("recent_keep=1 时仅最近 1 轮全文",
               "历史轮次9" in text and "历史轮次8" not in text)


# ── t3 指针访问矩阵（权限/篡改/过期/读取失败）────────────────────────────────

def t3_access_matrix():
    import memory.evidence_pointers as ep
    store = ep.EvidencePointerStore()
    p = store.register(content="工单 TL-2087 的完整时间线： brief→review→accept，含改判事件",
                       subject_id="u1", session_id="c1", kind="ticket")
    record("正常登记返回指针", bool(p and p.get("pointer_id")), f"p={p}")
    pid = p["pointer_id"]

    r = store.resolve(pid, subject_id="u1", session_id="c1")
    record("同主体同会话解析命中全文", bool(r.get("ok")) and "TL-2087" in r.get("content", ""))
    r = store.resolve(pid, subject_id="attacker", session_id="c1")
    record("跨主体读取被拒（fail-closed）",
           r.get("ok") is False and r.get("reason") == "subject_mismatch" and not r.get("content"))
    r = store.resolve(pid, subject_id="u1", session_id="c2")
    record("跨会话读取被拒（fail-closed）",
           r.get("ok") is False and r.get("reason") == "session_mismatch" and not r.get("content"))
    r = store.resolve("ctx-doesnotexist", subject_id="u1", session_id="c1")
    record("未知指针如实返回 not_found", r.get("ok") is False and r.get("reason") == "not_found")

    p_exp = store.register(content="早前会话的排产记录（已过保留期）", subject_id="u1",
                           session_id="c1", kind="conversation", ttl_s=-1)
    r = store.resolve(p_exp["pointer_id"], subject_id="u1", session_id="c1")
    record("过期指针被拒且理由如实", r.get("ok") is False and r.get("reason") == "expired")

    p_t = store.register(content="SOP 条款：动火作业实行分级管理，固定动火区外应办理动火证",
                         subject_id="u1", session_id="c1", kind="sop_clause")
    store._entries[p_t["pointer_id"]]["content"] += "（被篡改的尾巴）"
    r = store.resolve(p_t["pointer_id"], subject_id="u1", session_id="c1")
    record("内容被篡改时完整性校验拒绝（fail-closed）",
           r.get("ok") is False and r.get("reason") == "tampered" and not r.get("content"))

    class _BrokenStore(ep.EvidencePointerStore):
        def _entry(self, pointer_id):
            raise RuntimeError("存储 I/O 故障注入")

    bstore = _BrokenStore()
    r = bstore.resolve(pid, subject_id="u1", session_id="c1")
    record("读取失败如实返回 store_error 且不编造内容",
           r.get("ok") is False and r.get("reason") == "store_error" and not r.get("content"))

    empty = ep.EvidencePointerStore()
    r = empty.resolve(pid, subject_id="u1", session_id="c1")
    record("空库读取返回 not_found（诚实缺席）", r.get("ok") is False and r.get("reason") == "not_found")


# ── t4 自动授权写操作禁止登记 ────────────────────────────────────────────────

def t4_auto_write_refused():
    import memory.evidence_pointers as ep
    store = ep.EvidencePointerStore()
    r1 = store.register(content="auto 档自动建单 TL-99（不可引用）", subject_id="u1",
                        session_id="c1", kind="ticket", auto_write=True)
    r2 = store.register(content="自动授权写操作记录", subject_id="u1",
                        session_id="c1", kind="auto_write")
    record("auto_write=True 登记被拒绝", r1 is None, f"r1={r1}")
    record("kind=auto_write 登记被拒绝", r2 is None, f"r2={r2}")
    record("指针库中不存在自动授权写条目", len(store._entries) == 0, f"entries={store._entries}")
    r3 = store.register(content="   ", subject_id="u1", session_id="c1", kind="conversation")
    record("空内容拒绝登记", r3 is None)


# ── t5 展开走工具轮次预算 ────────────────────────────────────────────────────

class _FakeMessages:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


def t5_expansion_via_tool_budget():
    from core.tool_execution import ToolExecutor
    import memory.evidence_pointers as ep
    from mcp.tool_manager import MCPToolManager, Tool

    async def handler(params, context):
        pid = str((params or {}).get("pointer_id") or "")
        scope = ep.current_scope()
        r = ep.REGISTRY.resolve(pid, subject_id=scope.get("subject_id"),
                                session_id=scope.get("session_id"))
        if not r.get("ok"):
            return {"found": False, "reason": r.get("reason"), "content": ""}
        return {"found": True, "kind": r.get("kind"), "content": r.get("content")}

    store = ep.EvidencePointerStore()
    ep.swap_for_test(store)
    try:
        p = store.register(content="动火作业分级管理规定全文：特级/一级/二级动火分别由厂/分厂/车间审批。",
                           subject_id="u1", session_id="c1", kind="sop_clause")
        pid = p["pointer_id"]
        tm = MCPToolManager(api_key="test-key")
        tm.register(Tool(name="evidence_expand", description="按指针展开受权证据",
                         handler=handler,
                         schema={"type": "object",
                                 "properties": {"pointer_id": {"type": "string"}},
                                 "required": ["pointer_id"]},
                         cache_ttl=0.0))

        script_client = SimpleNamespace(messages=_FakeMessages([
            _resp([{"type": "tool_use", "id": "tu_1", "name": "evidence_expand",
                    "input": {"pointer_id": pid}},
                   {"type": "tool_use", "id": "tu_2", "name": "evidence_expand",
                    "input": {"pointer_id": pid}}]),
            _resp([{"type": "text", "text": "依据展开的 SOP 条款作答。"}]),
        ]))
        ex = ToolExecutor(client=script_client, model="dummy", tool_manager=tm,
                          allowlist=["evidence_expand"], max_rounds=3, max_calls=1)
        _tok = ep.bind_request_scope("u1", "c1")
        try:
            result = asyncio.new_event_loop().run_until_complete(ex.run(
                system="s", messages=[{"role": "user", "content": "问动火分级"}]))
        finally:
            ep.reset_scope(_tok)
        record("第一次展开命中全文（受权证据进入 tool_result）",
               any("动火作业分级管理规定全文" in json.dumps(m, ensure_ascii=False)
                   for m in script_client.messages.calls), "calls=%d" % len(script_client.messages.calls))
        record("第二次展开被调用数预算拒绝（展开沿用工具预算）",
               any("超出调用预算" in str(rec.error) for rec in result.tool_trace),
               f"trace={[str(r.error) for r in result.tool_trace]}")
        record("预算拒绝如实标记 degraded（budget_calls）",
               result.terminated == "budget_calls" and result.degraded)

        # 越权展开：请求不存在/越界的指针 → 诚实 not_found，不编造内容
        script2 = SimpleNamespace(messages=_FakeMessages([
            _resp([{"type": "tool_use", "id": "tu_9", "name": "evidence_expand",
                    "input": {"pointer_id": "ctx-forged"}}]),
            _resp([{"type": "text", "text": "好。"}]),
        ]))
        ex2 = ToolExecutor(client=script2, model="dummy", tool_manager=tm,
                           allowlist=["evidence_expand"], max_rounds=3, max_calls=4)
        _tok2 = ep.bind_request_scope("u1", "c1")
        try:
            r2 = asyncio.new_event_loop().run_until_complete(ex2.run(
                system="s", messages=[{"role": "user", "content": "伪造指针"}]))
        finally:
            ep.reset_scope(_tok2)
        record("伪造指针展开返回诚实 not_found（无编造内容）",
               any("not_found" in json.dumps(m, ensure_ascii=False)
                   for m in script2.messages.calls))
        record("越权尝试全程零证据泄露", "动火作业分级管理规定全文" not in r2.text)

        # 白名单外：开关未把 evidence_expand 加入 allowlist 时（模拟），请求被白名单拒绝
        ex3 = ToolExecutor(client=script2, model="dummy", tool_manager=tm,
                           allowlist=["knowledge_search"], max_rounds=1, max_calls=4)
        script3 = SimpleNamespace(messages=_FakeMessages([
            _resp([{"type": "tool_use", "id": "tu_x", "name": "evidence_expand",
                    "input": {"pointer_id": pid}}]),
        ]))
        ex3._client = script3
        _tok3 = ep.bind_request_scope("u1", "c1")
        try:
            r3 = asyncio.new_event_loop().run_until_complete(ex3.run(
                system="s", messages=[{"role": "user", "content": "白名单外"}]))
        finally:
            ep.reset_scope(_tok3)
        record("evidence_expand 不在白名单时被拒绝执行",
               any("白名单" in str(rec.error) for rec in r3.tool_trace))
    finally:
        ep.swap_for_test(None)


# ── t6 per-expert 使用的 pointer_id 归集与落盘 ───────────────────────────────

def t6_per_expert_pointer_uses():
    from agents.agent_orchestrator import (AgentResponse, AgentType,
                                           evidence_pointer_uses_of)
    from core.tool_execution import ToolCallRecord
    import memory.evidence_pointers as ep

    r_ok = AgentResponse(agent_type=AgentType.EQUIPMENT, content="答", success=True)
    r_ok.tool_trace = [ToolCallRecord(tool_use_id="t1", name="evidence_expand",
                                      params={"pointer_id": "ctx-abc"}, ok=True,
                                      error=None, latency_ms=1.0, result_preview="")]
    r_none = AgentResponse(agent_type=AgentType.GENERAL, content="答2", success=True)
    r_none.tool_trace = []
    uses = evidence_pointer_uses_of([r_ok, r_none])
    by = {u["agent_type"]: u for u in uses}
    record("设备专家实际使用的 pointer_id 被归集",
           by.get("equipment", {}).get("pointer_ids") == ["ctx-abc"], f"uses={uses}")
    record("未使用展开的专家如实出现为空列表（不编造）",
           by.get("general", {}).get("pointer_ids") == []
           and by.get("general", {}).get("expansions") == 0)
    record("结果默认字段存在且为空（additive）",
           evidence_pointer_uses_of([]) == [])

    with tempfile.TemporaryDirectory() as td:
        s = ep.EvidencePointerStore(uses_dir=td)
        s.record_pointer_uses("req-1", uses)
        back = s.get_pointer_uses("req-1")
        record("per-expert 使用记录落盘并可读回",
               back.get("recorded") is True
               and {u["agent_type"] for u in back["uses"]} == {"equipment", "general"},
               f"back={back}")
        miss = s.get_pointer_uses("req-missing")
        record("无记录时诚实返回 recorded=false 与空 uses",
               miss.get("recorded") is False and miss.get("uses") == [])


# ── t7 handle() 接线 ─────────────────────────────────────────────────────────

def t7_handle_wires_uses():
    from agents.agent_orchestrator import (AgentOrchestrator, AgentType,
                                           BaseAgent, EmployeeAgent)
    from agents.agent_orchestrator import Request as Req
    from core.intent_recognizer import IntentCategory, UrgencyLevel
    from core.tool_execution import ToolCallRecord

    class _StubAgent(BaseAgent):
        agent_type = AgentType.EQUIPMENT
        system_prompt = "stub"

        async def _call_llm(self, task):
            self._last_tool_trace = [ToolCallRecord(
                tool_use_id="t1", name="evidence_expand",
                params={"pointer_id": "ctx-xyz"}, ok=True, error=None,
                latency_ms=1.0, result_preview="")]
            return "已结合展开证据作答。"

    pool = {AgentType.EQUIPMENT: [_StubAgent(None, "dummy")],
            AgentType.GENERAL: [_StubAgent(None, "dummy")]}
    emp = EmployeeAgent(pool)
    req = Req(message="反应釜超温", user_id="u1", conv_id="c1",
              intent=IntentCategory.EQUIPMENT_ALARM, intent_confidence=0.9,
              urgency=UrgencyLevel.MEDIUM, entities={}, context="", knowledge="")
    result = asyncio.new_event_loop().run_until_complete(emp.handle(req))
    uses = getattr(result, "evidence_pointer_uses", None)
    record("handle() 结果携带 evidence_pointer_uses 字段", uses is not None)
    record("接线归集到执行专家的实际 pointer_id",
           bool(uses) and uses[0].get("agent_type") == "equipment"
           and uses[0].get("pointer_ids") == ["ctx-xyz"], f"uses={uses}")


# ── t8 窗口渲染异常回退旧渲染 ────────────────────────────────────────────────

def t8_window_failure_falls_back():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType
    from agents.agent_orchestrator import Request as Req
    from core.intent_recognizer import IntentCategory, UrgencyLevel
    import memory.evidence_pointers as ep
    orch = AgentOrchestrator(api_key="dummy", base_url="http://127.0.0.1:9", model="dummy")
    emp = orch._employee
    mem = _mk_memctx()
    with _env("SAFETYMIND_CONTEXT_WINDOW", "1"):
        orig = ep.window_memory_prompt

        def _boom(*a, **k):
            raise RuntimeError("窗口渲染故障注入")
        ep.window_memory_prompt = _boom
        try:
            req = Req(message="m", user_id="u1", conv_id="c1",
                      intent=IntentCategory.EQUIPMENT_ALARM, intent_confidence=0.9,
                      urgency=UrgencyLevel.MEDIUM, entities={}, context="", knowledge="",
                      memory_context=mem)
            task = emp.assemble_task(req)
            bg = task.messages[0]["content"]
            record("窗口渲染故障时回退旧渲染（对话不阻塞）",
                   bg == f"[背景信息]\n{mem.to_prompt_text()}")
        finally:
            ep.window_memory_prompt = orig


# ── t9 scope 绑定 ────────────────────────────────────────────────────────────

def t9_scope_binding():
    import memory.evidence_pointers as ep
    tok = ep.bind_request_scope("u9", "c9")
    record("绑定后 current_scope 返回服务端主体/会话",
           ep.current_scope() == {"subject_id": "u9", "session_id": "c9"})
    ep.reset_scope(tok)
    record("重置后 scope 恢复为空", ep.current_scope() == {})


def main():
    for fn in (t0_off_byte_identical, t1_window_keeps_key_pointerizes_rest,
               t2_veto_always_kept, t3_access_matrix, t4_auto_write_refused,
               t5_expansion_via_tool_budget, t6_per_expert_pointer_uses,
               t7_handle_wires_uses, t8_window_failure_falls_back, t9_scope_binding):
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {type(ex).__name__}: {ex}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} PASSED")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
