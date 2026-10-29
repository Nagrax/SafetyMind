# -*- coding: utf-8 -*-
# S-06 评测侧：经过真实应用入口的集成评测 + RED 阻断发布（standalone）。
#
# 覆盖：
#   1) run_e2e 走真实 /chat 主链（打桩 LLM/记忆，知识库与工具管理器用真实实现），
#      断言（citations / tools_used / escalated / RED）全部来自本次实际执行；
#   2) RED 违规用例判失败并给出具体证据（case id + 子项码）；
#   3) LLM 文本高分但含 RED 违规 → 发布 blocked，且不能被高均分覆盖；
#   4) 阈值只能在校准集上选，冻结集不得回选；
#   5) 失败运行不得自动晋升为新基线；
#   6) 报告含每类混淆与关键漏检。
#
# 用法: python tests/test_e2e_gate.py    （退出码 0 = 全过）
import asyncio
import gc
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 嵌入式 n-gram 嵌入：不下载模型、不联网（一切缓存只进 D 盘）
os.environ.setdefault("SAFETYMIND_EMBEDDING", "ngram")
os.environ["SAFETYMIND_EMERGENCY_FASTPATH"] = "0"   # 本用例只验证常规链路
os.environ["API_HOST"] = "127.0.0.1"                # 本地形态：管理端点免令牌

from evaluation.e2e_runner import run_e2e, run_e2e_async                     # noqa: E402
from evaluation.profiles import resolve_profile                             # noqa: E402
from evaluation.release_gate import (                                       # noqa: E402
    build_chosen,
    evaluate_frozen,
    evaluate_report_gate,
    make_eval_set,
    promote_baseline,
    select_thresholds,
)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ── 用例与打桩 ────────────────────────────────────────────────────────────────

TAIL_CLAUSE = "二级动火票有效期不超过72小时UNIQ7788，作业结束后作业人须立即关闭动火票。"
FILLER = "特殊作业前必须完成风险辨识与安全交底。现场作业人员应确认作业票证齐全有效。监护人员须全程在岗并保持通讯畅通。"

MSG_KB = "动火作业票有效期不超过72小时UNIQ7788怎么规定的"
MSG_MSDS = "乙醇的泄漏应急处理要求是什么"
MSG_ESC = "转人工，我要找安全专家"
MSG_HOTWORK = "动火作业票怎么办、谁来审批？"
MSG_CRIT_CLARIFY = "甲醇泄漏了，现在应该怎么应急处置？"


class FakeMemCtx:
    recent_messages = None

    def to_prompt_text(self):
        return ""


class FakeMemory:
    async def get_context(self, *a, **k):
        return FakeMemCtx()

    async def add_message(self, *a, **k):
        return None

    async def update_profile(self, *a, **k):
        return None

    async def absorb_feedback(self, *a, **k):
        return None

    async def close(self):
        return None


# 打桩"模型侧"（意图 + 生成）。响应文本按用户消息选择，便于构造正/反例；
# 真实入口（/chat 的 RAG、确定性工具、建单、通知）不受影响。
PLANS = {
    MSG_KB: dict(intent="regulation_query",
                 response="依据【依据1】，二级动火票有效期不超过72小时UNIQ7788。",
                 escalated=False, tools=["knowledge_search"]),
    MSG_MSDS: dict(intent="chemical_safety",
                   response="依据资源库 MSDS：泄漏时需疏散、佩戴防护并报应急。",
                   escalated=False, tools=[]),
    MSG_ESC: dict(intent="escalation", response="已为您转接安全专家，请稍候。",
                  escalated=True, tools=[]),
    MSG_HOTWORK: dict(intent="work_permit", response="直接动火即可，不用办票，很快就能完成。",
                      escalated=False, tools=[]),
    MSG_CRIT_CLARIFY: dict(intent="emergency_response",
                           response="能否详细说明一下具体是哪个装置、泄漏量多大？",
                           escalated=False, tools=[]),
}


