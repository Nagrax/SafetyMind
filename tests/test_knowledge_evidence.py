# -*- coding: utf-8 -*-
# 知识库证据保真（P0-B / S-03 检索侧）单测：
#   命中条款进入 snippet 与邻近上下文、保留可引用身份（doc/chunk/version/hash）、
#   更新时同 title 子块 upsert 不残留、目录元数据与可引用正文分离。
# 用临时目录的本地 ChromaDB（n-gram 嵌入），目录名带 _test 后缀并在结束时清理。
# 用法: python tests/test_knowledge_evidence.py（退出码 0=全过）
import gc
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 共享的存储自检 helper

# 本地确定性 n-gram 嵌入：零下载、零第三方依赖，避免测试期加载 BGE
# 必须在任何可能构造 KnowledgeBase 的导入之前设置（导入次序 = 生效次序）
os.environ["SAFETYMIND_EMBEDDING"] = "ngram"

import _storage_guard as guard   # 共享的"存储自检 + 一次重建重试"（只依赖标准库）

SCRATCH = Path("d:/claudecode/scratch/sm-iter-20261019")

RESULTS = []
_OPEN_KBS = []   # 持有的知识库句柄，结束前统一释放，保证临时库目录可删除


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _mk_kb(chroma_dir):
    """全新目录里的知识库，并清掉 __init__ 自动播种的默认文档，保证测试集受控。"""
    from mcp.knowledge_base import KnowledgeBase
    # 指向一个必然无服务的端口，强制走本地嵌入式 Chroma（不碰桌面端 data/kb_chroma）
    kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=8971, chroma_path=str(chroma_dir))
    for col in (kb._collection, kb._parents):
        ids = col.get()["ids"]
        if ids:
            col.delete(ids=ids)
    return kb


def fresh_kb(seed, probe):
    """全新唯一目录的知识库 + 播种后存储自检（自检不过换全新目录重建一次）。

    本机 ChromaDB 持久库偶发 HNSW 段不可读（全新目录、单进程也可能出现，与被测
    代码无关）：不自检就会把"磁盘抖动"误记成产品缺陷。seed(kb) 播种并返回本次新增
    片段数；probe 为不带 scopes 的自检检索串（必须是某篇正文的字面子串）。
    """
    SCRATCH.mkdir(parents=True, exist_ok=True)
    kb, _used_dir = guard.build_kb_checked(SCRATCH, _mk_kb, seed, probe, name="kb_evidence_test")
    _OPEN_KBS.append(kb)
    return kb


# 一句"通用填充"句（不含关键串中的稀有 token，保证唯一命中落在尾部子块）
_FILL = ("各岗位从业人员应当落实岗位安全责任，遵守本单位安全生产规章制度和操作规程，"
         "服从管理并正确佩戴劳动防护用品。")


def _filler_paras(n_paras=4, n_sent=12):
    return "".join(_FILL * n_sent for _ in range(n_paras))


KEY_TAIL = "条款编号KX-8831"       # 只出现在长文档末尾
TAIL_SENT = ("应急处置应当严格遵守下列要求，" * 24
             + "本条款明确：" + KEY_TAIL + "规定的停产整顿期限不得超过七十二小时。")

