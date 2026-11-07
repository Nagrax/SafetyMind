# -*- coding: utf-8 -*-
# ngram 嵌入模式下知识库得分口径回归（SM-01 簇 A，2026-11-07）。
#
# ## 根因（red-repros/test_e2e_gate.py.md §3.1 / test_eval_gate_endpoint.py.md §4.2）
#
# KB 集合创建未设 hnsw:space（mcp/knowledge_base.py 两个 get_or_create_collection，
# metadata 只有 description）→ ChromaDB 默认 **L2 空间**，query 返回的 dist 是
# 单位向量间的平方欧氏距离 = 2 − 2·cos。旧打分 `score = 1 − dist` 在该空间下
# 等于 **2cos − 1**，零点落在 cos=0.5 而非"无关/正交"：词面真实相关的命中
# （实测 cos≈0.17~0.39）得到系统性负分，被 api/main.py 的 SAFETYMIND_RAG_MIN_SCORE
# 默认 0.0 阈值全数过滤 → citations=[]、knowledge_used=False（3 个测试 12 条断言的红根因）。
#
# ## 修复（本测试锁定）
#
# ngram（含 auto 回退）模式下 score 换算为真实余弦 `1 − dist/2`；0.0 阈值随之恢复
# api/main.py:1885 注释声称的"负分=比随机还不相关"余弦语义。BGE / 服务器模式的
# 生产行为按旧口径（1 − dist）标定，**逐位零变化**；显式声明非 L2 空间的集合不换算。
#
# ## 本测试断言
#   1) ngram 模式下词面相关命中的 score 为正，且逐位等于真余弦（1 − dist/2）；
#   2) 该得分能通过**默认阈值**（SAFETYMIND_RAG_MIN_SCORE 未设 = 0.0）进入 citations；
#   3) 口径门控：仅"本地 ngram + 默认/L2 空间"换算；bge 命名、服务器模式（无嵌入函数）、
#      显式 cosine 空间均不换算（BGE 生产路径零改动）；
#   4) BGE 命名实例的检索得分保持旧口径 1 − dist（逐位）；
#   5) 阈值消费方未被改动（源码级守护：默认 "0" + 严格大于）。
#
# 用法: python tests/test_ngram_score_semantics.py
import asyncio
import gc
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 硬设 ngram：本测试锁定 ngram 口径（setdefault 会被外层覆盖跑出另一套失败面）
os.environ["SAFETYMIND_EMBEDDING"] = "ngram"
# 阈值消费方读默认值：本测试验证的就是默认 0.0 的语义（外部若设过该变量则先摘掉，
# 测毕恢复）
_SAVED_MIN_SCORE = os.environ.pop("SAFETYMIND_RAG_MIN_SCORE", None)

# 存储隔离（与 runner child_env 同款键）：本测试不触碰仓库 data/ 与共享存储
_TMP_ROOT = Path(tempfile.gettempdir())
_ISO = Path(tempfile.mkdtemp(prefix="ngram_score_sem_"))
os.environ.setdefault("AUDIT_DB_PATH", str(_ISO / "audit.db"))
os.environ.setdefault("CHROMA_PERSIST_DIRECTORY", str(_ISO / "chroma"))
os.environ.setdefault("CHROMA_KB_PERSIST_DIRECTORY", str(_ISO / "kb_chroma"))
os.environ.setdefault("SAFETYMIND_CONVERSATIONS_DIR", str(_ISO / "conversations"))
os.environ.setdefault("PROMETHEUS_PORT", "0")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


TAIL_CLAUSE = "二级动火票有效期不超过72小时UNIQ7788，作业结束后作业人须立即关闭动火票。"
FILLER = "特殊作业前必须完成风险辨识与安全交底。现场作业人员应确认作业票证齐全有效。监护人员须全程在岗并保持通讯畅通。"
MSG = "动火作业票有效期不超过72小时UNIQ7788怎么规定的"


def _rmtree(p):
    for _ in range(5):
        shutil.rmtree(p, ignore_errors=True)
        if not p.exists():
            break


def _manual_cos(a_text: str, b_text: str) -> float:
    from core.local_embedding import ngram_vector
    va, vb = ngram_vector(a_text), ngram_vector(b_text)
    return sum(x * y for x, y in zip(va, vb))