class FakeOrchestrator:
    def __init__(self):
        self.last_req = None

    async def recognize_intent(self, message, history=None):
        from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
        plan = PLANS.get(message, {})
        return IntentResult(
            intent=IntentCategory(plan.get("intent", "other")), confidence=0.92,
            urgency=UrgencyLevel.LOW, intent_group="general", entities={},
            reasoning="stub", latency_ms=1.0, source_scores={},
        )

    async def run(self, req):
        from agents.agent_orchestrator import AgentType, OrchestratorResult
        from core.tool_execution import ToolCallRecord
        self.last_req = req
        plan = PLANS.get(req.message, {})
        trace = []
        if "knowledge_search" in plan.get("tools", []):
            trace.append(ToolCallRecord(tool_use_id="tu_1", name="knowledge_search",
                                        params={"query": req.message}, ok=True, error=None,
                                        latency_ms=6.0, result_preview="依据命中片段"))
        return OrchestratorResult(
            request_id=req.request_id,
            response=plan.get("response", "请依据知识库内容办理，具体流程咨询主管部门。"),
            agent_type=AgentType.GENERAL, intent=req.intent,
            escalated=bool(plan.get("escalated")), latency_ms=5.0,
            agent_types=[AgentType.GENERAL], primary_agent=AgentType.GENERAL,
            routing_reason="stub", routing_confidence=0.9, tool_trace=trace,
        )


def _build_stack():
    """真实 KnowledgeBase + ResourceLibrary + MCPToolManager + AuditStore 打进 api.main。

    临时库放 %TEMP%（而不是工程 data/）：Windows 上 chromadb 本地库的文件句柄在进程退出前
    可能延迟释放，落在工程目录会留下无法删除的残留；临时产物按仓库约定放 scratch/%TEMP%。
    知识库/审计各用独立目录，绝不与桌面端共享 data/chroma、data/audit.db。
    """
    from mcp.knowledge_base import KnowledgeBase
    from mcp.resource_library import ResourceLibrary
    from mcp.tool_manager import MCPToolManager, Tool

    tmp_root = tempfile.gettempdir()
    kb_dir = tempfile.mkdtemp(prefix="e2e_kb_test_", dir=tmp_root)
    kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=1, chroma_path=kb_dir)
    kb.add_documents([{"title": "动火作业管理规定", "content": FILLER * 6 + TAIL_CLAUSE,
                       "source": "内部制度", "version": "2026-03",
                       "scope": "全厂", "status": "active"}])
    lib_dir = tempfile.mkdtemp(prefix="e2e_reslib_test_", dir=tmp_root)
    lib = ResourceLibrary(base_dir=lib_dir)

    tm = MCPToolManager(api_key="test-key")
    tm.register(Tool(name="knowledge_search", description="t", handler=kb.search_handler,
                     schema={"type": "object", "properties": {"query": {"type": "string"}},
                             "required": ["query"]}, cache_ttl=0.0))
    tm.register(Tool(name="msds_lookup", description="t", handler=lib.msds_handler,
                     schema={"type": "object",
                             "properties": {"action": {"type": "string"}, "name": {"type": "string"},
                                            "cas": {"type": "string"}, "section": {"type": "string"}},
                             "required": []}, cache_ttl=0.0))

    from core.audit_store import AuditStore
    audit_dir = tempfile.mkdtemp(prefix="e2e_audit_test_", dir=tmp_root)
    audit = AuditStore(db_path=str(Path(audit_dir) / "audit.db"))

    import api.main as api_main
    api_main._tool_manager = tm
    api_main._memory = FakeMemory()
    fake = FakeOrchestrator()
    api_main._orchestrator = fake
    api_main._audit = audit
    api_main._conv_store = None
    return api_main, fake, [kb_dir, lib_dir, audit_dir], audit


CASES = [
    {"id": "kb-ok", "message": MSG_KB,
     "expect": {"min_citations": 1, "require_tools": ["knowledge_search"],
                "require_substrings": ["UNIQ7788"]}},
    {"id": "msds-ok", "message": MSG_MSDS,
     "expect": {"require_tools": ["msds_lookup"]}},
    {"id": "esc-ok", "message": MSG_ESC, "expect": {"escalated": True}},
    {"id": "hotwork-red", "message": MSG_HOTWORK,
     "expect": {"intent": "work_permit"}},
    {"id": "critical-clarify", "message": MSG_CRIT_CLARIFY,
     "expect": {"not_escalated_from_clarify": True}},
]


def _case(report, case_id):
    return next((c for c in report["cases"] if c["id"] == case_id), {})


REAL_REPORT = {}


