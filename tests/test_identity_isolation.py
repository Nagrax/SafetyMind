# -*- coding: utf-8 -*-
# S-05 服务端身份与数据隔离端到端测试：python tests/test_identity_isolation.py（退出码 0 = 全过）
#
# 覆盖（全部走 FastAPI TestClient 真实 HTTP 端点）：
#   1) 主体归属：多用户形态下服务端主体覆盖客户端 user_id（改 user_id 读不到别人的会话/工单）
#   2) 会话隔离：/conversations 列表 / messages / DELETE 以服务端主体为界，不泄露他人会话
#   3) 工单/上报编号隔离：/tickets/progress/{rid} 仅上报主体可见（他人 found=false）
#   4) 角色边界：处理者不能越权验收；accept 必须有验收批注；旧兼容 close 缺整改证据 → 400
#   5) 资料 ACL：/search/direct 与 /chat 检索只出本主体范围与公共文档，绝不出他人范围正文
#   6) 本地形态（清掉 ACCESS_TOKEN/部署标记）行为不变：仍按客户端 user_id 工作
#   7) 多用户形态下无访问令牌 → 沿用 access_gate 的 401 语义，不泄露存在性
#
# 铁律 9：存储全部走临时目录（*_test / scratch），不与桌面端共享 data/chroma、data/kb_chroma。
# 骨架：编排器/记忆打桩（不依赖 LLM），知识库/工单/会话用真实实现 → 断言的是真实端点行为。
import asyncio
import gc
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SCRATCH = Path("d:/claudecode/scratch/sm-iter-20261019")

TOKEN_A = "tok-user-a"
TOKEN_B = "tok-user-b"
ACCESS = "gate-iso-77"
ADMIN = "admin-iso-9"

PLANT_A_MARK = "PLANTA-SECRET-1101"
PLANT_B_MARK = "PLANTB-SECRET-2202"
PUBLIC_MARK = "PUBLIC-SECRET-3303"

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _multi_user_env():
    """多用户/网络形态：部署标记 + 访问口令 + 两用户主体目录（A 仅上报者/plant-a，B 上报者+验收者/plant-b）。"""
    os.environ.update({
        "SAFETYMIND_DEPLOYED": "1",
        "ACCESS_TOKEN": ACCESS,
        "ADMIN_TOKEN": ADMIN,
        "SAFETYMIND_USERS": json.dumps([
            {"token": TOKEN_A, "subject_id": "u-A", "roles": ["reporter"], "scopes": ["plant-a"]},
            {"token": TOKEN_B, "subject_id": "u-B", "roles": ["reporter", "verifier"], "scopes": ["plant-b"]},
        ], ensure_ascii=False),
    })


def _local_form_env():
    """本地单人形态：无访问口令、无部署标记、绑定回环。"""
    os.environ.pop("ACCESS_TOKEN", None)
    os.environ.pop("SAFETYMIND_DEPLOYED", None)
    os.environ["API_HOST"] = "127.0.0.1"


# ── 打桩（不依赖 LLM）：编排器只记录注入内容并回固定答复；记忆不落盘 ──────────
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
    """只记录收到的任务包（含 RAG 注入文本）并返回带 tool_trace 的结果。"""

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
        self.last_req = req
        return OrchestratorResult(
            request_id=req.request_id, response="依据【依据1】给出的答复。",
            agent_type=AgentType.GENERAL, intent=req.intent, escalated=False,
            latency_ms=5.0, agent_types=[AgentType.GENERAL],
            primary_agent=AgentType.GENERAL, routing_reason="stub",
            routing_confidence=0.9, tool_trace=[],
        )


SEED_DOCS = [
    {"title": "A 厂受限空间作业制度", "content": f"受限空间作业取样分析要求，条款编号 {PLANT_A_MARK} 专用于甲厂区。",
     "source": "A厂制度", "version": "2026-01", "scope": "plant-a"},
    {"title": "B 厂受限空间作业制度", "content": f"受限空间作业取样分析要求，条款编号 {PLANT_B_MARK} 专用于乙厂区。",
     "source": "B厂制度", "version": "2026-01", "scope": "plant-b"},
    {"title": "全厂公共安全须知", "content": f"全厂通用安全须知，条款编号 {PUBLIC_MARK} 适用于所有厂区。",
     "source": "公司制度", "version": "2026-01", "scope": ""},
]
SEED_CHUNKS = 3   # 三篇短文档各切成 1 个子块（<500 字）


