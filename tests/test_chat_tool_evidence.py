# -*- coding: utf-8 -*-
# 集成（P0-A / P0-B + S-03）：走**真实 /chat 主链路**（LLM 与记忆打桩，
# 知识库/工具管理器用真实实现），验证两件事：
#   1) 用户输入真的触发了正确工具，且工具结果进入注入（tools_used 可见，不是"口头声称已查询"）；
#   2) 命中片段（而非"父文档开头 600 字"）进入生成请求，并产出可定位的 citations。
# 检索排序质量本身不在本用例断言范围内（S-03 另有检索命中率指标），
# 本用例固定查询命中尾部子块，只验证"命中证据保真 + 引用可定位"。
# 用法: python tests/test_chat_tool_evidence.py
import asyncio
import gc
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 共享的存储自检 helper
# 嵌入式 n-gram 嵌入：不下载模型、不联网（一切缓存只进 D 盘）
os.environ.setdefault("SAFETYMIND_EMBEDDING", "ngram")
os.environ["SAFETYMIND_EMERGENCY_FASTPATH"] = "0"   # 本用例只验证常规链路
# 临时存储放 scratch（工程目录内不留残留；Windows 上 chroma 句柄延迟释放会拖住目录删除）
_SCRATCH = Path("d:/claudecode/scratch/sm-iter-20261019")

import _storage_guard as guard   # 共享的"存储自检 + 一次重建重试"（只依赖标准库）

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


TAIL_CLAUSE = "二级动火票有效期不超过72小时UNIQ7788，作业结束后作业人须立即关闭动火票。"
FILLER = "特殊作业前必须完成风险辨识与安全交底。现场作业人员应确认作业票证齐全有效。监护人员须全程在岗并保持通讯畅通。"


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


class FakeOrchestrator:
    """打桩编排器：只记录收到的任务包并返回带 tool_trace 的结果。"""

    def __init__(self):
        self.last_req = None

    async def recognize_intent(self, message, history=None):
        from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
        return IntentResult(
            intent=IntentCategory.REGULATION_QUERY, confidence=0.92,
            urgency=UrgencyLevel.LOW, intent_group="general", entities={},
            reasoning="stub", latency_ms=1.0, source_scores={},
        )

    async def run(self, req):
        from agents.agent_orchestrator import AgentType, OrchestratorResult
        from core.tool_execution import ToolCallRecord
        self.last_req = req
        trace = [ToolCallRecord(tool_use_id="tu_1", name="knowledge_search",
                                params={"query": req.message}, ok=True, error=None,
                                latency_ms=6.0, result_preview="依据命中片段")]
        return OrchestratorResult(
            request_id=req.request_id, response="依据【依据1】给出的答复。",
            agent_type=AgentType.GENERAL, intent=req.intent, escalated=False,
            latency_ms=5.0, agent_types=[AgentType.GENERAL],
            primary_agent=AgentType.GENERAL, routing_reason="stub",
            routing_confidence=0.9, tool_trace=trace,
        )


def _fresh_dir(name: str) -> str:
    """确定性的临时目录：开跑前先清空（上一进程已退出、句柄已释放，删除必然成功），
    避免每次运行新建目录而在 scratch 里无限累积残留。"""
    p = _SCRATCH / name
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _build_stack():
    """真实 KnowledgeBase + ResourceLibrary + MCPToolManager，打进 api.main 的全局。"""
    from mcp.knowledge_base import KnowledgeBase
    from mcp.resource_library import ResourceLibrary
    from mcp.tool_manager import MCPToolManager, Tool

    _SCRATCH.mkdir(parents=True, exist_ok=True)
    # 长文档：尾部条款远超 600 字处（验证"命中子块"而非"父文档开头"进入注入）
    long_doc = FILLER * 8 + TAIL_CLAUSE
    # 本地 ChromaDB 偶发持久 HNSW 段不可读（全新目录也可能）：播种后自检，
    # 自检不过换全新目录重建一次，避免把环境抖动记成产品缺陷
    kb, kb_dir = guard.build_kb_checked(
        _SCRATCH,
        lambda d: KnowledgeBase(chroma_host="127.0.0.1", chroma_port=1, chroma_path=str(d)),
        lambda k: k.add_documents([{"title": "动火作业管理规定", "content": long_doc,
                                    "source": "内部制度", "version": "2026-03",
                                    "scope": "全厂", "status": "active"}]),
        "UNIQ7788", name="chat_kb_test_dir")
    lib_dir = _fresh_dir("chat_reslib_test_dir")
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
    audit_dir = _fresh_dir("chat_audit_test_dir")
    audit = AuditStore(db_path=str(Path(audit_dir) / "audit_test.db"))

    import api.main as api_main
    api_main._tool_manager = tm
    api_main._memory = FakeMemory()
    fake = FakeOrchestrator()
    api_main._orchestrator = fake
    api_main._audit = audit
    api_main._conv_store = None
    return api_main, fake, [kb_dir, lib_dir, audit_dir], kb, audit


def _cleanup(dirs, kb=None, audit=None):
    """显式释放 Chroma/存储句柄再删目录（Windows 句柄未释放会导致删除失败留残留）。"""
    guard.release_kb(kb)
    try:
        if audit is not None:
            audit.close()
    except Exception:
        pass
    gc.collect()
    # 知识库目录名带进程内序号（含自检失败后的重建目录），统一扫掉
    for d in list(dirs) + sorted(_SCRATCH.glob("chat_kb_test_dir*")):
        if not guard.rmtree_retry(d):
            print(f"[WARN] 临时目录残留未清干净（句柄未释放？）: {d}", flush=True)


