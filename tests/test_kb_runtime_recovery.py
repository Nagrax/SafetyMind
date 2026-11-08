# -*- coding: utf-8 -*-
# 知识库运行态自愈测试（2026-10-26 目标②："能否在不重启的情况下恢复"）。
#
# 背景（实测）：ChromaDB 1.5.9 Windows 上约 1/70 概率 HNSW 段损坏（环境级抖动，
# 见 tests/_storage_guard.py），运行中表现为 `Internal error: Error finding id`。
# 原有自愈只在启动时（core/chroma_utils.open_local_chroma）；运行中一路抛 500。
#
# 本轮修复：KnowledgeBase.search 捕获 corrupt → recover_runtime（归档+重开+重导默认知识）
# → 重试一次。SAFETYMIND_KB_RUNTIME_RECOVERY=0 可关闭。
#
# 本测试用**临时目录里的真库**（不是纯 mock），验证：
#   1. 第一次 search 抛 corrupt（注入坏 query 路径）→ 自愈触发 → 返回默认知识
#   2. 自愈后 _recovered_at 落盘、_last_degraded 清回 None
#   3. 冷却期内第二次 corrupt 不再触发自愈（防循环）
#   4. 开关关闭时走纯降级（返回空）
#   5. 服务器模式不支持自愈
#
# 用法: python tests/test_kb_runtime_recovery.py
import os
import shutil
import sys
import threading
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def make_real_kb(tmp: Path):
    """真库（本地嵌入式 + ngram 嵌入，快）+ 默认知识。"""
    from mcp.knowledge_base import KnowledgeBase
    d = tmp / "kb"
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)
    kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=9, chroma_path=str(d))
    return kb, d


def inject_corrupt_then_ok(kb):
    """让第一次 query 抛 InternalError，之后恢复正常（模拟 1/70 抖动后自愈）。"""
    real_query = kb._collection.query
    state = {"failed": False}

    def flaky(*a, **kw):
        if not state["failed"]:
            state["failed"] = True
            import chromadb
            raise chromadb.errors.InternalError(
                "Error executing plan: Internal error: Error finding id")
        return real_query(*a, **kw)

    kb._collection = SimpleNamespace(query=flaky)
    return state