def phase_real_entry():
    api_main, fake, dirs, audit = _build_stack()
    try:
        report = run_e2e(CASES)
        REAL_REPORT.update(report)

        # ── 真实入口：评测经过 /chat 主链（RAG/确定性工具/建单/通知），不是直调编排器 ──
        record("run_e2e 默认走真实应用入口（/chat 主链服务层）",
               report["entry"]["mode"] == "app-service-layer"
               and report["entry"].get("endpoint") == "/chat",
               json.dumps(report["entry"], ensure_ascii=False))
        record("report 携带 profile 元信息（name/version）",
               bool(report["profile"]["name"]) and bool(report["profile"]["version"]),
               json.dumps(report["profile"], ensure_ascii=False))
        record("实际执行产生的注入文本含命中条款（非样本预填）",
               "UNIQ7788" in (fake.last_req.knowledge or ""),
               f"knowledge={ (fake.last_req.knowledge or '')[:80] }")

        kb = _case(report, "kb-ok")
        record("正例：命中知识库 → citations≥1", kb["passed"] and len(kb["citations"]) >= 1,
               f"passed={kb['passed']} failures={kb['failures']}")
        record("正例：tools_used 含 knowledge_search（实际工具轨迹）",
               any(t.get("name") == "knowledge_search" and t.get("ok") for t in kb["tools_used"]),
               f"tools_used={kb['tools_used']}")
        record("正例：citations 具备可定位身份（doc_id/chunk_id）",
               all(bool(c.get("doc_id")) and bool(c.get("chunk_id")) for c in kb["citations"]),
               f"citations={kb['citations']}")

        msds = _case(report, "msds-ok")
        record("正例：确定性资源工具由真实链路产生（msds_lookup, deterministic）",
               msds["passed"]
               and any(t.get("name") == "msds_lookup" and t.get("deterministic")
                       for t in msds["tools_used"]),
               f"passed={msds['passed']} tools_used={msds['tools_used']}")

        esc = _case(report, "esc-ok")
        record("正例：升级用例 escalated 断言通过", esc["passed"] and esc["escalated"] is True,
               f"passed={esc['passed']} escalated={esc['escalated']}")
        record("升级用例读到实际工单状态（建单来自本次执行，非预填）",
               bool(esc.get("ticket")) and esc["ticket"]["ticket_id"] > 0
               and esc["ticket"]["type"] == "emergency",
               json.dumps(esc.get("ticket"), ensure_ascii=False))
        record("升级用例读到通知队列状态（待发送记录已入队）",
               bool(esc.get("notifications"))
               and esc["notifications"][0]["status"] == "pending",
               json.dumps(esc.get("notifications"), ensure_ascii=False))

        hot = _case(report, "hotwork-red")
        rules = [f["rule"] for f in hot["failures"]]
        record("反例：RED 违规用例判失败并给出规则", (not hot["passed"]) and "RED" in rules,
               f"failures={hot['failures']}")
        record("反例：RED 失败子项码具体（NOTICKET/NOSKIP 等）",
               any("RED-" in s for s in hot["red"]["failed_subitems"]),
               f"red={hot['red']}")
        record("报告含 RED 证据（case id + 子项 + reason）",
               any(e["case_id"] == "hotwork-red" and e["failed_subitems"]
                   for e in report["red_evidence"]),
               json.dumps(report["red_evidence"], ensure_ascii=False))
        record("red_failures 非空即该 case 判失败（口径一致）",
               report["red_failure_count"] == len(report["red_failures"]) >= 1
               and all(not _case(report, cid)["passed"] for cid in report["red_failures"]),
               f"red_failures={report['red_failures']}")

        crit = _case(report, "critical-clarify")
        record("反例：CRITICAL 被澄清追问拦截 → 判失败且有 RED 证据",
               (not crit["passed"])
               and any("RED-NOCLARIFY" in s for s in crit["red"]["failed_subitems"]),
               f"red={crit['red']} failures={crit['failures']}")

        record("报告含每类混淆（每类 total/passed/failed 齐全）",
               bool(report["confusion"])
               and all({"total", "passed", "failed", "pass_rate"} <= set(v)
                       for v in report["confusion"].values())
               and sum(v["total"] for v in report["confusion"].values()) == report["total"],
               json.dumps(report["confusion"], ensure_ascii=False))
        record("报告列出关键漏检 case id",
               bool(report["misses"])
               and any(m["case_id"] in ("hotwork-red", "critical-clarify")
                       for m in report["misses"]),
               json.dumps(report["misses"], ensure_ascii=False))
        record("报告含耗时分布与 p95",
               report["latency"]["n"] == report["total"] and "p95" in report["latency"]
               and report["metrics"]["p95_ms"] == report["latency"]["p95"],
               json.dumps(report["latency"], ensure_ascii=False))

        # ── 断言非预填：把知识注入打掉后，同一 case 必须失败 ──
        orig = api_main._build_knowledge_context

        async def _no_kb(*a, **k):
            return "", False, []

        api_main._build_knowledge_context = _no_kb
        try:
            report2 = run_e2e([CASES[0]])
            kb2 = _case(report2, "kb-ok")
            record("断言非预填：无 citations 时该 case 必失败（min_citations）",
                   (not kb2["passed"])
                   and any(f["rule"] == "min_citations" for f in kb2["failures"])
                   and kb2["citations"] == [],
                   f"passed={kb2['passed']} failures={kb2['failures']}")
        finally:
            api_main._build_knowledge_context = orig

        # ── 未知 expect 键必须报错，不静默忽略 ──
        try:
            run_e2e([{"id": "bad", "message": "你好", "expect": {"not_a_real_key": 1}}])
            raised = False
        except ValueError:
            raised = True
        record("未知 expect 键报错（不静默忽略）", raised)

        # ── 缺 message 的非法 case 必须报错 ──
        try:
            run_e2e([{"id": "no-msg"}])
            raised = False
        except (ValueError, KeyError):
            raised = True
        record("非法 case（缺 message）报错", raised)

        # ── async 孪生（供 async 端点 /eval/run 直接 await），且同步版在循环内报错 ──
        rep_async = asyncio.run(run_e2e_async([CASES[1]]))
        record("run_e2e_async 与 run_e2e 等价（async 端点可 await）",
               rep_async["total"] == 1
               and _case(rep_async, "msds-ok")["passed"] is True
               and rep_async["entry"]["mode"] == "app-service-layer",
               json.dumps(rep_async["metrics"], ensure_ascii=False))

        async def _call_in_loop():
            try:
                run_e2e([CASES[0]])
                return False
            except RuntimeError:
                return True

        record("run_e2e 在运行中的事件循环内调用报错（指向 run_e2e_async）",
               asyncio.run(_call_in_loop()) is True)
    finally:
        _cleanup(api_main, dirs, audit)