def phase_kb_score_semantics():
    """KB 层：ngram 模式下词面相关命中得分为正且等于真余弦。"""
    from tests._storage_guard import build_kb_checked, release_kb
    from mcp.knowledge_base import KnowledgeBase

    long_doc = FILLER * 6 + TAIL_CLAUSE
    kb, kb_dir = build_kb_checked(
        _TMP_ROOT,
        lambda d: KnowledgeBase(chroma_host="127.0.0.1", chroma_port=1, chroma_path=str(d)),
        lambda k: k.add_documents([{"title": "动火作业管理规定", "content": long_doc,
                                    "source": "内部制度", "version": "2026-03"}]),
        "UNIQ7788", name="ngram_sem_kb_")
    try:
        record("ngram 实例命中 L2+单位向量得分口径（_l2_unit_score）",
               kb._l2_unit_score is True, f"flag={kb._l2_unit_score}")

        hits = kb.search(MSG, top_k=3)
        record("词面相关查询有命中", bool(hits), "hits=[]")
        if not hits:
            return
        top = hits[0]
        record("ngram 模式下词面相关命中得分为正（默认阈值 0.0 可保留）",
               isinstance(top.get("score"), (int, float)) and top["score"] > 0.0,
               f"score={top.get('score')}")
        record("命中片段含查询锚点（UNIQ7788）", "UNIQ7788" in str(top.get("snippet", "")),
               str(top.get("snippet", ""))[:60])

        # 原始 Chroma 距离（白盒）：逐位锁定换算公式 score = 1 − dist/2
        raw = kb._collection.query(query_texts=[MSG], n_results=1)
        dist = float((raw.get("distances") or [[None]])[0][0])
        expect_new = round(1.0 - dist / 2.0, 4)
        expect_old = round(1.0 - dist, 4)
        record("score 逐位等于真余弦 1 − dist/2",
               top["score"] == expect_new,
               f"score={top['score']} expect={expect_new} raw_dist={dist}")
        record("旧口径 1 − dist（=2cos−1）不再是输出值（度量失配已修正）",
               dist > 0.4 and top["score"] != expect_old,
               f"dist={dist} old={expect_old}")
        # 独立余弦交叉验证（不依赖 chroma 数值链路）
        cos = _manual_cos(MSG, str(top.get("snippet", "")))
        record("score 与手算余弦一致（±1e-3）", abs(top["score"] - cos) <= 1e-3,
               f"score={top['score']} manual_cos={cos:.6f}")

        # 仿射换算保序：多块文档的父子聚合仍取"最相关子块"所在父文档
        long_doc2 = FILLER * 20 + TAIL_CLAUSE      # >500 字 → 多子块
        kb.add_documents([{"title": "动火作业管理规定（长文）", "content": long_doc2}])
        hits2 = kb.search(MSG, top_k=3)
        record("多子块聚合不受换算影响（命中含锚点的子块所在文档得分最高）",
               hits2 and "UNIQ7788" in str(hits2[0].get("snippet", ""))
               and hits2[0]["score"] > 0.0,
               f"top_score={hits2[0]['score'] if hits2 else None}")
    finally:
        release_kb(kb)
        gc.collect()
        _rmtree(kb_dir)


def phase_default_threshold_to_citations():
    """注入链路：默认阈值（未设 SAFETYMIND_RAG_MIN_SCORE = 0.0）下命中进入 citations。"""
    import api.main as api_main
    from core.intent_recognizer import IntentCategory
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager, Tool
    from tests._storage_guard import build_kb_checked, release_kb

    saved_tm = getattr(api_main, "_tool_manager", None)
    kb = None
    kb_dir = None
    try:
        kb, kb_dir = build_kb_checked(
            _TMP_ROOT,
            lambda d: KnowledgeBase(chroma_host="127.0.0.1", chroma_port=1,
                                    chroma_path=str(d)),
            lambda k: k.add_documents([{"title": "动火作业管理规定",
                                        "content": FILLER * 6 + TAIL_CLAUSE,
                                        "source": "内部制度", "version": "2026-03"}]),
            "UNIQ7788", name="ngram_sem_cit_")
        tm = MCPToolManager(api_key="test-key")
        tm.register(Tool(name="knowledge_search", description="t",
                         handler=kb.search_handler,
                         schema={"type": "object",
                                 "properties": {"query": {"type": "string"}},
                                 "required": ["query"]}, cache_ttl=0.0))
        api_main._tool_manager = tm

        record("默认阈值语义验证前提：SAFETYMIND_RAG_MIN_SCORE 未设（用默认 0.0）",
               os.getenv("SAFETYMIND_RAG_MIN_SCORE") is None,
               f"value={os.getenv('SAFETYMIND_RAG_MIN_SCORE')}")
        text, used, citations = asyncio.run(api_main._build_knowledge_context(
            MSG, intent=IntentCategory.REGULATION_QUERY))
        record("知识通道被使用（knowledge_used=True）", used is True, f"used={used}")
        record("ngram 词面相关命中通过默认阈值 0.0 进入 citations（≥1 条）",
               len(citations) >= 1, f"citations={len(citations)}")
        if citations:
            record("citation 得分为正（真余弦口径）",
                   isinstance(citations[0].get("score"), (int, float))
                   and citations[0]["score"] > 0.0,
                   f"score={citations[0].get('score')}")
            record("citation 指向命中片段（snippet 含锚点）",
                   "UNIQ7788" in str(citations[0].get("snippet", "")),
                   str(citations[0].get("snippet", ""))[:60])
        record("注入文本携带命中条款（非空注入）", "UNIQ7788" in (text or ""),
               (text or "")[:80])
    finally:
        api_main._tool_manager = saved_tm
        release_kb(kb)
        gc.collect()
        if kb_dir:
            _rmtree(kb_dir)


