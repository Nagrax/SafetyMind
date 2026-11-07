# -*- coding: utf-8 -*-
# SM-10 红灯测试：隐患写入、建议消费与通知同事务。python tests/test_hazard_tx_atomic.py
#
# 契约源（先于实现定死）：scratch/sm-loop/handoff/SM-10/decision.md
#   - 带显式幂等键（X-Idempotency-Key）的 /hazard/confirm：预留→去重→工单→建议标记→
#     outbox 入队五步一个 BEGIN IMMEDIATE；同键同哈希=重放原收据（200+replayed:true）、
#     同键异哈希=冲突行先落 hazard_ops 再 409、任一步失败零半提交；
#   - 无键请求语义逐字节不变（test_destructive_isolated.py S1 的"重复确认 404"不动）。
# 机制参考：carlaredoc-mutation-mapping.md §3（同键并发恰一 applied / 同键异 payload
#   409+冲突行可回放 / 冲突先落审计再抛），按同步 SQLite 独立实现。
#
# 用例（红灯 = 在当前产品上真实失败；逐条记录现状）：
#   T1 同键并发 10（多进程 spawn+barrier，对齐 test_outbox_multiworker 手法）：
#      全部 200、收据全同、恰 1 单 1 outbox 行、repeat_report(同 rid)=0、
#      hazard_ops applied=1/replayed=9；
#   T2 同键重试/异内容：重试 200 同收据+replayed；异内容 409+conflicted 行
#      （expected/current 可回放）；重放六表零漂移；
#   T3 重复上报独立事件+无重复通知（/chat 端到端，守卫既有正确语义）；
#   T4a 事务前异常：零写入、建议保持 pending、恢复后同键恰一次；
#   T4b 消费中断（mark 注入崩溃）：零半提交（无工单/时间线/outbox）+恢复后重试
#      恰 1 单 0 repeat_report（现状：工单已建=半提交，重试被误合并）；
#   T4d 渠道受理后标记前崩溃（store 级）：reap 重投 payload 逐字节同且带
#      idempotency_key，行数恒 1。
#
# 隔离铁律 9：全部存储走 tempfile 临时目录（本文件非破坏性演练，不用 iso 守卫形态；
# 与 test_hazard_tickets 同款），工程目录不留残留。
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

STAMP = __import__("datetime").datetime.now().strftime("%H%M%S%f")
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""),
          flush=True)