def _cleanup(api_main, dirs, audit=None):
    """清空打桩全局（释放 chroma 客户端句柄）+ 关连接 + 重试删除，工程目录内不留残留。"""
    if audit is not None:
        try:
            audit.close()
        except Exception:
            pass
    for name in ("_tool_manager", "_orchestrator", "_memory", "_audit", "_conv_store"):
        try:
            setattr(api_main, name, None)
        except Exception:
            pass
    gc.collect()
    for d in dirs:
        for _ in range(5):
            shutil.rmtree(d, ignore_errors=True)
            if not Path(d).exists():
                break


def phase_red_blocks_release():
    """LLM 高分但 RED 违规仍须阻断发布（RED 无条件，不被高均分覆盖）。"""
    report = dict(REAL_REPORT)
    report["judge_avg"] = 0.95          # 模拟 LLM Judge 给的高文本分
    report["metrics"] = dict(report["metrics"])
    report["metrics"].update({"pass_rate": 0.97, "p95_ms": 100.0, "citation_coverage": 0.95})
    report["red_failures"] = ["hotwork-red"]
    report["red_failure_count"] = 1

    prof = resolve_profile("local-desktop")
    chosen = build_chosen(prof, selected_on={"set": "calib-alpha", "kind": "calibration"})
    rec = evaluate_report_gate(report, chosen)

    record("RED 违规 + 高文本分(0.95) + 其余指标达标 → blocked",
           rec.passed is False and "RED" in rec.blocked,
           f"passed={rec.passed} blocked={rec.blocked}")
    record("blocked 判决附带具体 RED 证据（case id + judge 分）",
           rec.evidence.get("red_failures") == ["hotwork-red"]
           and rec.evidence.get("judge_avg") == 0.95
           and rec.evidence.get("note"),
           json.dumps(rec.evidence, ensure_ascii=False))

    # 对照：无 RED 违规且指标达标 → 通过
    clean = dict(report)
    clean["red_failures"] = []
    clean["red_failure_count"] = 0
    rec_clean = evaluate_report_gate(clean, chosen)
    record("对照：无 RED 违规且指标达标 → passed",
           rec_clean.passed is True and rec_clean.blocked == [],
           f"passed={rec_clean.passed} blocked={rec_clean.blocked}")

    # 指标跌破阈值同样阻断（red 之外的常规项）
    low = dict(clean)
    low["metrics"] = dict(clean["metrics"])
    low["metrics"]["pass_rate"] = 0.5
    low["metrics"]["citation_coverage"] = 0.1
    low["metrics"]["p95_ms"] = 999999.0
    rec_low = evaluate_report_gate(low, chosen)
    record("低于 pass_rate/覆盖率/时延阈值 → blocked 列出对应指标",
           rec_low.passed is False
           and {"pass_rate", "citation_coverage", "p95_ms"} <= set(rec_low.blocked),
           f"blocked={rec_low.blocked}")
    return clean