def _fresh_kb_dir(tmp_root: Path, tag: str) -> Path:
    """取一个全新未使用的 chroma 持久目录；同名残留（上次崩溃留下）先清理再断言为空。

    本机 ChromaDB 持久库偶发段文件不可读（见 core/chroma_utils.py 的说明），
    复用/残留目录会放大该问题，因此每次（含重建）都用全新目录。
    """
    d = tmp_root / f"kb_chroma_test_{tag}"
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)
    assert not d.exists(), f"chroma 目录残留未清理干净: {d}"
    d.mkdir(parents=True)
    return d


def _release_kb(kb) -> None:
    """显式释放 Chroma 客户端句柄：删集合 → 关客户端 → 清共享系统缓存 → gc。

    Windows 下句柄未释放时目录删不掉（已复现 WinError 32/5），也会让后续打开
    同一目录读到不一致状态；异常路径同样要走到这里。
    """
    if kb is None:
        return
    try:
        for name in (kb._collection.name, kb._parents.name):
            try:
                kb._client.delete_collection(name)
            except Exception:
                pass
        kb._client.close()
    except Exception:
        pass
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    gc.collect()


def _seed_kb(chroma_dir: Path):
    """在指定目录构造唯一 KnowledgeBase 实例并播种，返回 (kb, 播种前片段数)。"""
    from mcp.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=8973,
                       chroma_path=str(chroma_dir))
    base = kb.doc_count   # 与 doc_count_async 同源（同一 collection.count）
    kb.add_documents(SEED_DOCS)
    return kb, base


def _storage_healthy(kb, base: int):
    """播种后立刻自检存储真的可用 —— 度量的是隔离行为，而不是磁盘抖动。

    1) 走本机（非外部服务）且真实子块数 == 播种前 + 本次导入片段数；
    2) 不带 scopes 的检索必须命中 PLANT_A_MARK（段文件不可读时必失败）。
    """
    if kb._use_server:
        return False, f"意外连上外部 ChromaDB 服务（{kb._client}），度量对象不是本地临时目录"
    expected = base + SEED_CHUNKS
    got = kb.doc_count
    if got != expected:
        return False, f"doc_count={got} != 预期 {expected}"
    try:
        hits = asyncio.run(kb.search_async(PLANT_A_MARK, top_k=5, scopes=None))
    except Exception as ex:
        return False, f"无 scopes 自检检索异常 {type(ex).__name__}: {str(ex)[:120]}"
    if not any(PLANT_A_MARK in (h.get("snippet") or "") for h in hits):
        return False, f"无 scopes 自检检索命中 {len(hits)} 条，但无 {PLANT_A_MARK}"
    return True, ""


def _build_kb_checked(tmp_root: Path):
    """全新目录播种 + 自检；不可用则换**全新目录**重建一次并重新播种（最多一次）。

    返回值可能为 None（两次构造/播种都抛异常）→ 上层显式报错，不静默降级。
    """
    kb = None
    for tag in ("primary", "rebuild"):
        if kb is not None:            # 上一个实例（含其目录句柄）先彻底释放
            _release_kb(kb)
            kb = None
        chroma_dir = _fresh_kb_dir(tmp_root, tag)
        try:
            kb, base = _seed_kb(chroma_dir)
        except Exception as ex:
            print(f"[WARN] chroma 存储不可用（{chroma_dir.name}）：构造/播种失败 "
                  f"{type(ex).__name__}: {str(ex)[:160]}", flush=True)
            _release_kb(kb)
            kb = None
            continue
        ok, why = _storage_healthy(kb, base)
        if ok:
            if tag != "primary":
                print(f"[WARN] 存储自检失败后已在全新目录重建知识库：{chroma_dir.name}", flush=True)
            return kb
        print(f"[WARN] 存储自检失败（{chroma_dir.name}）：{why} → 换全新目录重建一次", flush=True)
        if tag == "rebuild":
            print("[WARN] 重建一次后存储自检仍不可用；后续断言将如实反映该状态", flush=True)
            return kb
    return kb


