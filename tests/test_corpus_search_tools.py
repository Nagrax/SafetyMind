# -*- coding: utf-8 -*-
# K03+K07 真实入口测试（打桩 LLM + 真实 corpus 小样本 + 真实工具管理器/端点）：
# 用法: D:/claudecode/SafetyMind/.venv/Scripts/python.exe tests/test_corpus_search_tools.py
# 覆盖：
#   - corpus_search 工具经 MCPToolManager 真被调用且结果进入注入（复用 test_chat_tool_evidence 打法）；
#   - /search/direct?source=corpus 与 ?source=all 返回同一 build marker；
#   - /chat 的 citations 带 build_id 且不含"目录卡当正文"；
#   - asset_open 对有原件返回路径/字节，对无原件如实回未落盘；
#   - 多用户形态下 A 的 scopes 检索不到 B 的 access_scope 文档。
# 铁律 9：corpus/chroma 全部走 scratch 下独立目录；
# 铁律 2：用户端/设计者端分离——本用例只走用户端 /chat 与检索端点，管理端点不在此覆盖。
import asyncio
import gc
import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 共享的存储自检 helper

import _storage_guard as guard   # 共享的"存储自检 + 一次重建重试"（只依赖标准库）

SCRATCH = Path("D:/claudecode/scratch/sm-corpus-20261019")
MARK = "UNIQ-TOOL-7788"
SCOPE_A_MARK = "UNIQ-SCOPE-A-11"
SCOPE_B_MARK = "UNIQ-SCOPE-B-22"

TOKEN_A = "tok-corpus-a"
TOKEN_B = "tok-corpus-b"
ACCESS = "gate-corpus-7"

RESULTS = []


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ── 打桩（不依赖 LLM），复用 test_chat_tool_evidence 的打法 ──────────────────
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


class FakeOrchestrator:
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


def _fresh(name: str) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    p = SCRATCH / name
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True)
    return p


def _seed_doc(m, item_id, title, content, *, scope="", provider="user_imported",
              parse_status="parsed", acq="parsed"):
    sid = m.upsert_source(provider, item_id, title=title,
                          acquisition_status=acq, parse_status=parse_status)
    lg = m.make_logical_doc_id(provider, item_id=item_id)
    vid = m.make_version_id(lg, "v1")
    m.add_version(lg, vid, sid, title, "", access_scope=scope)
    if content:
        m.add_blocks(vid, [{"content": content, "locator": {"para": 1},
                            "evidence_kind": "body"}])
    return vid


def _seed_and_build(root: Path):
    """自有资料（含唯一标记）+ 带原件条目 + 两厂范围文档，然后 build 并激活。"""
    from corpus.build import activate_build
    from corpus.manifest import CorpusManifest
    seed = root / "seed.json"
    seed.write_text(json.dumps([
        {"title": "动火作业管理规定",
         "content": f"动火票有效期不超过72小时，本条标记 {MARK} 用于端到端检出。",
         "meta": {"big": "操作技术"}},
    ], ensure_ascii=False), encoding="utf-8")

    from corpus.pipeline import import_user_owned
    import_user_owned(root, seed_path=seed)

    m = CorpusManifest(root / "manifest.sqlite")
    try:
        # 带原件的条目（toolbox_acquired）：登记资产 + 条目映射
        asset = m.register_asset(b"%PDF-1.4 corpus-original-bytes-for-asset-open",
                                 mime="application/pdf")
        m.upsert_source("toolbox_acquired", "tb-1", title="百宝箱已取得样本",
                        acquisition_status="acquired_verified", parse_status="parsed")
        m.link_asset("tb-1", asset)
        _seed_doc(m, "sa", "甲厂制度", f"甲厂条款 {SCOPE_A_MARK} 的作业要求。",
                  provider="other_verified_source", scope="plant-a")
        _seed_doc(m, "sb", "乙厂制度", f"乙厂条款 {SCOPE_B_MARK} 的作业要求。",
                  provider="other_verified_source", scope="plant-b")
    finally:
        m.close()
    # 自检存储可用后再谈检索行为（自检不过会删该 build 目录重建一次）
    meta = guard.build_index_checked("tools-build", root=root, probe=MARK)
    activate_build("tools-build", root)
    return meta, asset