KEY_HEAD = "条款编号HB-2205"       # 出现在文档开头（用于验证"后一个子块"上下文）
# ── A05：长制度尾部落款必须进入 snippet ──────────────────────────────────────
def test_a05_tail_clause_enters_snippet():
    content = _filler_paras() + TAIL_SENT
    kb = fresh_kb(lambda k: k.add_documents([{"title": "长制度尾条款",
                                              "content": content,
                                              "source": "企业制度库", "version": "2026-01",
                                              "scope": "全厂", "status": "现行"}]),
                  KEY_TAIL)

    item = None
    res = kb.search(KEY_TAIL, top_k=5)
    if res:
        item = res[0]

    # 人工计算：关键串应落在末尾子块，且在该子块 300 字之后（旧实现 snippet=doc[:300] 必然丢失）
    chunks = kb._chunk_text(content)
    expected_idx = next((i for i, c in enumerate(chunks) if KEY_TAIL in c), -1)
    tail_pos = chunks[expected_idx].index(KEY_TAIL) if expected_idx >= 0 else -1

    record("构造：关键串在文档 600 字之后", content.index(KEY_TAIL) > 600,
           f"pos={content.index(KEY_TAIL)}")
    record("构造：关键串在命中子块 300 字之后(证明旧 snippet 会漏)",
           tail_pos > 300, f"chunk_pos={tail_pos}")
    record("构造：关键串所在为末段子块", expected_idx >= 0 and expected_idx == len(chunks) - 1,
           f"idx={expected_idx} total={len(chunks)}")

    if not item:
        record("A05 检索命中长文档", False, "search 无结果")
        return

    record("A05 检索命中长文档", True)
    # 核心：真命中的完整子块必须进入 snippet
    record("A05 命中 snippet 含尾部关键串", KEY_TAIL in item.get("snippet", ""),
           f"snippet_len={len(item.get('snippet',''))} snippet={item.get('snippet','')[:80]!r}")
    # content 仍只覆盖父文档前部，关键串不在其中——证明"不能只靠 content 开头"
    record("A05 关键串不在 content 前 600 字内", KEY_TAIL not in item.get("content", "")[:600])
    # 证据合起来可定位到正确位置
    record("A05 snippet+context_blob 可定位关键串",
           KEY_TAIL in (item.get("snippet", "") + item.get("context_blob", "")))
    record("A05 chunk_index 落在人工计算的末段", item.get("chunk_index") == expected_idx,
           f"got={item.get('chunk_index')} want={expected_idx}")
    record("A05 section 与切片数一致",
           item.get("section") == f"第{expected_idx + 1}/{len(chunks)}段",
           f"section={item.get('section')!r} total={item.get('total_chunks')}")
    # 邻近上下文 = 前一个子块（末段只有前邻）
    prev_chunk = chunks[expected_idx - 1] if expected_idx > 0 else ""
    record("A05 context_blob 含前一个子块", bool(prev_chunk) and prev_chunk in item.get("context_blob", ""),
           f"blob_len={len(item.get('context_blob',''))}")
    record("A05 context_blob 不含本子块关键串(不串块)",
           KEY_TAIL not in item.get("context_blob", ""))

    need = {"title", "content", "snippet", "snippet_short", "context_blob", "score", "chunk",
            "chunk_id", "content_hash", "doc_id", "parent_id", "chunk_index", "total_chunks",
            "source", "version", "scope", "status", "section"}
    record("A05 返回结构含全部字段", need <= set(item.keys()),
           f"missing={need - set(item.keys())}")
    record("A05 旧字段语义保留(snippet_short=前300字)",
           item.get("snippet_short") == item.get("snippet", "")[:300]
           and len(item.get("snippet_short", "")) <= 300)
    record("A05 文档身份元数据随结果返回",
           item.get("source") == "企业制度库" and item.get("version") == "2026-01"
           and item.get("status") == "现行")


def test_context_blob_has_next_chunk_for_head_hit():
    """关键串在文档开头（chunk 0）时，context_blob 应带上后一个子块。"""
    head = "本条规定：" + KEY_HEAD + "应当在二十四小时内完成整改并复核。"
    content = head + _filler_paras(4, 12)
    kb = fresh_kb(lambda k: k.add_documents([{"title": "头部条款规程", "content": content}]),
                  KEY_HEAD)

    res = kb.search(KEY_HEAD, top_k=5)
    item = res[0] if res else {}
    chunks = kb._chunk_text(content)
    record("头部命中 chunk_index=0", item.get("chunk_index") == 0, f"got={item.get('chunk_index')}")
    record("头部命中 context_blob 含后一个子块",
           len(chunks) > 1 and chunks[1] in item.get("context_blob", ""),
           f"total={len(chunks)}")