def db_snapshot(db_path):
    """六表全量快照（canonical JSON，对齐 test_destructive_isolated.db_snapshot）。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = ["audit_log", "upgrade_events", "tickets", "hazard_suggestions",
                  "ticket_timeline", "notification_outbox"]
        return {t: json.dumps([dict(r) for r in con.execute(f"SELECT * FROM {t}")],
                              sort_keys=True, ensure_ascii=False, default=str)
                for t in tables}
    finally:
        con.close()


def db_delta(before, after):
    return sorted(t for t in before if before[t] != after[t])


def q1(db_path, sql, args=()):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def ops_counts(db_path, action, subject, idem_key):
    """hazard_ops 按状态计数；机制未实现时抛 OperationalError（调用方按红灯记录）。"""
    rows = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for st, n in con.execute(
                "SELECT status, COUNT(*) FROM hazard_ops"
                " WHERE action=? AND subject=? AND idem_key=? GROUP BY status",
                (action, subject, idem_key)):
            rows[st] = int(n)
    finally:
        con.close()
    return rows


def sug_payload(summary, entities, rid):
    return {"decision": {"action": "suggest", "severity": "general", "summary": summary,
                         "dept": "设备动力部", "deadline_hours": 72, "basis": "sm10 红灯夹具"},
            "brief": {"intent": "hazard_report", "urgency": "medium",
                      "entities": entities, "message_digest": summary,
                      "routing_reason": "sm10-tx-atomic", "request_id": rid}}


# ── T1 子进程入口（spawn：模块级函数，按引用 pickle；env 由父进程钉死后继承）────────
def _confirm_worker(db_path, rid, body, idem_key, barrier, q):
    try:
        sys.path.insert(0, str(REPO))
        from core.audit_store import AuditStore
        store = AuditStore(db_path)          # 先 open 再 barrier（排除初始化耗时干扰）
        import api.main as api_main
        api_main._audit = store              # 与 lifespan 同款构造（main.py:405-408），免起服务
        from fastapi.testclient import TestClient
        client = TestClient(api_main.app)    # 不进 lifespan（对齐 test_destructive_isolated:176）
        barrier.wait(timeout=120)
        r = client.post("/hazard/confirm", json=body,
                        headers={"X-Idempotency-Key": idem_key})
        try:
            bj = r.json()
        except Exception:
            bj = {}
        q.put({"code": r.status_code, "ticket_id": int(bj.get("ticket_id") or 0),
               "merged": bj.get("merged"), "replayed": bj.get("replayed"),
               "detail": str(bj.get("detail", ""))[:120]})
        store.close()
    except Exception as ex:                  # 子进程任何异常都作为一份"收据"上交，不静默
        q.put({"code": f"EXC:{type(ex).__name__}", "ticket_id": 0, "merged": None,
               "replayed": None, "detail": str(ex)[:200]})


def t1_same_key_race(tmp_root):
    """T1 同键并发 ≥10：恰好一次消费 + 同收据。"""
    n = 10
    db = str(Path(tmp_root) / f"t1_race_{STAMP}.db")
    rid = f"SM10-T1-{STAMP}"
    idem = f"sm10-t1-key-{STAMP}"
    body = {"request_id": rid, "user_id": "u-race"}
    from core.audit_store import AuditStore
    seed = AuditStore(db)                    # 父进程先建库+建议行，再放 10 个并发消费者
    seed.save_hazard_suggestion(rid, "u-race", f"conv-{rid}",
                                sug_payload("甲区3号储罐底部渗漏", {"equipment_id": ["P-RACE"]}, rid))
    seed.close()

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(n)
    q = ctx.Queue()
    ps = [ctx.Process(target=_confirm_worker, args=(db, rid, body, idem, barrier, q))
          for _ in range(n)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=180)
    receipts = []
    while not q.empty():
        receipts.append(q.get())

    dist = {}
    for r in receipts:
        k = (str(r["code"]), r["ticket_id"], str(r["merged"]))
        dist[k] = dist.get(k, 0) + 1
    dist_s = "; ".join(f"code={k[0]} ticket={k[1]} merged={k[2]} ×{v}" for k, v in dist.items())

    record("T1|同键并发 10：全部 200（重放不是 404/异常）",
           len(receipts) == n and all(r["code"] == 200 for r in receipts),
           f"收据分布: {dist_s or '无收据(子进程全超时)'}")
    tids = {r["ticket_id"] for r in receipts}
    merged = {str(r["merged"]) for r in receipts}
    record("T1|同键并发 10：收据全同（同工单号同 merged，同收据重放）",
           len(tids) == 1 and len(merged) == 1 and next(iter(tids), 0) > 0,
           f"收据分布: {dist_s}")
    one = q1(db, "SELECT COUNT(*) AS n FROM tickets WHERE source_request_id=?", (rid,))
    ob = q1(db, "SELECT COUNT(*) AS n FROM notification_outbox WHERE request_id=?", (rid,))
    rr = q1(db, "SELECT COUNT(*) AS n FROM ticket_timeline WHERE action='repeat_report'"
                " AND detail LIKE ?", (f'%"{rid}"%',))
    record("T1|同键并发 10：恰好一次消费（恰 1 张工单 + 恰 1 行 outbox，"
           "同键重试不得误判成业务合并）",
           one and one["n"] == 1 and ob and ob["n"] == 1 and rr and rr["n"] == 0,
           f"tickets={one['n'] if one else '?'} outbox={ob['n'] if ob else '?'} "
           f"repeat_report={rr['n'] if rr else '?'}")
    try:
        ops = ops_counts(db, "hazard_confirm", "u-race", idem)
        record("T1|hazard_ops 裁决痕：applied 恰 1 + replayed 恰 9（可回放）",
               ops.get("applied") == 1 and ops.get("replayed") == n - 1, f"ops={ops}")
    except sqlite3.OperationalError as ex:
        record("T1|hazard_ops 裁决痕：applied 恰 1 + replayed 恰 9（可回放）",
               False, f"机制未实现（{ex}）")
    for p in ps:
        if p.is_alive():
            p.terminate()


def t2_t4_retry_conflict_faults(tmp_root):
    """T2 同键重试/异内容 + T4a 事务前异常 + T4b 消费中断（HTTP 层，无 lifespan）。"""
    db = str(Path(tmp_root) / f"t2_t4_{STAMP}.db")
    from core.audit_store import AuditStore
    store = AuditStore(db)
    from fastapi.testclient import TestClient
    import api.main as api_main
    api_main._audit = store
    client = TestClient(api_main.app)                       # 正常路径
    client500 = TestClient(api_main.app, raise_server_exceptions=False)  # 注入 500 路径

    def confirm(c, rid, user, conv, key=None):
        headers = {"X-Idempotency-Key": key} if key else {}
        return c.post("/hazard/confirm", json={"request_id": rid, "user_id": user,
                                               "conv_id": conv}, headers=headers)

    # ══ T2 同键重放/同键异内容 ══════════════════════════════════════════════
    rid2 = f"SM10-T2-{STAMP}"
    key2 = f"sm10-t2-key-{STAMP}"
    store.save_hazard_suggestion(rid2, "u-tx2", "c-1",
                                 sug_payload("乙区配电柜绝缘垫缺失", {"area": ["乙区"]}, rid2))
    b0 = db_snapshot(db)
    r1 = confirm(client, rid2, "u-tx2", "c-1", key2)
    body1 = r1.json() if r1.status_code == 200 else {}
    t2_tid = int(body1.get("ticket_id") or 0)
    record("T2|首次确认（带键）→ 200 建单",
           r1.status_code == 200 and body1.get("ok") is True and body1.get("merged") is False
           and t2_tid > 0, f"code={r1.status_code} body={r1.text[:160]}")
    b1 = db_snapshot(db)
    r2 = confirm(client, rid2, "u-tx2", "c-1", key2)
    body2 = r2.json() if r2.status_code == 200 else {}
    record("T2|提交后断线重试（同键同内容）→ 200 同收据（同工单号）+replayed",
           r2.status_code == 200 and int(body2.get("ticket_id") or 0) == t2_tid
           and body2.get("merged") == body1.get("merged")
           and body2.get("replayed") is True,
           f"code={r2.status_code} body={r2.text[:160]}")
    record("T2|重放零漂移（六表逐字节不变）", db_delta(b1, db_snapshot(db)) == [],
           f"delta={db_delta(b1, db_snapshot(db))}")
    r3 = confirm(client, rid2, "u-tx2", "c-2", key2)
    record("T2|同键异内容（conv_id 改写）→ 409 幂等冲突（不是 404 也不是静默二次消费）",
           r3.status_code == 409 and "IDEMPOTENCY_CONFLICT" in r3.text,
           f"code={r3.status_code} body={r3.text[:160]}")
    r4 = confirm(client, rid2, "u-tx2", "c-1", key2)
    body4 = r4.json() if r4.status_code == 200 else {}
    record("T2|冲突后原内容重试 → 仍 200 原收据（冲突不破坏重放）",
           r4.status_code == 200 and int(body4.get("ticket_id") or 0) == t2_tid
           and body4.get("replayed") is True,
           f"code={r4.status_code} body={r4.text[:160]}")
    try:
        rows = {}
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        for r in con.execute("SELECT * FROM hazard_ops WHERE action=? AND subject=?"
                             " AND idem_key=? ORDER BY id",
                             ("hazard_confirm", "u-tx2", key2)):
            rows.setdefault(r["status"], []).append(dict(r))
        con.close()
        applied = rows.get("applied", [])
        conflicted = rows.get("conflicted", [])
        record("T2|hazard_ops 审计可回放：applied 1 行（存收据）+ conflicted 1 行"
               "（expected/current 双方哈希）",
               len(applied) == 1 and len(conflicted) == 1
               and json.loads(applied[0]["receipt"]).get("ticket_id") == t2_tid
               and "expected" in conflicted[0]["detail"] and "current" in conflicted[0]["detail"],
               f"applied={len(applied)} conflicted={len(conflicted)}")
    except sqlite3.OperationalError as ex:
        record("T2|hazard_ops 审计可回放：applied 1 行（存收据）+ conflicted 1 行"
               "（expected/current 双方哈希）", False, f"机制未实现（{ex}）")

    # ══ T4a 事务前异常（去重判定即抛）：零写入 ══════════════════════════════
    rid3 = f"SM10-T4A-{STAMP}"
    key3 = f"sm10-t4a-key-{STAMP}"
    store.save_hazard_suggestion(rid3, "u-tx2", "c-t4a",
                                 sug_payload("丙区灭火器过期", {"area": ["丙区"]}, rid3))
    orig_dup = store.find_open_hazard_duplicate

    def _boom_dup(*a, **k):
        raise RuntimeError("sm10-injected: 事务前异常")
    store.find_open_hazard_duplicate = _boom_dup
    try:
        b = db_snapshot(db)
        r = confirm(client500, rid3, "u-tx2", "c-t4a", key3)
        delta = db_delta(b, db_snapshot(db))
        record("T4a|事务前异常 → 拒绝且六表零漂移、建议保持 pending",
               r.status_code >= 500 and not delta
               and store.get_hazard_suggestion(rid3) is not None,
               f"code={r.status_code} delta={delta}")
        try:
            ops = ops_counts(db, "hazard_confirm", "u-tx2", key3)
            record("T4a|事务前异常不落 applied 预留行（可安全重试）",
                   ops.get("applied", 0) == 0, f"ops={ops}")
        except sqlite3.OperationalError as ex:
            record("T4a|事务前异常不落 applied 预留行（可安全重试）",
                   False, f"机制未实现（{ex}）")
    finally:
        store.find_open_hazard_duplicate = orig_dup
    r = confirm(client, rid3, "u-tx2", "c-t4a", key3)
    n3 = q1(db, "SELECT COUNT(*) AS n FROM tickets WHERE source_request_id=?", (rid3,))
    record("T4a|恢复后同键重试 → 200 且恰好建 1 单",
           r.status_code == 200 and n3 and n3["n"] == 1
           and (r.json() or {}).get("merged") is False,
           f"code={r.status_code} tickets={n3['n'] if n3 else '?'} body={r.text[:120]}")

    # ══ T4b 消费中断（标记建议时注入崩溃）：零半提交 ═════════════════════════
    rid4 = f"SM10-T4B-{STAMP}"
    key4 = f"sm10-t4b-key-{STAMP}"
    store.save_hazard_suggestion(rid4, "u-tx2", "c-t4b",
                                 sug_payload("丁区梯子踏板开裂", {"area": ["丁区"]}, rid4))
    orig_mark = store.mark_hazard_suggestion

    def _boom_mark(*a, **k):
        raise RuntimeError("sm10-injected: 消费中断")
    store.mark_hazard_suggestion = _boom_mark
    try:
        b = db_snapshot(db)
        r = confirm(client500, rid4, "u-tx2", "c-t4b", key4)
        snap_after = db_snapshot(db)
        n_t = q1(db, "SELECT COUNT(*) AS n FROM tickets WHERE source_request_id=?", (rid4,))
        n_ob = q1(db, "SELECT COUNT(*) AS n FROM notification_outbox WHERE request_id=?", (rid4,))
        record("T4b|消费中断 → 请求失败", r.status_code >= 500,
               f"code={r.status_code}")
        record("T4b|零半提交：无工单、无 outbox 行、建议仍 pending（现状：工单已建=半提交）",
               n_t and n_t["n"] == 0 and n_ob and n_ob["n"] == 0
               and store.get_hazard_suggestion(rid4) is not None,
               f"tickets={n_t['n'] if n_t else '?'} outbox={n_ob['n'] if n_ob else '?'} "
               f"pending={store.get_hazard_suggestion(rid4) is not None} "
               f"delta={db_delta(b, snap_after)}")
    finally:
        store.mark_hazard_suggestion = orig_mark
    r = confirm(client, rid4, "u-tx2", "c-t4b", key4)
    body = r.json() if r.status_code == 200 else {}
    rr = q1(db, "SELECT COUNT(*) AS n FROM ticket_timeline WHERE action='repeat_report'"
                " AND detail LIKE ?", (f'%"{rid4}"%',))
    n_t = q1(db, "SELECT COUNT(*) AS n FROM tickets WHERE source_request_id=?", (rid4,))
    record("T4b|恢复后同键重试 → 200 首次应用（merged=false）、恰 1 单、"
           "重试不被误合并成 repeat_report",
           r.status_code == 200 and body.get("merged") is False
           and n_t and n_t["n"] == 1 and rr and rr["n"] == 0,
           f"code={r.status_code} merged={body.get('merged')} tickets={n_t['n'] if n_t else '?'}"
           f" repeat_report={rr['n'] if rr else '?'}")

    store.close()
    api_main._audit = None


def t4d_channel_crash(tmp_root):
    """T4d 渠道受理后标记前崩溃（store 级）：reap 重投 payload 幂等。"""
    db = str(Path(tmp_root) / f"t4d_{STAMP}.db")
    from core.audit_store import AuditStore
    store = AuditStore(db)
    key = f"sm10-chan-{STAMP}"
    payload = {"request_id": f"SM10-T4D-{STAMP}", "text": "值班通知（幂等投递）"}
    nid = store.enqueue_notification(key, "http://127.0.0.1:9/hook", payload,
                                     request_id=payload["request_id"])
    record("T4d|outbox 入队 1 行", nid > 0 and store.notification_stats().get("pending") == 1)
    now0 = time.time()
    claimed = store.claim_pending_notifications(limit=5, now=now0)
    record("T4d|领取转 sending（模拟投递中）", len(claimed) == 1
           and claimed[0]["status"] == "sending")
    # —— 崩溃点：渠道已受理，但 mark_notification_sent 未及执行 ——
    store.reap_stale_sending(stale_s=100.0, now=now0 + 200.0)   # 悬挂复位
    deliveries = []
    claimed2 = store.claim_pending_notifications(limit=5, now=now0 + 201.0)
    for row in claimed2:
        try:
            deliveries.append(json.loads(row.get("payload") or "{}"))
        except (ValueError, TypeError):
            deliveries.append({})
    record("T4d|崩溃后 reap 复位并重投（不丢通知，行数恒 1）",
           len(claimed2) == 1 and store.notification_stats().get("sending") == 1,
           f"reclaimed={len(claimed2)} stats={store.notification_stats()}")
    record("T4d|重投 payload 与首次逐字节相同（渠道可安全去重）",
           len(deliveries) == 1 and deliveries[0].get("request_id") == payload["request_id"]
           and deliveries[0].get("text") == payload["text"],
           f"payload={json.dumps(deliveries, ensure_ascii=False)[:200]}")
    record("T4d|payload 携带 idempotency_key（渠道侧幂等去重依据）",
           len(deliveries) == 1 and deliveries[0].get("idempotency_key") == key,
           f"payload_keys={sorted(deliveries[0].keys()) if deliveries else '?'}")
    if claimed2:
        store.mark_notification_sent(claimed2[0]["id"])
    record("T4d|标记送达后不再重投（sent 终态）",
           store.claim_pending_notifications(limit=5, now=now0 + 202.0) == []
           and store.get_notification(claimed2[0]["id"])["status"] == "sent"
           if claimed2 else False)
    store.close()


def t3_duplicate_report(tmp_root):
    """T3 重复上报=独立事件（repeat_report 痕）+ 无重复通知（outbox 同键不新增）。"""
    tmp = Path(tmp_root)
    db = str(tmp / f"t3_chat_{STAMP}.db")
    os.environ.update({
        "AUDIT_DB_PATH": db,
        "PREFERENCE_DB_PATH": str(tmp / f"t3_pref_{STAMP}.db"),
        "SAFETYMIND_MEMORY_SNAPSHOT": str(tmp / f"t3_mem_{STAMP}.json"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(tmp / f"t3_convs_{STAMP}"),
        "CHROMA_PERSIST_DIRECTORY": str(tmp / f"t3_chroma_{STAMP}"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(tmp / f"t3_kbchroma_{STAMP}"),
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "API_HOST": "0.0.0.0",
        "ADMIN_TOKEN": f"sm10-admin-{STAMP}",
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_CLARIFY": "0",
        "SAFETYMIND_OUTBOX_ENABLED": "1",
        "ESCALATION_WEBHOOK_URL": "",        # channel=''：行落库但永不被领取，计数稳定
        "AUDIT_ENABLED": "1",
    })
    from core import hazard_gate
    from fastapi.testclient import TestClient
    import api.main as api_main
    fake_auto = {"action": "auto", "severity": "major", "category": "易燃易爆设施及场所",
                 "basis": "GB（sm10 红灯夹具）", "dept": "安环部", "deadline_hours": 24,
                 "summary": "易燃易爆设施及场所隐患：罐区3号储罐底部渗漏"}
    orig_decide = hazard_gate.decide
    admin = {"X-Admin-Token": f"sm10-admin-{STAMP}"}
    with TestClient(api_main.app) as client:                 # lifespan：/chat 依赖
        hazard_gate.decide = lambda *a, **k: dict(fake_auto)
        try:
            r1 = client.post("/chat", json={"message": "位号P-101储罐底部渗漏，需要处理",
                                            "user_id": "u-tx3", "conv_id": "c-tx3"})
            b1 = r1.json()
            tid = int(b1.get("hazard_ticket_id") or 0)
            rid1 = b1.get("request_id", "")
            record("T3|首报 auto 建单", r1.status_code == 200
                   and b1.get("hazard_action") == "auto_ticket" and tid > 0,
                   f"code={r1.status_code} body={ {k: b1.get(k) for k in ('hazard_action', 'hazard_ticket_id')} }")
            n_ob1 = q1(db, "SELECT COUNT(*) AS n FROM notification_outbox WHERE ticket_id=?", (tid,))
            record("T3|首报建单通知恰 1 行", n_ob1 and n_ob1["n"] == 1,
                   f"outbox={n_ob1['n'] if n_ob1 else '?'}")
            r2 = client.post("/chat", json={"message": "位号P-101储罐底部还在渗漏，越来越明显了",
                                            "user_id": "u-tx3", "conv_id": "c-tx3"})
            b2 = r2.json()
            rid2 = b2.get("request_id", "")
            record("T3|24h 同实体重复上报合并到既有单",
                   r2.status_code == 200 and b2.get("hazard_action") == "merged"
                   and int(b2.get("hazard_ticket_id") or 0) == tid,
                   f"code={r2.status_code} body={ {k: b2.get(k) for k in ('hazard_action', 'hazard_ticket_id')} }")
            tl = client.get(f"/tickets/{tid}/timeline", headers=admin).json().get("timeline", [])
            rep = [e for e in tl if e.get("action") == "repeat_report"
                   and (e.get("detail") or {}).get("request_id") == rid2]
            record("T3|重复上报是独立事件（repeat_report 痕含新上报编号，不被吞掉）",
                   bool(rep), f"timeline_actions={[e.get('action') for e in tl][-6:]}")
            n_ob2 = q1(db, "SELECT COUNT(*) AS n FROM notification_outbox WHERE ticket_id=?", (tid,))
            record("T3|无重复通知（合并后 outbox 仍恰 1 行）",
                   n_ob2 and n_ob2["n"] == 1, f"outbox={n_ob2['n'] if n_ob2 else '?'}")
        finally:
            hazard_gate.decide = orig_decide


def main():
    tmp_root = tempfile.mkdtemp(prefix="sm10_tx_atomic_")
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
        "SAFETYMIND_OUTBOX_ENABLED": "0",    # HTTP 层无 lifespan 本就不起 worker；显式钉死
        "PROMETHEUS_PORT": "0",
        "AUDIT_ENABLED": "1",
        "API_HOST": "127.0.0.1",
        "SAFETYMIND_DEPLOYED": "",           # 本地形态：主体=生效 user_id（decision §2.2）
        # 先于 import 钉死（api.main 导入时 load_dotenv 不覆盖已显式设置的变量）：
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "ESCALATION_WEBHOOK_URL": "",        # channel=''：outbox 行落库但永不被领取
    })
    try:
        t4d_channel_crash(tmp_root)
        t1_same_key_race(tmp_root)
        t2_t4_retry_conflict_faults(tmp_root)
        t3_duplicate_report(tmp_root)
    finally:
        failed = RESULTS.count(False)
        total = len(RESULTS)
        print(f"\n{total - failed}/{total} PASSED")
        print(f"[SM-10 RED 环境] 临时目录（保留备查，进程退出即弃）: {tmp_root}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