def phase_score_mode_gate():
    """口径门控：只有"本地 ngram + 默认/L2 空间"换算；BGE/服务器/cosine 空间不换算。"""
    from core.local_embedding import LocalEmbeddingFunction
    from mcp.knowledge_base import _l2_unit_score_mode

    class _FakeBGE:
        def name(self):
            return "safetymind-bge-base-zh-v1.5"

    class _Weird:
        def name(self):
            raise RuntimeError("boom")

    record("本地 ngram + 默认空间（无 hnsw:space）→ 换算",
           _l2_unit_score_mode(LocalEmbeddingFunction(), {"description": "x"}) is True)
    record("本地 ngram + 显式 l2 → 换算",
           _l2_unit_score_mode(LocalEmbeddingFunction(), {"hnsw:space": "l2"}) is True)
    record("本地 ngram + 显式 cosine 空间 → 不换算（公式前提不成立）",
           _l2_unit_score_mode(LocalEmbeddingFunction(), {"hnsw:space": "cosine"}) is False)
    record("BGE 命名嵌入 → 不换算（生产默认路径零改动）",
           _l2_unit_score_mode(_FakeBGE(), {}) is False)
    record("服务器模式（无嵌入函数）→ 不换算",
           _l2_unit_score_mode(None, {}) is False)
    record("name() 异常的嵌入函数 → 不换算（保守回落旧口径）",
           _l2_unit_score_mode(_Weird(), {}) is False)


def phase_bge_scale_unchanged():
    """BGE 路径零改动：bge 命名实例的检索得分保持旧口径 1 − dist（逐位）。"""
    import core.bge_embedding as bge_mod
    from core.local_embedding import ngram_vector
    from mcp.knowledge_base import KnowledgeBase

    class _FakeBGEFn:
        """bge 命名的假嵌入器（离线、确定性）：向量用 ngram 单位向量代替，
        只为验证"bge 命名 → 不换算"的实例级行为，不加载真实模型。"""

        def __call__(self, input):  # noqa: A002 - chroma 约定
            return [ngram_vector(t if isinstance(t, str) else str(t)) for t in input]

    def embed_documents(self, input):
        return self(input)

    def embed_query(self, input):
        return self(input)

    def name(self):
        return "safetymind-bge-fake-v1.5"

    _FakeBGEFn.embed_documents = embed_documents
    _FakeBGEFn.embed_query = embed_query
    _FakeBGEFn.name = name

    saved_env = os.environ.get("SAFETYMIND_EMBEDDING")
    saved_fn = bge_mod.try_bge_embedding
    kb = None
    kb_dir = None
    try:
        os.environ["SAFETYMIND_EMBEDDING"] = "bge"
        bge_mod.try_bge_embedding = lambda *a, **k: _FakeBGEFn()
        kb_dir = Path(tempfile.mkdtemp(prefix="ngram_sem_bge_", dir=_TMP_ROOT))
        kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=1, chroma_path=str(kb_dir))
        kb.add_documents([{"title": "动火作业管理规定",
                           "content": FILLER * 6 + TAIL_CLAUSE}])
        record("bge 命名实例不进入换算口径（_l2_unit_score=False）",
               kb._l2_unit_score is False, f"flag={kb._l2_unit_score}")
        hits = kb.search(MSG, top_k=1)
        raw = kb._collection.query(query_texts=[MSG], n_results=1)
        dist = float((raw.get("distances") or [[None]])[0][0])
        record("bge 命名实例得分保持旧口径 1 − dist（逐位，生产零变化）",
               bool(hits) and hits[0]["score"] == round(1.0 - dist, 4),
               f"score={hits[0]['score'] if hits else None} expect={round(1.0 - dist, 4)}")
    finally:
        bge_mod.try_bge_embedding = saved_fn
        if saved_env is None:
            os.environ.pop("SAFETYMIND_EMBEDDING", None)
        else:
            os.environ["SAFETYMIND_EMBEDDING"] = saved_env
        from tests._storage_guard import release_kb
        release_kb(kb)
        gc.collect()
        if kb_dir:
            _rmtree(kb_dir)


def phase_threshold_consumer_frozen():
    """阈值消费方是既有决策（0.0 默认 + 严格大于），本修复不得触碰（源码级守护）。"""
    src = (Path(__file__).resolve().parents[1] / "api" / "main.py").read_text(encoding="utf-8")
    record("api/main.py 仍以默认 '0' 读取 SAFETYMIND_RAG_MIN_SCORE",
           '_min_score = float(os.getenv("SAFETYMIND_RAG_MIN_SCORE", "0") or 0)' in src)
    record("api/main.py 过滤条件仍为严格大于（_sc > _min_score）",
           "if _sc > _min_score:" in src)


def main():
    phase_score_mode_gate()
    phase_kb_score_semantics()
    phase_default_threshold_to_citations()
    phase_bge_scale_unchanged()
    phase_threshold_consumer_frozen()

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    rc = 1 if failed else 0
    if _SAVED_MIN_SCORE is not None:
        os.environ["SAFETYMIND_RAG_MIN_SCORE"] = _SAVED_MIN_SCORE
    sys.exit(rc)


if __name__ == "__main__":
    main()