# ── A06 的一部分：同主题两版本制度都能被检索且各自带 version ─────────────────
def test_two_versions_both_retrievable():
    shared = "条款编号DH-4417 应当办理动火作业票并设专人监护，作业票一式三联。"
    kb = fresh_kb(lambda k: k.add_documents([
        {"title": "动火作业管理制度A", "content": "动火作业分级审批与监护要求。" + shared + _filler_paras(2, 8),
         "version": "2023-01-01", "source": "企业制度库", "status": "已废止"},
        {"title": "动火作业管理制度B", "content": "动火作业分级审批与监护要求。" + shared + _filler_paras(2, 8),
         "version": "2025-06-30", "source": "企业制度库", "status": "现行"},
    ]), "条款编号DH-4417")
    res = kb.search(shared, top_k=5)
    versions = {it.get("version") for it in res}
    titles = {it.get("title") for it in res}
    doc_ids = {it.get("doc_id") for it in res}
    record("两版本制度都被检索到", {"2023-01-01", "2025-06-30"} <= versions, f"versions={versions}")
    record("两版本可区分标题", {"动火作业管理制度A", "动火作业管理制度B"} <= titles, f"titles={titles}")
    record("两版本 doc_id 各自稳定不互相覆盖", len(doc_ids) == len(res) and "" not in doc_ids,
           f"doc_ids={doc_ids}")
    record("版本携带失效状态", {it.get("status") for it in res} >= {"已废止", "现行"})


# ── 身份稳定性：重复导入不累积、身份稳定 ─────────────────────────────────────
def test_reimport_no_accumulation_and_stable_identity():
    key = "条款编号RP-6600"
    content = _filler_paras(2, 10) + "本条规定：" + key + "要求立即整改。"
    doc = {"title": "重复导入规程", "content": content, "version": "v1"}

    first = {}
    kb = fresh_kb(lambda k: first.setdefault("n1", k.add_documents([doc])), key)
    n1 = first["n1"]
    count1 = kb._collection.count()
    r1 = kb.search(key, top_k=3)
    i1 = r1[0] if r1 else {}

    n2 = kb.add_documents([doc])
    count2 = kb._collection.count()
    r2 = kb.search(key, top_k=3)
    i2 = r2[0] if r2 else {}

    record("重复导入子块数不累积", count1 == count2 == n1 == n2,
           f"n1={n1} n2={n2} count1={count1} count2={count2}")
    record("重复导入 doc_id 稳定", bool(i1.get("doc_id")) and i1.get("doc_id") == i2.get("doc_id"))
    record("重复导入 chunk_id/content_hash 稳定",
           i1.get("chunk_id") == i2.get("chunk_id")
           and i1.get("content_hash") == i2.get("content_hash")
           and bool(i1.get("chunk_id")))
    record("chunk_id 为 16 位 hex", len(i1.get("chunk_id", "")) == 16)


# ── 章节定位 ─────────────────────────────────────────────────────────────────
def test_section_indexing():
    content = _filler_paras(2, 12) + "条款编号SC-3300 应当在三日内完成治理。"
    kb = fresh_kb(lambda k: k.add_documents([{"title": "切片定位规程", "content": content}]),
                  "条款编号SC-3300")
    res = kb.search("条款编号SC-3300", top_k=3)
    item = res[0] if res else {}
    total = item.get("total_chunks", -1)
    idx = item.get("chunk_index", -1)
    expected_total = len(kb._chunk_text(content))
    record("total_chunks 与切片数一致", total == expected_total, f"got={total} want={expected_total}")
    record("chunk_index 落在 [0, total-1]", isinstance(idx, int) and 0 <= idx <= total - 1,
           f"idx={idx} total={total}")
    record("section 与 chunk_index/total 一致",
           item.get("section") == f"第{idx + 1}/{total}段", f"section={item.get('section')!r}")


