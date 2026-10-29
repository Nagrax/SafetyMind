# -*- coding: utf-8 -*-
# 知识库运行态降级测试（2026-10-26 闭环第 8 轮，端到端预演发现的生产缺陷）。
#
# ## 缺陷现象（实测）
#   - 跑过多次压测的旧服务实例：/search/direct 稳定 500（3/3），/chat 降级为
#     "抱歉，处理您的请求时出现问题"。
#   - 同一份 .env、同一配置的全新实例：/search/direct 200 正常。
#   → 结论是**运行态劣化**，不是配置错误。
#
# ## 根因（从服务日志提取的异常栈）
#   chromadb.errors.InternalError: Error executing plan: Internal error: Error finding id
#   发生在 mcp/knowledge_base._scoped_child_query → collection.query。
#   仓库原有自愈只在**启动时**（core/chroma_utils.open_local_chroma）生效，
#   运行中遇到该错误会一路抛到 HTTP 500。
#
# ## 修复
#   在 KnowledgeBase.search 里捕获检索异常 → 标记 _last_degraded → 返回空结果。
#   原则：**检索不可用不该让整个接口 500**，但**绝不静默伪造检索结果**，
#   必须让上层能区分"没查到"与"库坏了"。
#
# 本测试锁定：
#   1. Chroma 抛 InternalError 时 search 返回空列表而不是抛异常
#   2. 降级标记区分 corrupt / transient（决定运维该不该重启）
#   3. /search/direct 响应体带 degraded 字段与说明
#   4. 正常路径不受影响（无降级标记）
#
# 用法: python tests/test_kb_runtime_degrade.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
# 本文件测的是"纯降级语义"（corrupt → 空结果 + 标记），运行态自愈另见
# tests/test_kb_runtime_recovery.py——关闭开关避免自愈分支干扰断言。
os.environ["SAFETYMIND_KB_RUNTIME_RECOVERY"] = "0"
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def make_kb_with_failing_query(exc_type, exc_msg):
    """构造一个最小 KnowledgeBase，其 collection.query 抛指定异常。"""
    from types import SimpleNamespace
    from mcp.knowledge_base import KnowledgeBase

    kb = KnowledgeBase.__new__(KnowledgeBase)   # 跳过 __init__，不连真库
    kb._last_degraded = None

    def boom(*a, **kw):
        raise exc_type(exc_msg)

    kb._collection = SimpleNamespace(query=boom)
    kb._parents = SimpleNamespace(get=lambda *a, **kw: {"documents": [], "metadatas": []})
    kb.RETURN_PARENT_MAX_CHARS = 600
    # _identity_fields 依赖实例方法，直接给一个原样返回
    kb._identity_fields = lambda doc, meta: {}
    kb._client_scope_filter = lambda res, scopes: res
    return kb


def main():
    import chromadb

    # ── 1. Chroma InternalError：不抛异常，返回空列表 ─────────────────────
    kb = make_kb_with_failing_query(
        chromadb.errors.InternalError, "Error executing plan: Internal error: Error finding id")
    try:
        out = kb.search("受限空间作业注意事项", top_k=3)
        raised = None
    except Exception as e:
        out, raised = None, e
    record("Chroma InternalError 时 search 不抛异常", raised is None, f"raised={raised}")
    record("Chroma InternalError 时返回空列表（不伪造结果）", out == [], f"out={out}")

    # ── 2. 降级标记区分 corrupt / transient ──────────────────────────────
    record("检出 Error finding id 判为 corrupt（需重启）",
           kb._last_degraded == "corrupt", f"flag={kb._last_degraded}")

    kb2 = make_kb_with_failing_query(ConnectionError, "connection reset by peer")
    kb2.search("动火作业", top_k=3)
    record("普通连接错误判为 transient（无需重启）",
           kb2._last_degraded == "transient", f"flag={kb2._last_degraded}")

    kb3 = make_kb_with_failing_query(chromadb.errors.InternalError,
                                     "Error creating hnsw segment reader: Nothing found on disk")
    kb3.search("动火作业", top_k=3)
    record("HNSW 段损坏判为 corrupt", kb3._last_degraded == "corrupt", f"flag={kb3._last_degraded}")

    # ── 3. 正常路径不产生降级标记 ────────────────────────────────────────
    from types import SimpleNamespace
    from mcp.knowledge_base import KnowledgeBase
    kb4 = KnowledgeBase.__new__(KnowledgeBase)
    kb4._last_degraded = None
    kb4.RETURN_PARENT_MAX_CHARS = 600
    kb4._identity_fields = lambda doc, meta: {}
    kb4._client_scope_filter = lambda res, scopes: res
    # 空结果：走"兼容旧数据"分支后再 _collection.query，两次都返回空
    kb4._collection = SimpleNamespace(query=lambda *a, **kw: {
        "documents": [[]], "metadatas": [[]], "distances": [[]]})
    kb4._parents = SimpleNamespace(get=lambda *a, **kw: {"documents": [], "metadatas": []})
    out4 = kb4.search("动火作业", top_k=3)
    record("正常空结果不标记降级", out4 == [] and kb4._last_degraded is None,
           f"out={out4} flag={kb4._last_degraded}")

    # ── 4. 源码层面：catch 位置在 search 内（不是只在启动时）──────────────
    src = (Path(__file__).resolve().parents[1] / "mcp" / "knowledge_base.py").read_text(encoding="utf-8")
    record("search 内含运行态降级捕获", "_runtime_degrade" in src)
    record("降级方法区分 corrupt 标记", "corrupt_markers" in src and "error finding id" in src)
    record("降级时如实记 ERROR 日志（提示重启）",
           "重启才能恢复" in src or "重启服务" in src)

    # ── 5. API 层：/search/direct 会读取降级标记 ─────────────────────────
    api_src = (Path(__file__).resolve().parents[1] / "api" / "main.py").read_text(encoding="utf-8")
    record("search_direct 读取 _last_degraded 并写 degraded 字段",
           "_last_degraded" in api_src and '"degraded"' in api_src)
    record("降级说明明确区分'库坏了'与'没检索到'",
           "不代表库中没有相关资料" in api_src)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