def _build_stack(tmp_root: Path):
    """真实 KnowledgeBase/工单存储/会话存储 + 打桩编排器与记忆，打进 api.main 的全局。

    注意：不进入 TestClient 的 lifespan（`with` 语句），避免真实启动覆盖打桩组件且要求
    真实 LLM 凭据——本用例只验证端点行为与隔离判定。
    """
    from core.audit_store import AuditStore
    from mcp.tool_manager import MCPToolManager, Tool
    from memory.conversation_store import ConversationStore

    kb = _build_kb_checked(tmp_root)
    if kb is None:
        raise RuntimeError("chroma 临时存储在两次全新目录中都不可用（构造/播种均失败），无法度量隔离行为")

    tm = MCPToolManager(api_key="test-key")
    tm.register(Tool(name="knowledge_search", description="t", handler=kb.search_handler,
                     schema={"type": "object", "properties": {"query": {"type": "string"}},
                             "required": ["query"]}, cache_ttl=300.0))

    audit = AuditStore(db_path=str(tmp_root / "audit_test.db"))
    conv = ConversationStore(root=str(tmp_root / "convs_test"))

    import api.main as api_main
    api_main._tool_manager = tm
    api_main._memory = FakeMemory()
    api_main._orchestrator = FakeOrchestrator()
    api_main._audit = audit
    api_main._conv_store = conv
    # 进程内只允许一个 KnowledgeBase 实例绑定该目录：端点拿到的必须就是本实例
    assert api_main._tool_manager._tools["knowledge_search"].handler.__self__ is kb
    return api_main, audit, conv, kb


def _hdr(token):
    return {"X-Access-Token": ACCESS, "X-User-Token": token}


def _ahdr(token):
    """管理端点请求头：管理密码 + 主体令牌。"""
    return {"X-Access-Token": ACCESS, "X-Admin-Token": ADMIN, "X-User-Token": token}