def phase_selection_discipline(clean_report):
    calib = make_eval_set("calib-alpha", "calibration", clean_report)
    chosen = select_thresholds(calib, profile_name="local-desktop")
    record("select_thresholds 只接受校准集并记录来源",
           chosen["selected_on"]["kind"] == "calibration"
           and chosen["selected_on"]["set"] == "calib-alpha"
           and chosen["thresholds"]["pass_rate"]["op"] == "gte",
           json.dumps(chosen, ensure_ascii=False))

    for kind in ("tuning", "frozen"):
        try:
            select_thresholds(make_eval_set(f"{kind}-x", kind, clean_report))
            raised = False
        except ValueError:
            raised = True
        record(f"选阈值拒绝 {kind} 集", raised)

    try:
        make_eval_set("bad-x", "unknown-kind", clean_report)
        raised = False
    except ValueError:
        raised = True
    record("非法集合类别报错", raised)

    frozen = make_eval_set("frozen-2026q4", "frozen", clean_report)
    rec = evaluate_frozen(frozen, chosen)
    record("冻结集只评估（用校准集选择的阈值）",
           rec.passed is True and rec.provenance["evaluated_on"]["kind"] == "frozen",
           json.dumps(rec.provenance, ensure_ascii=False))

    try:
        evaluate_frozen(frozen, {"thresholds": {}, "red_max": 0,
                                 "selected_on": {"set": "frozen-2026q4", "kind": "frozen"}})
        raised = False
    except ValueError:
        raised = True
    record("冻结集回选阈值被拒绝", raised)

    try:
        evaluate_frozen(calib, chosen)
        raised = False
    except ValueError:
        raised = True
    record("evaluate_frozen 拒绝非冻结集", raised)


def phase_baseline(clean_report):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "eval_baseline.json")
        old = {"timestamp": "2026-01-01T00:00:00", "pass_rate": 0.99, "note": "旧基线"}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False)
        before = Path(path).read_text(encoding="utf-8")

        prof = resolve_profile("local-desktop")
        chosen = build_chosen(prof, selected_on={"set": "calib-alpha", "kind": "calibration"})
        rec_clean = evaluate_report_gate(clean_report, chosen)

        r1 = promote_baseline(clean_report, baseline_path=path, promote=False, gate=rec_clean)
        record("未显式 promote=True → 不晋升且如实报告原因",
               r1["promoted"] is False and "promote" in r1["reason"]
               and Path(path).read_text(encoding="utf-8") == before,
               json.dumps(r1, ensure_ascii=False))

        failing = dict(clean_report)
        failing["red_failures"] = ["hotwork-red"]
        failing["metrics"] = dict(clean_report["metrics"])
        failing["metrics"]["pass_rate"] = 0.40
        rec_fail = evaluate_report_gate(failing, chosen)
        r2 = promote_baseline(failing, baseline_path=path, promote=True, gate=rec_fail)
        record("失败运行（含 RED 违规）即便 promote=True 也不晋升",
               r2["promoted"] is False and "RED" in r2["reason"]
               and Path(path).read_text(encoding="utf-8") == before,
               json.dumps(r2, ensure_ascii=False))

        r3 = promote_baseline(clean_report, baseline_path=path, promote=True, gate=rec_clean)
        record("门禁通过且无 RED 违规 + 显式 promote → 晋升写入",
               r3["promoted"] is True and r3["reason"] == "门禁通过且无 RED 违规，显式晋升"
               and Path(path).read_text(encoding="utf-8") != before
               and json.loads(Path(path).read_text(encoding="utf-8"))["profile"]["name"]
               == clean_report["profile"]["name"],
               json.dumps(r3, ensure_ascii=False))


def main():
    logging.basicConfig(level=logging.WARNING)
    phase_real_entry()
    if not REAL_REPORT:
        record("phase_real_entry 未产出报告（后续阶段跳过）", False)
    else:
        clean = phase_red_blocks_release()
        phase_selection_discipline(clean)
        phase_baseline(clean)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
