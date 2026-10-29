# -*- coding: utf-8 -*-
# 紧急消息快速分流（本地 precheck + 最小响应）测试：python tests/test_emergency_fastpath.py
# 覆盖：precheck 命中四类紧急表述与 kind 合法性、误升级抑制（演练/培训/否定/讨论语境）、
#       强现场词可突破演练语境、既有紧急规则未被弱化（复用同一词表对象；"泄漏"单出现仍 HIGH）、
#       precheck 性能（1000 次亚秒级，证明无 LLM/IO）、
#       _emergency_fast_path 直接调用（不启服务、无 LLM/无记忆/无 RAG）的最小响应与四态处置状态区、
#       未配置渠道绝不出现"已通知/已收到"、已配置渠道状态为"待发送"不声称送达、
#       同一 rid 重复调用幂等不重复建单、拿不到应急闸门容量时降级为"待补"且无副作用。
# 本测试不调用真实模型（死端点仅作环境占位，快速分流不依赖 LLM）。退出码 0 = 全过。
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 需在导入 api.main 前设定：应急独立闸门容量与排队上限（消费端不阻塞主对话闸门）
os.environ["EMERGENCY_MAX_CONCURRENCY"] = "1"
os.environ["EMERGENCY_QUEUE_TIMEOUT_S"] = "0.05"
os.environ["API_HOST"] = "127.0.0.1"

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


HITS = ["着火了快来人", "有人被困在反应釜里快救命", "液氨钢瓶泄漏正在扩散", "中毒了"]
SUPPRESSED = ["我们正在做火灾应急演练", "培训里讲的泄漏案例", "没有泄漏，只是例行巡检",
              "不是爆炸，是设备放空", "泄漏这个词怎么写", "换个话题，推荐几首歌",
              "爆炸是什么意思", "假如现在爆炸了怎么办"]


def test_precheck():
    from core.emergency import precheck
    import core.emergency as em
    from core.intent_recognizer import _URGENCY_KEYWORDS, _URGENCY_COMBOS, UrgencyLevel

    for msg in HITS:
        sig = precheck(msg)
        record(f"precheck 命中: {msg}",
               bool(sig) and sig.get("kind") in ("emergency", "incident")
               and bool(sig.get("matched")) and bool(sig.get("reason")),
               json.dumps(sig, ensure_ascii=False))

    sig_combo = precheck("液氨钢瓶泄漏正在扩散")
    record("组合规则（泄漏+扩散）命中且标记为 emergency",
           sig_combo["kind"] == "emergency" and any("组合" in m or "扩散" in m for m in sig_combo["matched"]),
           json.dumps(sig_combo, ensure_ascii=False))
    sig_inc = precheck("爆炸事故已造成人员伤亡，需要事故上报")
    record("事故上报语境 kind=incident", bool(sig_inc) and sig_inc["kind"] == "incident",
           json.dumps(sig_inc, ensure_ascii=False))

    for msg in SUPPRESSED:
        record(f"precheck 抑制（误升级防护）: {msg}", precheck(msg) is None,
               json.dumps(precheck(msg), ensure_ascii=False))

    sig_strong = precheck("培训现场真的着火了")
    record("强现场词突破演练语境（不误抑制真实紧急）",
           bool(sig_strong) and sig_strong["kind"] == "emergency", json.dumps(sig_strong, ensure_ascii=False))

    record("紧急规则未弱化：复用既有 CRITICAL 词表/组合对象（非自造副本）",
           em._URGENCY_KEYWORDS is _URGENCY_KEYWORDS and em._URGENCY_COMBOS is _URGENCY_COMBOS)
    record("既有规则未弱化：'泄漏' 单出现仍是 HIGH，不进 CRITICAL 快速分流",
           "泄漏" not in _URGENCY_KEYWORDS[UrgencyLevel.CRITICAL] and precheck("泄漏") is None)

    t0 = time.perf_counter()
    for _ in range(1000):
        precheck("液氨钢瓶泄漏正在扩散")
    dt = time.perf_counter() - t0
    record("precheck 1000 次 < 1.5s（亚毫秒级，无 LLM/IO）", dt < 1.5, f"{dt:.3f}s")


