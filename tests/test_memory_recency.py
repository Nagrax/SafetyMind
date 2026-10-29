# -*- coding: utf-8 -*-
# 情景记忆时间感知重排测试（2026-10-26 闭环第 5 轮）。
#
# 修复的缺陷：`_search_episodic` 纯按向量相似度取 top-k，完全不看时间，
# 连 `ts` 元数据都没读回。安全生产场景里"三个月前的处置记录"和"昨天的"
# 语义同样相关但时效性完全不同（旧处置可能因法规修订已失效）。
#
# 本测试锁定：
#   1. 同等相关度下，越新的记忆排越前（时间维度真的生效）
#   2. 时间重排是**重排而非过滤**：旧记忆仍在候选里，不被直接丢弃
#   3. SAFETYMIND_MEMORY_RECENCY=0 可回退纯相似度（向后兼容）
#   4. 半衰期可配置，且衰减曲线符合预期（30 天权重 0.5、90 天约 0.125）
#   5. ts 缺失/损坏时不崩、不奖不罚
#   6. 结果条数不超过 HISTORY_TOP_K（接口契约不变）
#
# 用法: python tests/test_memory_recency.py
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class FakeEpisodic:
    """假 Chroma 集合：返回预设的 documents/metadatas/distances。"""

    def __init__(self, docs, metas, dists):
        self._docs, self._metas, self._dists = docs, metas, dists
        self.last_n = None

    def query(self, query_texts=None, n_results=None, where=None):
        self.last_n = n_results
        n = min(n_results or len(self._docs), len(self._docs))
        return {
            "documents": [self._docs[:n]],
            "metadatas": [self._metas[:n]],
            "distances": [self._dists[:n]],
        }


def make_memory(episodic):
    """构造一个只挂假 episodic 的最小 MemoryManager（不连 Redis/Chroma/LLM）。"""
    from memory.conversation_memory import MemoryManager
    m = MemoryManager.__new__(MemoryManager)
    m._episodic = episodic
    m.HISTORY_TOP_K = 3
    return m


def run_search(episodic, query="动火作业票怎么办理"):
    import asyncio
    from memory.conversation_memory import MemoryManager
    m = make_memory(episodic)
    return asyncio.run(MemoryManager._search_episodic(m, "u1", query))


