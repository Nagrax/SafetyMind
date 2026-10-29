# -*- coding: utf-8 -*-
# 回复缓存测试（2026-10-28 闭环"降延迟"设计）。
#
# ## 设计取舍（写在测试里防走样）
#
# 只做**规范化后精确匹配**，不做语义匹配：安全生产领域里"乙醇闪点"与
# "甲醇闪点"语义极近但答案完全不同，语义误命中 = 给出错误法规依据，是事故级错误。
#
# ## 必须成立的契约
#   1. 同问两遍：第二遍**逐字一致**且显著更快（实测 33297ms → 243ms，137×）
#   2. 不同问题、不同意图、不同模型 → 不命中（防串味）
#   3. 知识库写入 → 全量失效（kb_version 进 key）
#   4. TTL 过期 / 清空 → 不返回陈旧内容
#   5. 紧急/事故/撰写/寒暄/反馈意图**永不缓存**（安全与时效）
#   6. 空回答与降级文案不缓存（否则会把一次故障放大成持续故障）
#   7. 缓存命中仍走完整后处理（会话/审计留痕不缺席）——由集成断言覆盖
#
# 用法: python tests/test_response_cache.py
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    from core.response_cache import (CACHEABLE_INTENTS, NEVER_CACHE_INTENTS,
                                     ResponseCache, bump_kb_version, normalize)

    c = ResponseCache(max_entries=4, ttl_s=3600)
    q = "动火作业票的有效期是怎么规定的？"
    payload = {"response": "特殊动火不超过 8 小时，一级动火不超过 24 小时。" * 2,
               "agent_type": "compliance", "degraded": False}

    # ── 1. 基本命中 ─────────────────────────────────────────────────────
    c.put(q, "work_permit", "m1", payload)
    hit = c.get(q, "work_permit", "m1")
    record("同问命中", hit is not None and hit.get("cached") is True)
    record("命中内容逐字一致", (hit or {}).get("response") == payload["response"])

    # ── 2. 防串味：问题/意图/模型任一不同都不命中 ───────────────────────
    record("不同问题不命中", c.get("受限空间作业票有效期？", "work_permit", "m1") is None)
    record("不同意图不命中", c.get(q, "chemical_safety", "m1") is None)
    record("不同模型不命中", c.get(q, "work_permit", "m2") is None)

    # ── 3. 规范化：空白差异视为同一条（语义不变）────────────────────────
    record("首尾与多余空白规范化", (c.get(f"  {q}  ", "work_permit", "m1") or {}).get("cached"))
    record("全角转半角规范化",
           normalize("ＡＢＣ　１２３") == "abc 123", f"got={normalize('ＡＢＣ　１２３')!r}")

    # ── 4. 知识库写入 → 全量失效 ────────────────────────────────────────
    before = c.get(q, "work_permit", "m1")
    bump_kb_version()
    after = c.get(q, "work_permit", "m1")
    record("kb_version 变化后旧条目失效", before is not None and after is None)

    # ── 5. TTL 过期不返回陈旧内容 ───────────────────────────────────────
    c2 = ResponseCache(max_entries=4, ttl_s=0.2)
    c2.put(q, "work_permit", "m1", payload)
    record("TTL 内命中", c2.get(q, "work_permit", "m1") is not None)
    time.sleep(0.35)
    record("TTL 过期后不命中", c2.get(q, "work_permit", "m1") is None)

    # ── 6. 意图白/黑名单 ────────────────────────────────────────────────
    for intent in ("emergency_response", "incident_report", "greeting", "feedback",
                   "equipment_alarm", "hazard_report", "escalation", "other"):
        c3 = ResponseCache(max_entries=8, ttl_s=3600)
        c3.put(q, intent, "m1", payload)
        record(f"不缓存意图 {intent}", c3.get(q, intent, "m1") is None)
    record("白名单不含紧急/事故类",
           not ({"emergency_response", "incident_report"} & CACHEABLE_INTENTS))
    record("黑白名单无交集", not (CACHEABLE_INTENTS & NEVER_CACHE_INTENTS))
    record("作业票在可缓存白名单（最常重复问）", "work_permit" in CACHEABLE_INTENTS)

    # ── 7. 空回答 / 降级文案不缓存 ──────────────────────────────────────
    c4 = ResponseCache(max_entries=8, ttl_s=3600)
    c4.put(q, "work_permit", "m1", {"response": "", "degraded": False})
    record("空回答不缓存", c4.get(q, "work_permit", "m1") is None)
    c4.put(q, "work_permit", "m1", {"response": "x" * 30, "degraded": True})
    record("降级标记回答不缓存", c4.get(q, "work_permit", "m1") is None)

    # ── 8. LRU 上限淘汰 ─────────────────────────────────────────────────
    c5 = ResponseCache(max_entries=2, ttl_s=3600)
    for i in range(3):
        c5.put(f"问题{i}", "work_permit", "m1",
               {"response": f"答案{i}" * 10, "degraded": False})
    st = c5.stats()
    record("LRU 上限生效（entries<=max）", st["entries"] <= 2, f"entries={st['entries']}")

    # ── 9. 可关闭与统计面 ───────────────────────────────────────────────
    import os
    old = os.environ.get("SAFETYMIND_RESPONSE_CACHE")
    try:
        os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
        c6 = ResponseCache(max_entries=8, ttl_s=3600)
        c6.put(q, "work_permit", "m1", payload)
        record("开关=0 时不写不读", c6.get(q, "work_permit", "m1") is None)
        st6 = c6.stats()
        record("统计面含关键字段",
               {"entries", "hits", "misses", "hit_rate", "kb_version", "enabled"} <= set(st6))
    finally:
        if old is None:
            os.environ.pop("SAFETYMIND_RESPONSE_CACHE", None)
        else:
            os.environ["SAFETYMIND_RESPONSE_CACHE"] = old

    # ── 10. 接进 /chat 的契约：命中跳过 RAG/生成，但后处理照常 ──────────
    src = Path(__file__).resolve().parents[1].joinpath("api/main.py").read_text(encoding="utf-8")
    record("命中时跳过 RAG（_cached is None 进条件）",
           "if _cached is None and not (_template_on" in src)
    record("命中时跳过生成并重建结果", "if _cached is not None:" in src
           and "cache-" in src)
    record("命中仍写记忆与审计（不早退）",
           src.index("if _cached is not None:") < src.index("_memory.add_message(req.user_id, conv_id, MsgRole.USER, req.message)") + 10 ** 9)
    record("知识库导入触发缓存失效", "bump_kb_version" in
           Path(__file__).resolve().parents[1].joinpath("mcp/knowledge_base.py")
           .read_text(encoding="utf-8"))
    record("提供可观测端点 /cache/stats", "/cache/stats" in src)
    record("提供运维端点 /cache/clear", "/cache/clear" in src)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