def test_wiring_static():
    src = (Path(__file__).resolve().parents[1] / "api" / "main.py").read_text(encoding="utf-8")
    i_pre = src.find("precheck(req.message)")
    i_gate = src.find("_acquire_with_timeout(_CHAT_GATE")
    record("FastPath 在普通对话闸门之前完成预检分流", 0 <= i_pre < i_gate,
           f"precheck@{i_pre} chat_gate@{i_gate}")

    i_emg = src.find("_EMERGENCY_GATE = asyncio.Semaphore")
    record("应急通道使用独立有界闸门（不复用 _CHAT_GATE）",
           0 <= i_emg and "EMERGENCY_MAX_CONCURRENCY" in src[i_emg:i_emg + 220])

    start = src.find("async def _emergency_fast_path")
    end = src.find("async def _build_knowledge_context")
    seg = src[start:end] if 0 <= start < end else ""
    record("FastPath 不读记忆上下文/不调 LLM/RAG/不进知识库",
           bool(seg) and all(k not in seg for k in (
               "get_context", "_orchestrator.run", "_build_knowledge_context", "_tool_manager")),
           f"seg_len={len(seg)}")

    i_out = src.find("_outbox_loop")
    i_shutdown = src.find("hazard_overdue_task.cancel()")
    record("lifespan 挂载 outbox 投递循环并在 shutdown 取消",
           0 <= i_out and 0 <= i_shutdown and "outbox_task.cancel()" in src)