def main():
    import chromadb
    tmp = Path(tempfile.mkdtemp(prefix="kb_recover_", dir="D:/claudecode/scratch"))
    try:
        # ── 1. 正常路径先证明库是好的 ────────────────────────────────────
        kb, kbd = make_real_kb(tmp)
        n0 = kb.search("动火作业", top_k=3)
        record("初始库正常（能查到默认知识）", len(n0) > 0, f"hits={len(n0)}")

        # ── 2. 注入 corrupt → 自愈 → 重试成功 ───────────────────────────
        state = inject_corrupt_then_ok(kb)
        out = kb.search("动火作业", top_k=3)
        record("corrupt 触发自愈后 search 返回结果（不抛异常）", isinstance(out, list),
               f"out_type={type(out).__name__}")
        record("自愈后查到重导的默认知识", len(out) > 0, f"hits={len(out)}")
        record("自愈后 _last_degraded 清回 None", kb._last_degraded is None,
               f"flag={kb._last_degraded}")
        record("自愈时间戳已记录", kb._recovered_at > 0, f"at={kb._recovered_at}")
        record("flaky 确实被调用过一次（注入生效）", state["failed"] is True)

        # ── 3. 冷却期内不再自愈（防循环重建）──────────────────────────────
        calls_before = state["failed"]
        # 再注入一次 corrupt：自愈应因冷却期被跳过，返回空列表
        def always_fail(*a, **kw):
            import chromadb
            raise chromadb.errors.InternalError("Internal error: Error finding id")
        kb._collection = SimpleNamespace(query=always_fail)
        out2 = kb.search("受限空间", top_k=3)
        record("冷却期内第二次 corrupt 走纯降级（返回空，不自愈）",
               out2 == [] and kb._last_degraded == "corrupt",
               f"out={out2} flag={kb._last_degraded}")

        # ── 4. 开关关闭 → 纯降级 ─────────────────────────────────────────
        old = os.environ.get("SAFETYMIND_KB_RUNTIME_RECOVERY")
        try:
            os.environ["SAFETYMIND_KB_RUNTIME_RECOVERY"] = "0"
            kb._recovered_at = 0.0    # 解除冷却，确保走的是开关分支而不是冷却分支
            kb3, _ = make_real_kb(tmp)
            kb3._collection = SimpleNamespace(query=always_fail)
            out3 = kb3.search("动火作业", top_k=3)
            record("开关=0 时 corrupt 纯降级返回空（不自愈）",
                   out3 == [] and kb3._recovered_at == 0.0,
                   f"out={out3} recovered_at={kb3._recovered_at}")
        finally:
            if old is None:
                os.environ.pop("SAFETYMIND_KB_RUNTIME_RECOVERY", None)
            else:
                os.environ["SAFETYMIND_KB_RUNTIME_RECOVERY"] = old

        # ── 5. 服务器模式不支持自愈 ──────────────────────────────────────
        from mcp.knowledge_base import KnowledgeBase as _KB
        kb4 = _KB.__new__(_KB)
        kb4._use_server = True
        kb4._recovery_lock = threading.Lock()
        kb4._recovered_at = 0.0
        record("服务器模式跳过本地自愈", _KB.recover_runtime(kb4, "test") is False)

        # ── 5b. 自愈重播本实例文档（2026-11-08 门禁修复）─────────────────
        # 门禁实录（runs/wf-sm-31-c2.json、runs/diag-sm31-1.json）：Windows HNSW
        # 偶发段不可读（core/chroma_utils 开篇实录的本机已知 flake）触发运行态自愈，
        # 原实现归档重建只重导默认知识——本进程刚播种的证据被静默丢弃，默认文档
        # 顶替命中（"检索成功"但内容换了，证据保真被破坏）。契约：自愈后本实例
        # 添加过的文档仍可检索（注册表按 title 重播，add_documents 幂等）。
        kb5, _ = make_real_kb(tmp)
        seeded = kb5.add_documents([{
            "title": "动火作业管理规定（自愈重播回归种子）",
            "content": "二级动火票有效期不超过72小时SEEDREGEN99，"
                       "作业结束后作业人须立即关闭动火票并复位现场警戒。",
            "source": "内部制度", "version": "2026-11",
        }])
        _probe = kb5.search("SEEDREGEN99 动火票有效期", top_k=3)

        def _has_seed(hits):
            return any("SEEDREGEN99" in str(h.get("snippet", ""))
                       or "SEEDREGEN99" in str(h.get("content", "")) for h in hits)

        record("5b 前置：种子文档已入库且可检索",
               seeded > 0 and _has_seed(_probe),
               f"seeded={seeded} hits={[(h.get('title'), str(h.get('snippet', ''))[:40]) for h in _probe]}")
        _registry = getattr(kb5, "_instance_docs", None)
        record("产品具备自愈重播注册表（_instance_docs）", _registry is not None,
               "缺 _instance_docs——自愈重播修复缺失")
        reg_before = len(_registry) if _registry is not None else 0
        inject_corrupt_then_ok(kb5)
        out5 = kb5.search("SEEDREGEN99 动火票有效期", top_k=3)
        record("自愈重播：corrupt 重建后本实例文档仍可检索（不再静默丢证据）",
               _has_seed(out5),
               f"hits={[(h.get('title'), str(h.get('snippet', ''))[:40]) for h in out5]}")
        _registry2 = getattr(kb5, "_instance_docs", None)
        record("重播后种子文档仍在注册表（按 title 保留最新版）",
               _registry2 is not None
               and "动火作业管理规定（自愈重播回归种子）" in _registry2
               and len(_registry2) >= reg_before,
               f"before={reg_before} after={(len(_registry2) if _registry2 is not None else 'N/A')}")
        if _registry2 is not None:
            _n = len(_registry2)
            kb5.add_documents([{
                "title": "动火作业管理规定（自愈重播回归种子）",
                "content": "二级动火票有效期不超过72小时SEEDREGEN99，"
                           "作业结束后作业人须立即关闭动火票并复位现场警戒。",
                "source": "内部制度", "version": "2026-11",
            }])
            record("注册表按 title 幂等（同 title 重导不增长）",
                   len(_registry2) == _n, f"before={_n} after={len(_registry2)}")

        # ── 6. 源码守护：开关名与冷却期常量存在 ──────────────────────────
        src = (Path(__file__).resolve().parents[1] / "mcp" / "knowledge_base.py").read_text(encoding="utf-8")
        record("自愈开关 SAFETYMIND_KB_RUNTIME_RECOVERY 存在",
               "SAFETYMIND_KB_RUNTIME_RECOVERY" in src)
        record("冷却期常量 _RECOVERY_COOLDOWN_S 存在", "_RECOVERY_COOLDOWN_S" in src)
        record("自愈只重试一次（不循环）", src.count("self._scoped_child_query(query, top_k * 4, scopes)") >= 2
               and "自愈后重查仍失败" in src)
    finally:
        try:
            shutil.rmtree(str(tmp), ignore_errors=True)
        except Exception:
            pass

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