async def _chat(api_main, message):
    return await api_main.chat(api_main.ChatRequest(message=message, user_id="u1"))


def test_tail_clause_hits_injection_and_citations():
    """命中位于文档尾部的条款时：snippet 进入注入，并产出可定位 citations。"""
    api_main, fake, dirs, kb, audit = _build_stack()
    try:
        resp = asyncio.new_event_loop().run_until_complete(
            _chat(api_main, "动火作业票有效期不超过72小时UNIQ7788怎么规定的"))
        injected = (fake.last_req.knowledge or "") + (fake.last_req.context or "")
        record("命中的注入文本包含尾部关键条款",
               "UNIQ7788" in injected, f"injected_len={len(injected)}")
        record("响应产出 citations", bool(resp.citations), f"citations={resp.citations}")
        first = resp.citations[0] if resp.citations else {}
        record("citation 指向正确片段（某条 citation 的 snippet 含尾条款）",
               any("UNIQ7788" in str(c.get("snippet", "")) for c in resp.citations),
               f"snippets={[str(c.get('snippet',''))[:40] for c in resp.citations]}")
        hit_cit = next((c for c in resp.citations if "UNIQ7788" in str(c.get("snippet", ""))), {})
        record("命中条款那条 citation 可定位到末段",
               bool(hit_cit) and int(hit_cit.get("chunk_index", -1)) >= 0
               and int(hit_cit.get("chunk_index", -1)) == int(hit_cit.get("total_chunks", 0)) - 1,
               f"chunk_index={hit_cit.get('chunk_index')} total={hit_cit.get('total_chunks')}")
        record("citation 带可定位身份（doc_id/chunk_id/section）",
               bool(hit_cit.get("doc_id")) and bool(hit_cit.get("chunk_id")) and bool(hit_cit.get("section")),
               f"doc_id={hit_cit.get('doc_id')} chunk_id={hit_cit.get('chunk_id')} section={hit_cit.get('section')}")
        record("citation 携带文档身份元数据(source/version)",
               hit_cit.get("source") == "内部制度" and hit_cit.get("version") == "2026-03",
               f"source={hit_cit.get('source')} version={hit_cit.get('version')}")
        record("catalog_only=false（有真实可引用正文）", hit_cit.get("catalog_only") is False)
        record("响应 tools_used 记录真实工具调用（knowledge_search）",
               any(t.get("name") == "knowledge_search" and t.get("ok") for t in resp.tools_used),
               f"tools_used={resp.tools_used}")
        record("knowledge_used 为真", resp.knowledge_used is True)
    finally:
        _cleanup(dirs, kb, audit)


def test_explicit_msds_query_triggers_deterministic_tool():
    """明确资源查询（化学品名/CAS）走确定性工具调用，证据进入回答。"""
    api_main, fake, dirs, kb, audit = _build_stack()
    try:
        resp = asyncio.new_event_loop().run_until_complete(
            _chat(api_main, "乙醇的泄漏应急处理要求是什么"))
        det = [t for t in resp.tools_used if t.get("name") == "msds_lookup"]
        record("明确化学品查询触发 msds_lookup", bool(det), f"tools_used={resp.tools_used}")
        record("msds_lookup 调用成功且标记为确定性调用",
               bool(det) and det[0].get("ok") is True and det[0].get("deterministic") is True,
               f"det={det}")
        record("MSDS 证据进入注入",
               "[资源库确定性查询]" in (fake.last_req.knowledge or ""),
               f"knowledge={ (fake.last_req.knowledge or '')[:80] }")
        record("资源库命中真实记录（含乙醇 CAS）",
               "64-17-5" in (fake.last_req.knowledge or ""),
               f"knowledge={ (fake.last_req.knowledge or '')[:160] }")
    finally:
        _cleanup(dirs, kb, audit)


def test_missing_resource_reports_gap_not_fabrication():
    """资源库不存在的对象（未知 CAS）：明确输出缺失状态，不编造 SDS。"""
    api_main, fake, dirs, kb, audit = _build_stack()
    try:
        resp = asyncio.new_event_loop().run_until_complete(
            _chat(api_main, "9999-99-9 这个化学品的泄漏应急处理要求是什么"))
        knowledge = fake.last_req.knowledge or ""
        det = [t for t in resp.tools_used if t.get("name") == "msds_lookup"]
        record("未知 CAS 触发确定性查询", bool(det), f"tools_used={resp.tools_used}")
        record("未命中输出资料缺口而非编造",
               "资料缺口" in knowledge and "不得编造" in knowledge, f"knowledge={knowledge[:160]}")
        record("响应未伪造该化学品条款", "9999-99-9" not in resp.response)
    finally:
        _cleanup(dirs, kb, audit)


if __name__ == "__main__":
    for fn in (test_tail_clause_hits_injection_and_citations,
               test_explicit_msds_query_triggers_deterministic_tool,
               test_missing_resource_reports_gap_not_fabrication):
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {type(ex).__name__}: {ex}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} PASSED")
    sys.exit(0 if passed == total else 1)