async def _async_main():
    import api.main as m
    from core.audit_store import AuditStore
    import core.emergency as em

    db = "data/_emergency_fastpath_test.db"
    for p in (db, db + "-wal", db + "-shm"):
        Path(p).unlink(missing_ok=True)
    store = AuditStore(db)
    m._audit = store
    m._memory = None          # 无记忆：快速分流不得依赖
    m._conv_store = None
    os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"   # 死端点占位：快速分流不依赖 LLM
    os.environ["ANTHROPIC_API_KEY"] = "dummy"

    async def run_fast(message, rid):
        sig = em.precheck(message)
        req = m.ChatRequest(message=message, user_id="t-fast", conv_id="c-fast")
        return await m._emergency_fast_path(req, sig, time.perf_counter(), request_id=rid)

    # ── A) 未配置通知渠道：仍建单/入队，文案绝不承诺"已通知/已收到" ──
    os.environ["ESCALATION_WEBHOOK_URL"] = ""
    resp = await run_fast("液氨钢瓶泄漏正在扩散", "emg-fp-1")
    record("FastPath 无 LLM/RAG 仍返回最小响应（escalated/意图/紧急度/耗时/编号回填）",
           resp.escalated is True and resp.emergency_fastpath is True
           and resp.intent == "emergency_response" and resp.urgency == "critical"
           and resp.request_id == "emg-fp-1" and resp.knowledge_used is False
           and resp.total_ms > 0 and resp.latency_ms > 0,
           f"escalated={resp.escalated} intent={resp.intent} total_ms={resp.total_ms}")
    record("最小响应含紧急事态通用要点与结尾免责声明",
           all(k in resp.response for k in
               ("紧急事态", "自身防护", "疏散隔离", "控制源头", "呼叫求援", "现场引导",
                "现场处置以本单位应急预案和应急指挥指令为准")),
           resp.response[:200])
    record("未配置渠道：文案不含'已通知'/'已收到'，状态区如实写未配置渠道",
           "已通知" not in resp.response and "已收到" not in resp.response
           and set(resp.emergency_status) == {"recorded", "ticket", "notify"}
           and "未配置通知渠道" in resp.emergency_status["notify"]
           and resp.emergency_status["recorded"] == "【已记录】上报编号 emg-fp-1",
           json.dumps(resp.emergency_status, ensure_ascii=False))

    tid = store.find_ticket_by_request("emg-fp-1", "emergency")
    tk = store.get_ticket(tid) if tid else None
    record("FastPath 已建应急工单（brief 简报齐备，来源编号可回查）",
           bool(tk) and tk["type"] == "emergency" and tk["priority"] == "critical"
           and tk["stage"] == "brief" and tk["intent"] == "emergency_response"
           and tk["urgency"] == "critical" and resp.emergency_status["ticket"] == f"【已建单】应急工单 #{tid}",
           json.dumps(tk, ensure_ascii=False, default=str))
    rows = {r["idempotency_key"]: r for r in store.list_notifications()}
    n1 = rows.get("emergency:emg-fp-1:webhook")
    record("未配置渠道仍入 outbox：pending + last_error='未配置通知渠道'（绝不写成 sent）",
           bool(n1) and n1["status"] == "pending" and n1["last_error"] == "未配置通知渠道"
           and n1["channel_configured"] is False and n1["ticket_id"] == tid,
           json.dumps(n1, ensure_ascii=False, default=str))

    # ── B) 已配置渠道：行为 pending，状态为"待发送"，不声称送达 ──
    os.environ["ESCALATION_WEBHOOK_URL"] = "http://127.0.0.1:9/hook"
    resp2 = await run_fast("中毒了", "emg-fp-2")
    n2 = {r["idempotency_key"]: r for r in store.list_notifications()}.get("emergency:emg-fp-2:webhook")
    record("已配置渠道：入队 pending，状态文案为'待发送'且不声称已送达",
           resp2.emergency_status["notify"] == "【通知】待发送"
           and "已送达" not in resp2.response and "已通知" not in resp2.response
           and "已收到" not in resp2.response
           and bool(n2) and n2["status"] == "pending" and n2["channel_configured"] is True,
           f"status={resp2.emergency_status.get('notify')} row={json.dumps(n2, ensure_ascii=False, default=str)}")

    # ── C) 幂等：同一 rid 重复调用不重复建单/不重复入队 ──
    resp3 = await run_fast("液氨钢瓶泄漏正在扩散", "emg-fp-1")
    dup_tickets = [t for t in store.list_tickets(limit=200)
                   if t["source_request_id"] == "emg-fp-1" and t["type"] == "emergency"]
    keys = [r["idempotency_key"] for r in store.list_notifications()]
    record("同一 rid 重复调用不重复建单（幂等，返回既有工单状态）",
           len(dup_tickets) == 1 and resp3.request_id == "emg-fp-1"
           and keys.count("emergency:emg-fp-1:webhook") == 1
           and resp3.escalated is True,
           f"tickets={len(dup_tickets)} keys={keys}")

    # ── D) 拿不到应急闸门容量：降级为"待补"，仍返回最小响应且无任何副作用 ──
    await m._EMERGENCY_GATE.acquire()      # 占满唯一容量（EMERGENCY_MAX_CONCURRENCY=1）
    try:
        resp4 = await run_fast("着火了快来人", "emg-fp-4")
    finally:
        m._EMERGENCY_GATE.release()
    record("应急闸门满时降级为待补（仍返回最小响应、不抛错、不占普通对话闸门）",
           resp4.escalated is True and "紧急事态" in resp4.response
           and "待补" in resp4.emergency_status["recorded"]
           and resp4.emergency_status["ticket"] == "【建单】暂未成功，已记录待补建"
           and "已通知" not in resp4.response and "已收到" not in resp4.response
           and store.find_ticket_by_request("emg-fp-4", "emergency") is None
           and not any(r["idempotency_key"] == "emergency:emg-fp-4:webhook"
                       for r in store.list_notifications()),
           json.dumps(resp4.emergency_status, ensure_ascii=False))

    store.close()
    for p in (db, db + "-wal", db + "-shm"):
        Path(p).unlink(missing_ok=True)


def main():
    test_precheck()
    test_wiring_static()
    asyncio.run(_async_main())
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