def _build_stack(root: Path, marker):
    """真实 MCPToolManager + 真实 KB + corpus 工具与提供者（复刻 lifespan 接线）。"""
    from corpus.search import CorpusSearch
    from corpus.search import open_asset as corpus_open_asset
    from mcp.knowledge_base import SCOPES_CONTEXT, set_corpus_provider
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager, Tool

    def _mk_kb(chroma_dir):
        kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=1, chroma_path=str(chroma_dir))
        for col in (kb._collection, kb._parents):
            ids = col.get()["ids"]
            if ids:
                col.delete(ids=ids)
        return kb

    # 本地 ChromaDB 偶发持久 HNSW 段不可读（全新目录也可能）：播种后自检，
    # 自检不过换全新目录重建一次，避免把环境抖动记成产品缺陷
    kb, kb_dir = guard.build_kb_checked(
        SCRATCH,
        _mk_kb,
        lambda k: k.add_documents([{"title": "既有知识库文档",
                                   "content": "既有知识库中的通用安全要求，与 corpus 并列。",
                                   "source": "内部制度", "version": "2026-01"}]),
        "既有知识库中的通用安全要求", name="tools_kb_chroma")

    def _search_sync(query, top_k, scopes):
        cs = CorpusSearch()
        try:
            return cs.search(query, top_k=top_k, scopes=scopes)
        finally:
            cs.close()

    def corpus_provider(query, scopes=None):
        return _search_sync(query, 6, scopes)

    async def corpus_search_handler(params, context):
        scopes = params.get("scopes")
        if scopes is None and isinstance(context, dict):
            scopes = context.get("scopes")
        if scopes is None:
            scopes = SCOPES_CONTEXT.get()
        return await asyncio.to_thread(_search_sync, str(params.get("query", "")),
                                       int(params.get("top_k") or 8), scopes)

    def corpus_search_fallback(params, context, error):
        return {"blocks": [], "cards": [], "build": None, "fallback": True, "error": error}

    async def asset_open_handler(params, context):
        key = str(params.get("asset") or "")
        return await asyncio.to_thread(corpus_open_asset, key)

    def asset_open_fallback(params, context, error):
        return {"found": False, "is_materialized": False, "error": error}

    tm = MCPToolManager(api_key="test-key")
    tm.register(Tool(name="knowledge_search", description="t", handler=kb.search_handler,
                     schema={"type": "object", "properties": {"query": {"type": "string"},
                                                              "top_k": {"type": "integer"}},
                             "required": ["query"]}, cache_ttl=0.0))
    tm.register(Tool(name="corpus_search", description="t", handler=corpus_search_handler,
                     schema={"type": "object", "properties": {"query": {"type": "string"},
                                                              "top_k": {"type": "integer"},
                                                              "scopes": {"type": "array"}},
                             "required": ["query"]}, cache_ttl=0.0))
    tm.register(Tool(name="asset_open", description="t", handler=asset_open_handler,
                     schema={"type": "object", "properties": {"asset": {"type": "string"}},
                             "required": ["asset"]}, cache_ttl=0.0))
    set_corpus_provider(corpus_provider)

    import api.main as api_main
    api_main._tool_manager = tm
    api_main._memory = FakeMemory()
    api_main._orchestrator = FakeOrchestrator()
    api_main._audit = None
    api_main._conv_store = None
    return api_main, tm, kb, kb_dir