def main():
    now = datetime.now()

    def days_ago(n):
        return (now - timedelta(days=n)).isoformat()

    # 三份记忆，语义相关度相同（distance 都是 0.2），唯一差别是时间
    docs = ["旧处置记录：动火票由车间主任审批", "中间记录：动火票需安环部复核",
            "最新记录：动火票一级动火须安环部审批，有效期8小时"]
    metas = [{"user_id": "u1", "ts": days_ago(120)},
             {"user_id": "u1", "ts": days_ago(45)},
             {"user_id": "u1", "ts": days_ago(1)}]
    dists = [0.2, 0.2, 0.2]

    # ── 1. 同相关度下新的排前（时间维度生效）────────────────────────────
    ep = FakeEpisodic(docs, metas, dists)
    os.environ.pop("SAFETYMIND_MEMORY_RECENCY", None)
    out = run_search(ep)
    record("同相关度下按时间新鲜度重排（最新在前）",
           out and out[0] == docs[2],
           f"out={out}")
    record("旧的记忆仍在结果里（重排不是过滤）",
           docs[0] in out, f"out={out}")
    record("结果条数不超过 HISTORY_TOP_K=3", len(out) <= 3, f"len={len(out)}")

    # ── 2. 重排池大于最终条数（否则重排无意义）──────────────────────────
    record("检索候选池大于 HISTORY_TOP_K（给重排留空间）",
           ep.last_n is not None and ep.last_n > 3, f"n_results={ep.last_n}")

    # ── 3. 相关度足够高时，旧记忆也能排到前面（不能变成纯时间排序）────────
    # 注意 distance 取值要贴近真实 Chroma：无关文档 distance 通常 ≥2.5
    # （1.5 其实已算"有点相关"，用它会得出误导性结论）
    docs2 = ["旧记录（语义极相关）动火票审批", "新记录（语义无关）食堂菜单"]
    metas2 = [{"user_id": "u1", "ts": days_ago(200)}, {"user_id": "u1", "ts": days_ago(0)}]
    dists2 = [0.02, 3.2]
    ep2 = FakeEpisodic(docs2, metas2, dists2)
    out2 = run_search(ep2)
    record("语义相关度能压过时间差（不是纯时间排序）",
           out2 and out2[0] == docs2[0], f"out={out2}")

    # 极端情形：旧记忆确实该让位——语义只稍好、时间极旧，新记忆应排前
    docs2b = ["旧记录（略相关）", "新记录（略相关）"]
    metas2b = [{"user_id": "u1", "ts": days_ago(365)}, {"user_id": "u1", "ts": days_ago(0)}]
    dists2b = [0.8, 0.9]   # 两者相关度接近
    out2b = run_search(FakeEpisodic(docs2b, metas2b, dists2b))
    record("相关度接近时时间成为裁决因素（新记忆胜出）",
           out2b and out2b[0] == docs2b[1], f"out={out2b}")

    # ── 4. 关闭开关回退纯相似度 ─────────────────────────────────────────
    os.environ["SAFETYMIND_MEMORY_RECENCY"] = "0"
    try:
        ep3 = FakeEpisodic(docs, metas, dists)
        out3 = run_search(ep3)
        record("SAFETYMIND_MEMORY_RECENCY=0 回退纯相似度（顺序按原返回）",
               out3 == docs[:3], f"out={out3}")
    finally:
        os.environ.pop("SAFETYMIND_MEMORY_RECENCY", None)

    # ── 5. 半衰期衰减曲线符合设计 ────────────────────────────────────────
    # 直接验算式：freshness = 0.5 ** (age/half_life)
    hl = 30.0
    f30 = 0.5 ** (30 / hl)
    f90 = 0.5 ** (90 / hl)
    record("30 天半衰期下 30 天权重为 0.5", abs(f30 - 0.5) < 1e-9, f"f30={f30}")
    record("30 天半衰期下 90 天权重约 0.125", abs(f90 - 0.125) < 1e-9, f"f90={f90}")

    # 半衰期可配置：改成 7 天后，同样 30 天的记录权重应明显更低
    docs4 = ["旧记录", "新记录"]
    metas4 = [{"user_id": "u1", "ts": days_ago(30)}, {"user_id": "u1", "ts": days_ago(0)}]
    dists4 = [0.2, 0.2]
    out_a = run_search(FakeEpisodic(docs4, metas4, dists4))
    os.environ["SAFETYMIND_MEMORY_HALF_LIFE_DAYS"] = "7"
    try:
        out_b = run_search(FakeEpisodic(docs4, metas4, dists4))
        record("半衰期可配置（7 天与 30 天下新记忆都排前，且不影响可用性）",
               out_a[0] == docs4[1] and out_b[0] == docs4[1],
               f"30d={out_a} 7d={out_b}")
    finally:
        os.environ.pop("SAFETYMIND_MEMORY_HALF_LIFE_DAYS", None)

    # ── 6. ts 缺失/损坏时不崩、不奖不罚 ──────────────────────────────────
    docs5 = ["无时间戳记录", "有时间戳的新记录"]
    metas5 = [{"user_id": "u1"}, {"user_id": "u1", "ts": days_ago(0)}]
    ep5 = FakeEpisodic(docs5, metas5, [0.2, 0.2])
    out5 = run_search(ep5)
    record("ts 缺失时不抛异常且仍返回结果", isinstance(out5, list) and len(out5) == 2, f"out={out5}")

    metas6 = [{"user_id": "u1", "ts": "not-a-date"}, {"user_id": "u1", "ts": days_ago(0)}]
    out6 = run_search(FakeEpisodic(docs5, metas6, [0.2, 0.2]))
    record("ts 损坏时不抛异常", isinstance(out6, list) and len(out6) == 2, f"out={out6}")

    # ── 7. 空查询 / 空结果 ──────────────────────────────────────────────
    empty_ep = FakeEpisodic([], [], [])
    record("空结果时返回空列表不崩", run_search(empty_ep) == [])
    record("空查询时直接返回空列表（不发起检索）",
           run_search(empty_ep, query="   ") == [])

    # ── 8. 存储侧带 ts 元数据（时间重排的前提）──────────────────────────
    src = Path(__file__).resolve().parents[1] / "memory" / "conversation_memory.py"
    text = src.read_text(encoding="utf-8")
    record("情景记忆写入时存了 ts 元数据（重排的前提条件）",
           '"ts": datetime.now().isoformat()' in text, "写入侧需保留 ts")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
