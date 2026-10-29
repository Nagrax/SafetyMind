# -*- coding: utf-8 -*-
# 批量灌库回归测试（2026-10-29 闭环第 45 轮）。
#
# ## 缺陷（实测发现）
#
# 灌 1698 条真实隐患数据时跑了 20 分钟还没完（CPU 1208s、内存 1GB）。根因不是嵌入模型慢，
# 而是 `add_documents` 对**每篇文档**各做一次父块 upsert（= 一次嵌入）和一次按标题删除：
# 1698 篇 → 1698 次嵌入 + 1698 次删除查询。
#
# ## 修法
#
# 父块改为**一次批量 upsert**；按标题删除改为 `{"$in": [...]}` 分片（每片 200 个标题）。
#
# ## 本测试锁定
#
# 1. N 篇文档只调一次父块 upsert（批处理生效）
# 2. 按标题删除按 200 一片分片（250 篇 → 2 次）
# 3. upsert 语义不变：同标题重复导入不产生重复子块
# 4. 身份元数据（source/version/scope）仍写入子块
#
# 用法: python tests/test_kb_batch_import.py
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SAFETYMIND_EMBEDDING", "ngram")   # 测试不加载 bge，数调用次数即可
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class _Counting:
    """代理 Chroma 集合：只数调用次数，其余透传。"""

    def __init__(self, obj):
        self._o = obj
        self.upserts = 0
        self.deletes = 0
        self.adds = 0
        self.delete_where = []

    def upsert(self, **kw):
        self.upserts += 1
        return self._o.upsert(**kw)

    def delete(self, **kw):
        self.deletes += 1
        self.delete_where.append(kw.get("where"))
        return self._o.delete(**kw)

    def add(self, **kw):
        self.adds += 1
        return self._o.add(**kw)

    def __getattr__(self, item):
        return getattr(self._o, item)


def make_kb(tmp: Path):
    from mcp.knowledge_base import KnowledgeBase
    return KnowledgeBase(chroma_host="127.0.0.1", chroma_port=9,
                         chroma_path=str(tmp / "kb_chroma"))


def docs(n, prefix="条目"):
    return [{"title": f"{prefix}{i}", "content": f"检查内容：第 {i} 条检查项的要求说明。",
                 "source": "test", "version": "v1", "scope": "public"} for i in range(n)]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="kb_batch_"))
    try:
        kb = make_kb(tmp)
        kb._parents = _Counting(kb._parents)
        kb._collection = _Counting(kb._collection)

        base_children = kb._collection.count()
        added = kb.add_documents(docs(5))
        record("导入 5 篇只调一次父块 upsert（批处理生效）",
               kb._parents.upserts == 1, f"upserts={kb._parents.upserts}")
        record("导入 5 篇只做一次按标题删除（<200 片）",
               kb._collection.deletes == 1, f"deletes={kb._collection.deletes}")
        record("子块数量符合预期", added == 5, f"added={added}")
        record("子块确实写入了库", kb._collection.count() == base_children + 5,
               f"count={kb._collection.count()}")

        # upsert 语义：同标题再导一次不应重复
        kb.add_documents(docs(5))
        record("同标题重复导入不产生重复子块",
               kb._collection.count() == base_children + 5,
               f"count={kb._collection.count()}")

        # 元数据保留
        got = kb._collection.get(where={"title": "条目3"}, include=["metadatas"])
        meta = (got.get("metadatas") or [{}])[0] or {}
        record("身份元数据写入子块", meta.get("source") == "test"
               and meta.get("version") == "v1" and meta.get("scope") == "public",
               f"meta={meta}")

        # 分片：250 篇 → 2 次删除
        kb._collection.deletes = 0
        kb._collection.delete_where.clear()
        kb.add_documents(docs(250, prefix="批量"))
        record("250 篇按 200 一片分片删除（2 次）",
               kb._collection.deletes == 2, f"deletes={kb._collection.deletes}")
        in_clauses = [w.get("title", {}).get("$in") if isinstance(w, dict) else None
                      for w in kb._collection.delete_where]
        record("删除条件用的是标题 $in 分片",
               bool(in_clauses) and all(isinstance(c, list) and len(c) <= 200
                                        for c in in_clauses if c is not None),
               f"where={kb._collection.delete_where[:2]}")
        record("250 篇的子块全部写入", kb._collection.count() == base_children + 255,
               f"count={kb._collection.count()}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