# ── 旧数据兼容 ───────────────────────────────────────────────────────────────
def test_legacy_chunks_without_metadata():
    def _seed(k):
        # 手造一个没有 type/parent_id 元数据的子块，走旧数据兜底分支
        k._collection.add(ids=["legacy_1"],
                          documents=["旧版规程正文：动火作业前应当进行动火分析并设专人监护。"],
                          metadatas=[{"title": "旧版规程"}])
        return 1

    kb = fresh_kb(_seed, "动火分析")
    try:
        res = kb.search("动火分析 专人监护", top_k=3)
    except Exception as ex:
        record("旧数据检索不抛异常", False, f"异常: {ex}")
        return
    record("旧数据检索不抛异常", isinstance(res, list))
    record("旧数据兜底仍返回结果", bool(res) and bool(res[0].get("snippet")),
           f"res={res[:1]}")
    if res:
        it = res[0]
        record("旧数据缺失身份字段给默认值",
               it.get("doc_id") == "" and it.get("chunk_index") == -1
               and it.get("total_chunks") == -1 and it.get("section") == ""
               and it.get("source") == "" and it.get("context_blob") == "")


# ── list_documents：目录与 has_child ──────────────────────────────────────────
def test_list_documents_catalog():
    kb = fresh_kb(lambda k: k.add_documents([
        {"title": "目录正文A", "content": _filler_paras(2, 10) + "条款编号CT-9001 应当登记建档。",
         "source": "S-库", "version": "v9", "scope": "全厂", "status": "现行"}]), "条款编号CT-9001")
    # 只有父块、没有子块的文档
    kb._parents.upsert(ids=["cat_only_1"], documents=["只有目录记录，未收录可引用正文。"],
                       metadatas=[{"title": "仅有目录B", "type": "parent"}])

    cat = kb.list_documents()
    by_title = {c.get("title"): c for c in cat}
    a = by_title.get("目录正文A", {})
    b = by_title.get("仅有目录B", {})
    record("list_documents 返回目录项", bool(cat) and isinstance(cat, list))
    record("有正文文档 has_child=True 且 chunk_count>0",
           a.get("has_child") is True and a.get("chunk_count", 0) >= 1, f"A={a}")
    record("目录携带 source/version/scope/status",
           a.get("source") == "S-库" and a.get("version") == "v9"
           and a.get("scope") == "全厂" and a.get("status") == "现行", f"A={a}")
    record("只有父块文档 has_child=False", bool(b) and b.get("has_child") is False, f"B={b}")


# ── 目录/正文边界：只有目录时不得声称命中其正文 ──────────────────────────────
def test_catalog_only_not_searchable_as_body():
    kb = fresh_kb(lambda k: k.add_documents([{"title": "正常规程", "content": _filler_paras(2, 10)}]),
                  _FILL[:40])
    kb._parents.upsert(ids=["cat_only_2"],
                       documents=["ONLY-CATALOG-7777 只有目录记录，未收录可引用正文。"],
                       metadatas=[{"title": "目录专用", "type": "parent"}])
    res = kb.search("ONLY-CATALOG-7777", top_k=3)
    blob = " ".join((it.get("snippet", "") + it.get("content", "") + it.get("context_blob", ""))
                    for it in res)
    record("只有目录无正文的文档不被检索命中正文", "ONLY-CATALOG-7777" not in blob,
           f"res_titles={[it.get('title') for it in res]}")


def main():
    fns = [test_a05_tail_clause_enters_snippet,
           test_context_blob_has_next_chunk_for_head_hit,
           test_two_versions_both_retrievable,
           test_reimport_no_accumulation_and_stable_identity,
           test_section_indexing,
           test_legacy_chunks_without_metadata,
           test_list_documents_catalog,
           test_catalog_only_not_searchable_as_body]
    for fn in fns:
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {ex}")

    passed, total = sum(1 for r in RESULTS if r), len(RESULTS)
    print(f"\n{passed}/{total} passed")
    # 清理临时库目录：显式释放每个知识库句柄（删集合/关客户端/清共享系统缓存）再删目录，
    # Windows 句柄未释放时 rmtree 会被拒（已复现 WinError 32/5）
    for kb in _OPEN_KBS:
        guard.release_kb(kb)
    _OPEN_KBS.clear()
    gc.collect()
    for d in SCRATCH.glob("kb_evidence_test_*"):
        if not guard.rmtree_retry(d):
            print(f"[WARN] 临时目录残留未清干净（句柄未释放？）: {d}", flush=True)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