def _advance_to_feedback(client, tid):
    """建单后顺序流转到 feedback 阶段（accept 只能从 feedback 起）。"""
    for stage in ("discuss", "execute", "feedback"):
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": stage, "actor": "admin", "note": "流转"}, headers=_ahdr(TOKEN_B))
        assert r.status_code == 200, f"流转 {stage} 失败: {r.status_code} {r.text[:120]}"


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="identity_iso_test_", dir=str(SCRATCH)))

    # 环境变量必须在任何可能构造 KnowledgeBase 的导入之前设置好（导入次序 = 生效次序）
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",          # 零下载离线嵌入
        "SAFETYMIND_EMERGENCY_FASTPATH": "1",
        "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
        "SAFETYMIND_OUTBOX_ENABLED": "0",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "AUDIT_DB_PATH": str(tmp_root / "audit_test.db"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(tmp_root / "convs_test"),
        # chroma 相关变量：显式钉在本次运行的临时目录/不可达端口上，
        # 任何按环境变量构造 KB 的代码路径也不会落到工程 data/ 目录
        "CHROMA_HOST": "127.0.0.1",
        "CHROMA_PORT": "8973",
        "CHROMA_KB_PERSIST_DIRECTORY": str(tmp_root / "kb_chroma_test_primary"),
    })
    _multi_user_env()

    from fastapi.testclient import TestClient

    try:
        api_main, audit, conv, kb = _build_stack(tmp_root)
    except BaseException:
        shutil.rmtree(str(tmp_root), ignore_errors=True)   # 异常路径同样不留残留
        raise
    client = TestClient(api_main.app)   # 不进入 lifespan：保留打桩组件

    try:
        # ── 1) 主体归属：服务端主体覆盖客户端 user_id ───────────────────────
        r = client.post("/chat", json={"message": "受限空间的取样分析要求是什么",
                                       "user_id": "u-B", "conv_id": "conv-A1"},
                        headers=_hdr(TOKEN_A))
        record("A 上报：/chat 以服务端主体为准（客户端 user_id=u-B 被覆盖）",
               r.status_code == 200 and r.json().get("subject_id") == "u-A",
               f"code={r.status_code} subject_id={r.json().get('subject_id') if r.status_code == 200 else r.text[:120]}")

        r = client.post("/chat", json={"message": "受限空间的取样分析要求是什么",
                                       "user_id": "u-B", "conv_id": "conv-B1"},
                        headers=_hdr(TOKEN_B))
        record("B 建会话：会话归属服务端主体 u-B",
               r.status_code == 200 and r.json().get("subject_id") == "u-B",
               f"code={r.status_code}")

        # ── 2) 会话隔离：改 user_id 读不到别人的数据 ─────────────────────────
        r = client.get("/conversations", params={"user_id": "u-B"}, headers=_hdr(TOKEN_A))
        convs = [c["conv_id"] for c in r.json().get("conversations", [])] if r.status_code == 200 else []
        record("A 改 user_id=u-B 读列表：读不到 B 的会话（也不泄露 B 会话存在）",
               r.status_code == 200 and "conv-B1" not in convs,
               f"code={r.status_code} convs={convs}")

        r = client.get("/conversations/conv-B1/messages",
                       params={"user_id": "u-B"}, headers=_hdr(TOKEN_A))
        msgs = r.json().get("messages", []) if r.status_code == 200 else ["<err>"]
        record("A 改 user_id 读 B 会话消息：拿不到内容（空列表，不泄露存在性）",
               r.status_code == 200 and msgs == [], f"code={r.status_code} n={len(msgs)}")

        r = client.delete("/conversations/conv-B1", params={"user_id": "u-B"}, headers=_hdr(TOKEN_A))
        record("A 改 user_id 删 B 会话：删不掉（deleted=false）",
               r.status_code == 200 and r.json().get("deleted") is False,
               f"code={r.status_code} body={r.text[:120]}")

        r = client.get("/conversations/conv-B1/messages",
                       params={"user_id": "u-B"}, headers=_hdr(TOKEN_B))
        record("B 自己的会话完好（隔离未破坏 B 的数据）",
               r.status_code == 200 and len(r.json().get("messages", [])) >= 2,
               f"code={r.status_code} n={len(r.json().get('messages', [])) if r.status_code == 200 else -1}")

        # ── 3) 工单/上报编号隔离 ─────────────────────────────────────────────
        r = client.post("/chat", json={"message": "车间着火了怎么办",
                                       "user_id": "u-B", "conv_id": "conv-emg"},
                        headers=_hdr(TOKEN_A))
        rid_a = r.json().get("request_id", "") if r.status_code == 200 else ""
        record("A 上报应急：拿到上报编号且响应主体为 u-A",
               r.status_code == 200 and bool(rid_a) and r.json().get("subject_id") == "u-A",
               f"code={r.status_code} rid={rid_a}")

        pa = client.get(f"/tickets/progress/{rid_a}", headers=_hdr(TOKEN_A))
        record("A 查自己的上报编号：可见（found=true）",
               pa.status_code == 200 and pa.json().get("found") is True,
               f"code={pa.status_code} body={pa.text[:120]}")

        pb = client.get(f"/tickets/progress/{rid_a}", headers=_hdr(TOKEN_B))
        record("B 查 A 的上报编号：found=false（不泄露存在性）",
               pb.status_code == 200 and pb.json().get("found") is False,
               f"code={pb.status_code} body={pb.text[:120]}")

        # ── 4) 角色边界（处理者不能越权验收）───────────────────────────────
        r = client.post("/tickets", json={"title": "隔离验收测试单", "due_hours": 48},
                        headers=_ahdr(TOKEN_A))
        tid = r.json().get("ticket_id", 0)
        _advance_to_feedback(client, tid)

        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-A", "note": "我给验收"},
                        headers=_ahdr(TOKEN_A))
        record("A（无 verifier）transition accept → 403",
               r.status_code == 403, f"code={r.status_code} body={r.text[:120]}")

        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-B", "note": ""},
                        headers=_ahdr(TOKEN_B))
        record("B（verifier）accept 但批注为空 → 400",
               r.status_code == 400, f"code={r.status_code} body={r.text[:120]}")

        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-B", "note": "整改到位，验收通过"},
                        headers=_ahdr(TOKEN_B))
        record("B（verifier）带验收批注 accept → 成功闭环",
               r.status_code == 200 and r.json().get("ticket", {}).get("stage") == "accept",
               f"code={r.status_code} body={r.text[:160]}")

        # 旧兼容 close：缺整改证据 → 400；无验收角色 → 403；验收者带证据 → 成功
        r2 = client.post("/tickets", json={"title": "隔离旧闭单测试单", "due_hours": 48},
                         headers=_ahdr(TOKEN_A))
        tid2 = r2.json().get("ticket_id", 0)
        _advance_to_feedback(client, tid2)

        r = client.post(f"/tickets/{tid2}/close", headers=_ahdr(TOKEN_A))
        record("A close 缺整改证据 → 400（明确原因，且不泄露工单存在性）",
               r.status_code == 400 and "证据" in r.text, f"code={r.status_code} body={r.text[:160]}")

        r = client.post(f"/tickets/{tid2}/close",
                        json={"note": "整改完成", "evidence": "现场照片"},
                        headers=_ahdr(TOKEN_A))
        record("A 无验收角色 close（带证据）→ 403",
               r.status_code == 403, f"code={r.status_code} body={r.text[:120]}")

        r = client.post(f"/tickets/{tid2}/close",
                        json={"note": "整改完成", "evidence": "现场照片"},
                        headers=_ahdr(TOKEN_B))
        record("B close 带整改证据 → 成功",
               r.status_code == 200 and r.json().get("closed") is True,
               f"code={r.status_code} body={r.text[:120]}")

        # 确认卡越权：建议归属 u-A，B 不能确认
        audit.save_hazard_suggestion("sug-a", "u-A", "conv-sug",
                                     {"decision": {"action": "suggest", "severity": "general",
                                                   "summary": "电气隐患", "dept": "设备动力部",
                                                   "deadline_hours": 72, "basis": "测试"},
                                      "brief": {"entities": {}, "request_id": "sug-a"}})
        r = client.post("/hazard/confirm", json={"request_id": "sug-a", "user_id": "u-B"},
                        headers=_hdr(TOKEN_B))
        record("B 确认 A 的建议 → 403（改 user_id 也不行）",
               r.status_code == 403, f"code={r.status_code} body={r.text[:120]}")

        r = client.post("/hazard/confirm", json={"request_id": "sug-a", "user_id": "u-B"},
                        headers=_hdr(TOKEN_A))
        record("A 确认自己的建议 → 200（服务端主体覆盖客户端 user_id）",
               r.status_code == 200 and r.json().get("ok") is True,
               f"code={r.status_code} body={r.text[:160]}")

        r = client.post("/hazard/confirm", json={"request_id": "no-such-sug"}, headers=_hdr(TOKEN_A))
        record("未知/已处理建议仍 404（既有语义优先级不变）", r.status_code == 404, f"code={r.status_code}")

        # ── 5) 资料 ACL：检索层就按主体范围过滤 ──────────────────────────────
        # 断言只看检索结果（响应里的 "query" 字段会回显查询串，不能当正文看）
        def _results_blob(resp):
            return json.dumps(resp.json().get("results") if resp.status_code == 200 else {},
                              ensure_ascii=False)

        r = client.post("/search/direct", params={"query": PLANT_B_MARK, "top_k": 5},
                        headers=_hdr(TOKEN_A))
        record("A 检索 plant-b 条款：绝不返回他人范围正文",
               r.status_code == 200 and PLANT_B_MARK not in _results_blob(r),
               f"code={r.status_code} results={_results_blob(r)[:200]}")

        r = client.post("/search/direct", params={"query": PLANT_A_MARK, "top_k": 5},
                        headers=_hdr(TOKEN_A))
        record("A 检索本主体范围条款：可见",
               r.status_code == 200 and PLANT_A_MARK in _results_blob(r),
               f"code={r.status_code} results={_results_blob(r)[:200]}")

        r = client.post("/search/direct", params={"query": PUBLIC_MARK, "top_k": 5},
                        headers=_hdr(TOKEN_A))
        record("A 检索公共文档（旧数据 scope 空）：不被范围过滤排除",
               r.status_code == 200 and PUBLIC_MARK in _results_blob(r),
               f"code={r.status_code} results={_results_blob(r)[:200]}")

        r = client.post("/search/direct", params={"query": PLANT_A_MARK, "top_k": 5},
                        headers=_hdr(TOKEN_B))
        record("B 检索 plant-a 条款：同样看不到他人范围正文",
               r.status_code == 200 and PLANT_A_MARK not in _results_blob(r),
               f"code={r.status_code} results={_results_blob(r)[:200]}")

        # /chat 注入上下文（不是只在回答层过滤）
        r = client.post("/chat", json={"message": f"条款编号 {PLANT_B_MARK} 的作业要求是什么",
                                       "user_id": "u-B", "conv_id": "conv-acl"},
                        headers=_hdr(TOKEN_A))
        body = r.json() if r.status_code == 200 else {}
        injected = (api_main._orchestrator.last_req.knowledge or "") + (api_main._orchestrator.last_req.context or "")
        record("A /chat 检索注入中绝不出现 plant-b 正文",
               r.status_code == 200 and PLANT_B_MARK not in injected
               and PLANT_B_MARK not in json.dumps(body.get("citations") or [], ensure_ascii=False),
               f"code={r.status_code} injected={injected[:160]}")

        r = client.post("/chat", json={"message": f"条款编号 {PLANT_A_MARK} 的作业要求是什么",
                                       "user_id": "u-B", "conv_id": "conv-acl2"},
                        headers=_hdr(TOKEN_A))
        injected = (api_main._orchestrator.last_req.knowledge or "") + (api_main._orchestrator.last_req.context or "")
        cits = json.dumps(r.json().get("citations") or [], ensure_ascii=False) if r.status_code == 200 else ""
        record("A /chat 检索命中带访问范围的本主体文档（citations 可断言）",
               r.status_code == 200 and (PLANT_A_MARK in injected or PLANT_A_MARK in cits),
               f"code={r.status_code} injected={injected[:120]} cits={cits[:120]}")

        # ── 6) 本地形态不受影响：仍按客户端 user_id 工作 ──────────────────────
        _local_form_env()
        r = client.get("/conversations", params={"user_id": "u-B"})
        convs = [c["conv_id"] for c in r.json().get("conversations", [])] if r.status_code == 200 else []
        record("本地形态：/conversations 仍按客户端 user_id 工作（现状不变）",
               r.status_code == 200 and "conv-B1" in convs, f"code={r.status_code} convs={convs}")

        r = client.post("/chat", json={"message": "你好", "user_id": "z-local", "conv_id": "conv-local"},
                        headers={})
        rl = client.get("/conversations", params={"user_id": "z-local"})
        convs = [c["conv_id"] for c in rl.json().get("conversations", [])] if rl.status_code == 200 else []
        record("本地形态：/chat 按客户端 user_id 落盘且 subject_id 为空（本地可信主体）",
               r.status_code == 200 and r.json().get("subject_id") == "" and "conv-local" in convs,
               f"code={r.status_code} subject_id={r.json().get('subject_id') if r.status_code == 200 else '?'} convs={convs}")

        _multi_user_env()

        # ── 7) 无主体令牌/令牌不匹配 → 不泄露、不越权 ────────────────────────
        r = client.post("/chat", json={"message": "受限空间取样", "user_id": "u-A", "conv_id": "c-x"})
        record("无访问令牌访问 /chat → 401（沿用 access_gate 语义）", r.status_code == 401, f"code={r.status_code}")
        r = client.get("/conversations", params={"user_id": "u-A"})
        record("无访问令牌访问 /conversations → 401", r.status_code == 401, f"code={r.status_code}")
        r = client.get(f"/tickets/progress/{rid_a}")
        record("无访问令牌访问 /tickets/progress → 401", r.status_code == 401, f"code={r.status_code}")

        r = client.get("/conversations", params={"user_id": "u-A"},
                       headers={"X-Access-Token": ACCESS})
        convs = [c["conv_id"] for c in r.json().get("conversations", [])] if r.status_code == 200 else []
        record("有访问令牌但无主体令牌：读不到 u-A 的会话（匿名主体）",
               r.status_code == 200 and "conv-A1" not in convs, f"code={r.status_code} convs={convs}")

        r = client.post("/knowledge/add", json={"documents": [{"title": "越权入库", "content": "x"}]},
                        headers=_hdr(TOKEN_A))
        record("非管理员角色入库 → 403", r.status_code == 403, f"code={r.status_code} body={r.text[:120]}")

        r = client.post("/knowledge/add", json={"documents": [{"title": "合法入库", "content": "公共内容", "scope": ""}]},
                        headers=_hdr(TOKEN_B))
        record("普通用户（非 admin）入库同样 403（角色边界）", r.status_code == 403, f"code={r.status_code}")
    finally:
        try:
            audit.close()
        except Exception:
            pass
        # 释放知识库句柄（删集合 + 关客户端 + 清共享系统缓存 + gc）与打桩引用；
        # 未释放句柄时 Windows 会拒绝 rmtree，也会污染下一次同目录打开（已在残留目录上复现）
        api_main._tool_manager = None
        api_main._orchestrator = None
        api_main._memory = None
        _release_kb(kb)
        kb = conv = None
        # 请求级资料范围绑定必须已 reset；万一残留（端点异常路径），兜底清掉避免串味
        try:
            from mcp.knowledge_base import SCOPES_CONTEXT
            if SCOPES_CONTEXT.get() is not None:
                SCOPES_CONTEXT.set(None)
        except Exception:
            pass
        gc.collect()
        shutil.rmtree(str(tmp_root), ignore_errors=True)
        residue = [p.name for p in tmp_root.parent.glob(f"{tmp_root.name}*")]
        if residue:
            print(f"[WARN] 临时目录残留未清干净（句柄未释放？）: {residue}", flush=True)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