def main():
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_EMERGENCY_FASTPATH": "0",
    })
    os.environ.pop("SAFETYMIND_DEPLOYED", None)
    os.environ.pop("ACCESS_TOKEN", None)
    os.environ.pop("SAFETYMIND_USERS", None)

    root = _fresh("tools_root")
    os.environ["SAFETYMIND_CORPUS_ROOT"] = str(root)
    meta, asset = _seed_and_build(root)
    marker = meta["marker"]
    api_main, tm, kb, kb_dir = _build_stack(root, marker)

    from fastapi.testclient import TestClient
    client = TestClient(api_main.app)

    try:
        # ── 1. corpus_search 工具真经 ToolManager 调用，结果进入注入 ─────────
        res = asyncio.new_event_loop().run_until_complete(
            tm.call("corpus_search", {"query": MARK, "top_k": 5}, {}))
        data = res.data if res.success else {}
        record("corpus_search 经 ToolManager 调用成功",
               res.success and isinstance(data, dict), f"success={res.success} err={res.error}")
        record("corpus_search 结果含唯一标记正文（blocks）",
               any(MARK in b.get("content", "") for b in (data.get("blocks") or [])),
               f"blocks={[b.get('content','')[:24] for b in (data.get('blocks') or [])]}")
        record("corpus_search 结果带 build marker",
               (data.get("build") or {}).get("marker") == marker,
               f"build={data.get('build')}")
        stats = tm.get_stats()
        record("工具统计记录 corpus_search 调用次数",
               stats.get("corpus_search", {}).get("total", 0) >= 1, f"stats={stats.get('corpus_search')}")

        src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
        record("默认工具白名单包含 corpus_search 与 asset_open",
               "corpus_search,asset_open" in src.replace("\n", "").replace(" ", ""),
               "allowlist 默认值未含新工具")

        # ── 2. /chat：唯一标记进入注入 + citations 带 build_id ───────────────
        r = client.post("/chat", json={"message": f"标记 {MARK} 对应的动火作业规定是什么",
                                       "user_id": "u1", "conv_id": "c-corpus"})
        body = r.json() if r.status_code == 200 else {}
        injected = (api_main._orchestrator.last_req.knowledge or "") + \
                   (api_main._orchestrator.last_req.context or "")
        cits = body.get("citations") or []
        record("/chat 唯一标记进入注入（正文可达）",
               r.status_code == 200 and MARK in injected, f"code={r.status_code} inj={injected[:120]}")
        record("/chat citations 命中 corpus 正文且带同一 build_id",
               any(c.get("build_id") == "tools-build" and c.get("evidence_kind") == "body"
                   and MARK in str(c.get("snippet", "")) for c in cits),
               f"cits={[(c.get('evidence_kind'), c.get('build_id'), str(c.get('snippet'))[:16]) for c in cits]}")
        record("/chat 引子里不含'目录卡当正文'（catalog 一律 catalog_only=True）",
               all(not (c.get("evidence_kind") == "catalog" and c.get("catalog_only") is False)
                   for c in cits),
               f"bad={[c for c in cits if c.get('evidence_kind')=='catalog' and c.get('catalog_only') is False]}")

        # ── 3. /search/direct source=corpus / all 同 build ───────────────────
        rc = client.post("/search/direct", params={"query": MARK, "top_k": 5, "source": "corpus"})
        rj = rc.json() if rc.status_code == 200 else {}
        record("/search/direct?source=corpus 返回 build marker 且命中标记",
               rc.status_code == 200 and (rj.get("build") or {}).get("marker") == marker
               and MARK in json.dumps(rj.get("results"), ensure_ascii=False),
               f"code={rc.status_code} build={rj.get('build')}")
        ra = client.post("/search/direct", params={"query": MARK, "top_k": 5, "source": "all"})
        aj = ra.json() if ra.status_code == 200 else {}
        record("/search/direct?source=all 返回同一 build marker",
               ra.status_code == 200 and (aj.get("build") or {}).get("marker") == marker,
               f"code={ra.status_code} build={aj.get('build')}")
        rk = client.post("/search/direct", params={"query": "安全要求", "top_k": 5, "source": "kb"})
        kj = rk.json() if rk.status_code == 200 else {}
        record("/search/direct?source=kb 不含 corpus build（既有契约不变）",
               rk.status_code == 200 and "build" not in kj, f"code={rk.status_code} keys={list(kj.keys())}")

        # ── 4. asset_open：有原件 / 无原件 ──────────────────────────────────
        a1 = asyncio.new_event_loop().run_until_complete(
            tm.call("asset_open", {"asset": "tb-1"}, {}))
        d1 = a1.data or {}
        record("asset_open 有原件：返回路径/字节且 is_materialized=True",
               a1.success and d1.get("found") is True and d1.get("is_materialized") is True
               and bool(d1.get("path")) and int(d1.get("bytes", 0)) > 0,
               f"data={d1}")
        record("asset_open 有原件：路径真实存在",
               bool(d1.get("path")) and Path(str(d1["path"])).exists())
        from corpus.manifest import CorpusManifest
        m = CorpusManifest(root / "manifest.sqlite")
        try:
            user_item = [s for s in m.list_sources(provider="user_imported")
                         if s["title"] == "动火作业管理规定"][0]["item_id"]
        finally:
            m.close()
        a2 = asyncio.new_event_loop().run_until_complete(
            tm.call("asset_open", {"asset": user_item}, {}))
        d2 = a2.data or {}
        record("asset_open 无原件：如实回未落盘（found=False/is_materialized=False）",
               a2.success and d2.get("found") is False and d2.get("is_materialized") is False
               and "无原件" in str(d2.get("blocking_reason", "")), f"data={d2}")

        # ── 5. 多用户形态：A 的 scopes 检索不到 B 的 access_scope 文档 ───────
        os.environ.update({
            "SAFETYMIND_DEPLOYED": "1",
            "ACCESS_TOKEN": ACCESS,
            "SAFETYMIND_USERS": json.dumps([
                {"token": TOKEN_A, "subject_id": "u-A", "roles": ["reporter"], "scopes": ["plant-a"]},
                {"token": TOKEN_B, "subject_id": "u-B", "roles": ["reporter"], "scopes": ["plant-b"]},
            ], ensure_ascii=False),
        })
        hdr = {"X-Access-Token": ACCESS, "X-User-Token": TOKEN_A}
        rb = client.post("/search/direct",
                         params={"query": f"条款 {SCOPE_B_MARK}", "top_k": 5, "source": "corpus"},
                         headers=hdr)
        blob_b = json.dumps(rb.json().get("results") if rb.status_code == 200 else {},
                            ensure_ascii=False)
        record("多用户形态：A 检索不到 B 的 access_scope 正文",
               rb.status_code == 200 and SCOPE_B_MARK not in blob_b,
               f"code={rb.status_code} results={blob_b[:200]}")
        r_a = client.post("/search/direct",
                          params={"query": f"条款 {SCOPE_A_MARK}", "top_k": 5, "source": "corpus"},
                          headers=hdr)
        blob_a = json.dumps(r_a.json().get("results") if r_a.status_code == 200 else {},
                            ensure_ascii=False)
        record("多用户形态：A 检索得到本范围正文",
               r_a.status_code == 200 and SCOPE_A_MARK in blob_a,
               f"code={r_a.status_code} results={blob_a[:200]}")

        # ── 6. /corpus/status 管理端点（本地形态免密）────────────────────────
        os.environ.pop("SAFETYMIND_DEPLOYED", None)
        os.environ.pop("ACCESS_TOKEN", None)
        os.environ.pop("SAFETYMIND_USERS", None)
        rs = client.get("/corpus/status", headers={"X-Admin-Token": os.getenv("ADMIN_TOKEN", "")})
        sj = rs.json() if rs.status_code == 200 else {}
        record("/corpus/status 返回 ACTIVE build 与 marker",
               rs.status_code == 200 and (sj.get("active_build") or {}).get("build_id") == "tools-build"
               and (sj.get("active_build") or {}).get("marker") == marker,
               f"code={rs.status_code} active={sj.get('active_build')}")
        record("/corpus/status 含三台阶计数与 stopped_by",
               isinstance(sj.get("ladder"), dict) and "catalog_entries" in sj["ladder"]
               and "originals_materialized" in sj["ladder"]
               and sj.get("builds", [{}])[0].get("stopped_by") is not None,
               f"ladder={sj.get('ladder')}")
    finally:
        api_main._tool_manager = None
        api_main._orchestrator = None
        api_main._memory = None
        try:
            from mcp.knowledge_base import set_corpus_provider
            set_corpus_provider(None)
        except Exception:
            pass
        # 显式释放知识库句柄（删集合/关客户端/清共享系统缓存）后再删目录
        guard.release_kb(kb)
        gc.collect()
        # 知识库目录名带进程内序号（含自检失败后的重建目录），统一扫掉
        for d in [root, kb_dir] + sorted(SCRATCH.glob("tools_kb_chroma*")):
            if not guard.rmtree_retry(d):
                print(f"[WARN] 临时目录残留未清干净（句柄未释放？）: {d}", flush=True)

    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} PASSED")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
