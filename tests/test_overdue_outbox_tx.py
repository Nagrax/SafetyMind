# -*- coding: utf-8 -*-
# SM-11 红灯测试：逾期事件与入队同事务、等待渠道恢复、领取代次守卫、可信通知回执。
# python tests/test_overdue_outbox_tx.py
#
# 契约源（先于实现定死）：scratch/sm-loop/handoff/SM-11/decision.md
#   - T1/T2：check_overdue_tickets 的"判重→记事件→入队"收敛为单 BEGIN IMMEDIATE
#     （audit_store.py:1114-1130 现状为两段事务 + check-then-act 跨锁窗口：
#       事件 commit 在 record_ticket_event @audit_store.py:695，通知入队另起事务在
#       api/main.py:423-428 → _enqueue_ticket_notification → enqueue_notification @372-382）；
#   - T3：未配置渠道 → waiting（待配置，不耗 attempts、不计失败），旧空渠道行
#     （pending+channel=''）一次性迁移为 waiting，补配置后经 adopt_waiting_notifications
#     可安全领取（现状伪装 pending 且永不恢复，audit_store.py:353-370/405-414）；
#   - T4：领取发代次 claim_generation，mark sent/failed 校验代次——旧代次（被 reap 复位的
#     悬挂消费者）无法改写当前领取的结果（现状 WHERE id=? 裸更新 @428-447）；
#   - T5：连续不同事件独立送达、同一事件重试保留行 id（守卫 + kind 参数红灯）；
#   - T6：外部受理与本地提交间杀进程 → at-least-once 边界实录（同键恰 2 次、payload
#     逐字节同且带幂等键、行 id 不变）——披露/守卫用例（SM-10 §2.5.3 幂等键已落地，
#     现状预期绿，钉住不回退）；
#   - T7：按 (kind,channel) 健康项连败 3 次才 breached（对标 dsh effects.ts:7
#     FAILURES_BEFORE_ERROR=3）。
#
# 手法沿用：多进程 spawn+barrier（tests/test_outbox_multiworker.py）、实例属性 monkeypatch
# 注入与 tempfile 隔离（tests/test_hazard_tx_atomic.py，隔离铁律 9：工程目录不留残留）。
# 全程假 sender / 本地账本文件，不发任何真实网络请求。退出码 0 = 全过。
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
HOOK = "http://hook.local/sm11"          # 假渠道：测试内从不真正拨号
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""),
          flush=True)


def q1(db_path, sql, args=()):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def fresh(tmp_root, name):
    from core.audit_store import AuditStore
    db = str(Path(tmp_root) / f"{name}_{STAMP}.db")
    return AuditStore(db), db


def make_overdue_ticket(store, title, rid, overdue_hours=1.0):
    """建一张已逾期未闭环工单（due_ts 在过去、status=open）。"""
    return store.create_ticket(
        title, type_="hazard", priority="high", source_request_id=rid,
        due_ts=time.time() - overdue_hours * 3600.0,
        brief={"intent": "hazard_report", "urgency": "medium", "entities": {},
               "message_digest": title[:40], "routing_reason": "sm11 红灯夹具",
               "request_id": rid})


def dance_enqueue(store, ticket):
    """现状口径复刻 api/main.py:423-428 的入队步（幂等键与 _enqueue_ticket_notification
    @api/main.py:970 同款：ticket:{id}:overdue:webhook）。修复后与事务内入队同键去重。"""
    return store.enqueue_notification(
        f"ticket:{ticket['id']}:overdue:webhook", HOOK,
        {"request_id": ticket.get("source_request_id", ""),
         "text": f"【SafetyMind 工单逾期】#{ticket['id']} {ticket['title']}（sm11 红灯夹具）"},
        request_id=ticket.get("source_request_id", ""), ticket_id=ticket["id"])


# ── T1 子进程入口（spawn：模块级函数，按引用 pickle）────────────────────────────
def _scan_worker(db_path, barrier, q, record_delay, scan_delay):
    try:
        sys.path.insert(0, str(REPO))
        from core.audit_store import AuditStore
        store = AuditStore(db_path)
        if record_delay > 0:                    # 拉宽"判重→记账"窗口（确定性复现竞态）
            orig = store.record_ticket_event

            def slow_record(*a, **k):
                time.sleep(record_delay)
                return orig(*a, **k)
            store.record_ticket_event = slow_record
        barrier.wait(timeout=120)
        if scan_delay:
            time.sleep(scan_delay)
        got = store.check_overdue_tickets()
        for t in got:                           # 现状两段式的第二段（api/main.py:424-428 同款）
            dance_enqueue(store, t)
        q.put({"returned": len(got), "err": ""})
        store.close()
    except Exception as ex:                     # 子进程异常如实上交，不静默
        q.put({"returned": 0, "err": f"{type(ex).__name__}: {ex}"})


def t1_two_process_scan(tmp_root):
    """T1 两进程扫描同一逾期单：恰 1 条 overdue_notify 事件 + 恰 1 行待发通知。"""
    db = str(Path(tmp_root) / f"t1_scan_{STAMP}.db")
    rid = f"SM11-T1-{STAMP}"
    from core.audit_store import AuditStore
    seed = AuditStore(db)
    tid = make_overdue_ticket(seed, "甲区3号储罐底部渗漏（逾期）", rid)
    seed.close()

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    q = ctx.Queue()
    # A：barrier 即扫、记账前睡 1.0s；B：barrier 后睡 0.12s 再扫（0.12 << 1.0，B 的判重
    # 必然落在 A 提交之前）→ 现状 check-then-act 窗口被确定性拉满，两进程各记一条事件。
    ps = [ctx.Process(target=_scan_worker, args=(db, barrier, q, 1.0, 0.0)),
          ctx.Process(target=_scan_worker, args=(db, barrier, q, 1.0, 0.12))]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=120)
    receipts = []
    while not q.empty():
        receipts.append(q.get())
    errs = [r["err"] for r in receipts if r.get("err")]

    n_events = q1(db, "SELECT COUNT(*) AS n FROM ticket_timeline"
                      " WHERE ticket_id=? AND action='overdue_notify'", (tid,))
    n_rows = q1(db, "SELECT COUNT(*) AS n FROM notification_outbox WHERE ticket_id=?", (tid,))
    record("T1|两进程扫描同一逾期单：overdue_notify 事件恰 1 条（现状 check-then-act 跨锁竞态实录）",
           n_events and n_events["n"] == 1 and not errs,
           f"events={n_events['n'] if n_events else '?'} rows={n_rows['n'] if n_rows else '?'} "
           f"worker_errs={errs or '无'}")
    record("T1|两进程扫描同一逾期单：outbox 待发恰 1 行（幂等键去重守卫，现状已正确）",
           n_rows and n_rows["n"] == 1, f"rows={n_rows['n'] if n_rows else '?'}")
    for p in ps:
        if p.is_alive():
            p.terminate()


def t2_injected_failure_recovery(tmp_root):
    """T2 注入失败可恢复：入队崩溃后，事件与通知必须能经重扫恢复（现状：通知永久丢失）。"""
    db = str(Path(tmp_root) / f"t2_recover_{STAMP}.db")
    rid = f"SM11-T2-{STAMP}"
    from core.audit_store import AuditStore
    store = AuditStore(db)
    tid = make_overdue_ticket(store, "乙区配电柜绝缘垫缺失（逾期）", rid)

    orig_enqueue_locked = store._enqueue_locked     # 注入点：契约 §2.1 钉死的原子路径入队口
    state = {"fail_once": True}

    def flaky_enqueue_locked(*a, **k):
        if state["fail_once"]:
            state["fail_once"] = False
            raise RuntimeError("sm11-injected: 入队崩溃（事务内/事务外注入面）")
        return orig_enqueue_locked(*a, **k)
    store._enqueue_locked = flaky_enqueue_locked

    # 第一轮扫描 + 入队步（现状两段式；api/main.py:976-977 对入队失败只告警不重试）
    try:
        for t in store.check_overdue_tickets():
            try:
                dance_enqueue(store, t)
            except Exception:
                pass                                # 入队失败被吞（现状口径）
    except Exception:
        pass                                        # 修复后：事务整体回滚、异常上抛（api/main.py:429-430 同款捕获）
    mid_events = q1(db, "SELECT COUNT(*) AS n FROM ticket_timeline"
                        " WHERE ticket_id=? AND action='overdue_notify'", (tid,))
    mid_rows = q1(db, "SELECT COUNT(*) AS n FROM notification_outbox WHERE ticket_id=?", (tid,))
    print(f"    [T2 中间态实录] 注入后：events={mid_events['n']} rows={mid_rows['n']}"
          f"（现状=事件已提交而通知缺失；修复后=整体回滚归零）", flush=True)

    store._enqueue_locked = orig_enqueue_locked     # 恢复
    try:
        for t in store.check_overdue_tickets():     # 恢复后重扫（唯一的重试来源）
            try:
                dance_enqueue(store, t)
            except Exception:
                pass
    except Exception:
        pass

    n_events = q1(db, "SELECT COUNT(*) AS n FROM ticket_timeline"
                      " WHERE ticket_id=? AND action='overdue_notify'", (tid,))
    n_rows = q1(db, "SELECT COUNT(*) AS n FROM notification_outbox WHERE ticket_id=?", (tid,))
    record("T2|注入失败恢复后：逾期事件与待发通知必须齐全（无重试来源的丢失即缺陷）",
           n_events and n_events["n"] == 1 and n_rows and n_rows["n"] == 1,
           f"events={n_events['n'] if n_events else '?'} rows={n_rows['n'] if n_rows else '?'}"
           f"（中间态 events={mid_events['n']} rows={mid_rows['n']}）")
    try:
        krow = q1(db, "SELECT kind FROM notification_outbox WHERE ticket_id=?", (tid,))
        record("T2|扫描路径入队行 kind='overdue'（kind 分类契约 §2.2）",
               krow is not None and krow["kind"] == "overdue",
               f"kind={krow['kind'] if krow else '行不存在'}")
    except sqlite3.OperationalError as ex:
        record("T2|扫描路径入队行 kind='overdue'（kind 分类契约 §2.2）",
               False, f"机制未实现（{ex}）")
    store.close()


def t3_waiting_rows(tmp_root):
    """T3 空渠道行与 waiting：待配置显式化、旧行迁移、不耗 attempts 不计失败、补配置可恢复。"""
    from core.audit_store import AuditStore
    from core.notification_outbox import deliver_pending
    store, db = fresh(tmp_root, "t3_wait")

    # a) 未配置渠道入队 → waiting（现状伪装 pending）
    wid = store.enqueue_notification("sm11-wait:new", "", {"request_id": "w1", "text": "t"})
    n = store.get_notification(wid)
    record("T3|未配置渠道入队 → waiting（待配置，不再伪装 pending）",
           n["status"] == "waiting", f"status={n['status']} last_error={n['last_error']!r}")
    # b) stats 区分 waiting，不混入 pending/failed
    stats = store.notification_stats()
    record("T3|stats 增 waiting 桶且不混入 pending/failed",
           stats.get("waiting") == 1 and stats.get("pending") == 0 and stats.get("failed") == 0,
           json.dumps(stats, ensure_ascii=False))
    # c) 旧空渠道行（pending+channel=''）一次性迁移为可识别 waiting
    con = sqlite3.connect(db)
    con.execute("INSERT INTO notification_outbox (created_ts, idempotency_key, channel,"
                " payload, status, attempts, next_attempt_ts, last_error)"
                " VALUES (?,?,?,?, 'pending', 0, 0, '未配置通知渠道')",
                (time.time(), "sm11-wait:legacy", "", "{}"))   # 旧形态：pending+channel=''
    con.commit()
    con.close()
    store.close()
    store = AuditStore(db)                          # 重新打开触发迁移（契约 §2.4）
    legacy = {r["idempotency_key"]: r for r in store.list_notifications(limit=50)}.get(
        "sm11-wait:legacy")
    record("T3|旧空渠道行（pending+channel=''）迁移为可识别 waiting（只读扫描→定向 UPDATE）",
           legacy is not None and legacy["status"] == "waiting",
           f"status={legacy['status'] if legacy else '行不存在'}")
    # d) waiting 不被领取、不耗 attempts、不计失败
    res = deliver_pending(store, lambda url, payload: True, batch=10, max_attempts=5)
    rows = {r["idempotency_key"]: r for r in store.list_notifications(limit=50)}
    all_waiting = all(rows[k]["attempts"] == 0 for k in ("sm11-wait:new", "sm11-wait:legacy"))
    record("T3|waiting 行不被领取且零 attempts、零失败计数（Later 语义）",
           res["claimed"] == 0 and res["failed"] == 0 and all_waiting
           and store.notification_stats().get("failed") == 0,
           f"claimed={res['claimed']} failed={res['failed']} stats={store.notification_stats()}")
    # e) 补配置后可安全领取（adopt waiting → pending → claim 恰耗 1 attempt → sent）
    try:
        moved = store.adopt_waiting_notifications(HOOK)
        claimed = store.claim_pending_notifications(limit=10, max_attempts=5)
        for r in claimed:
            store.mark_notification_sent(r["id"])
        after = {r["idempotency_key"]: r for r in store.list_notifications(limit=50)}
        record("T3|补配置后 waiting 行可安全领取（adopt 迁移 2 行、恰耗 1 attempt、送达 sent）",
               moved == 2 and len(claimed) == 2
               and all(after[k]["attempts"] == 1 and after[k]["status"] == "sent"
                       for k in ("sm11-wait:new", "sm11-wait:legacy")),
               f"moved={moved} claimed={len(claimed)} "
               f"states={[(after[k]['status'], after[k]['attempts']) for k in after]}")
    except AttributeError as ex:
        record("T3|补配置后 waiting 行可安全领取（adopt 迁移 2 行、恰耗 1 attempt、送达 sent）",
               False, f"机制未实现（{ex}）")
    store.close()


def t4_claim_generation_guard(tmp_root):
    """T4 领取代次守卫：reap→重领后，旧代次的 sent/failed 回执无法改写当前领取。"""
    from core.audit_store import AuditStore
    store, _ = fresh(tmp_root, "t4_gen")

    nid = store.enqueue_notification("sm11-gen:1", HOOK, {"request_id": "g1", "text": "t"})
    c1 = store.claim_pending_notifications(limit=5, max_attempts=5, now=time.time())
    gen1 = c1[0].get("claim_generation") if c1 else None
    record("T4|领取行携带领取代次 claim_generation（领取即发代次凭证）",
           bool(c1) and gen1 is not None,
           f"claimed_keys={sorted(c1[0].keys()) if c1 else '无'}")

    store.reap_stale_sending(stale_s=0.001, now=time.time() + 120)   # 模拟悬挂复位
    c2 = store.claim_pending_notifications(limit=5, max_attempts=5, now=time.time() + 121)
    gen2 = c2[0].get("claim_generation") if c2 else None
    record("T4|重领产生新代次（旧代次随之失效）",
           bool(c2) and gen1 is not None and gen2 is not None and gen2 != gen1,
           f"gen1={gen1} gen2={gen2}")

    try:
        stale_ok = store.mark_notification_sent(nid, generation=gen1)
        row = store.get_notification(nid)
        record("T4|旧代次 sent 回执被拒（行保持 sending、无 sent_ts）",
               stale_ok is False and row["status"] == "sending" and not row["sent_ts"],
               f"stale_ok={stale_ok} status={row['status']} sent_ts={row['sent_ts']}")
        ok = store.mark_notification_sent(nid, generation=gen2)
        record("T4|当前代次 sent 回执被接受（可信回执=只有当前领取持有者可写）",
               ok is True and store.get_notification(nid)["status"] == "sent",
               f"ok={ok} status={store.get_notification(nid)['status']}")
    except TypeError as ex:
        record("T4|旧代次 sent 回执被拒（行保持 sending、无 sent_ts）",
               False, f"机制未实现（{ex}）")
        record("T4|当前代次 sent 回执被接受（可信回执=只有当前领取持有者可写）",
               False, f"机制未实现（{ex}）")

    nid2 = store.enqueue_notification("sm11-gen:2", HOOK, {"request_id": "g2", "text": "t"})
    cf1 = store.claim_pending_notifications(limit=5, max_attempts=5, now=time.time() + 200)
    genf1 = cf1[0].get("claim_generation") if cf1 else None
    store.reap_stale_sending(stale_s=0.001, now=time.time() + 320)
    cf2 = store.claim_pending_notifications(limit=5, max_attempts=5, now=time.time() + 321)
    genf2 = cf2[0].get("claim_generation") if cf2 else None
    try:
        stale_fail = store.mark_notification_failed(nid2, "旧代次的失败", 5.0, generation=genf1)
        row2 = store.get_notification(nid2)
        record("T4|旧代次 failed 回执被拒（不覆盖当前领取的投递中状态）",
               stale_fail is False and row2["status"] == "sending",
               f"stale_fail={stale_fail} status={row2['status']}")
        okf = store.mark_notification_failed(nid2, "当前代次的失败", 0.0,
                                             max_attempts=5, generation=genf2)
        row2 = store.get_notification(nid2)
        record("T4|当前代次 failed 回执被接受（failed+退避可见）",
               okf is True and row2["status"] == "failed"
               and "当前代次的失败" in row2["last_error"],
               f"okf={okf} row={dict(row2)}")
    except TypeError as ex:
        record("T4|旧代次 failed 回执被拒（不覆盖当前领取的投递中状态）",
               False, f"机制未实现（{ex}）")
        record("T4|当前代次 failed 回执被接受（failed+退避可见）",
               False, f"机制未实现（{ex}）")

    # 旧行为守卫：缺省 generation 的 mark 仍可用（既有测试/旧调用零破坏）
    nid3 = store.enqueue_notification("sm11-gen:3", HOOK, {"request_id": "g3", "text": "t"})
    store.claim_pending_notifications(limit=5, max_attempts=5, now=time.time() + 400)
    legacy_ok = store.mark_notification_sent(nid3)
    record("T4|守卫腿：缺省 generation 的 mark_notification_sent 行为不变（既有契约零破坏）",
           legacy_ok is True and store.get_notification(nid3)["status"] == "sent",
           f"legacy_ok={legacy_ok}")
    store.close()


def t5_independent_events_and_retry_id(tmp_root):
    """T5 连续不同事件独立送达；同一事件重试保留行 id；入队携带 kind。"""
    from core.audit_store import AuditStore
    from core.notification_outbox import deliver_pending
    store, _ = fresh(tmp_root, "t5_events")

    # 守卫腿（现状兼容）：两独立事件，A 失败不影响 B；A 重试同行 id
    ga = store.enqueue_notification("sm11-t5:g-a", HOOK, {"request_id": "ga", "text": "A"})
    gb = store.enqueue_notification("sm11-t5:g-b", HOOK, {"request_id": "gb", "text": "B"})

    def sender_fail_a(url, payload):
        return payload.get("request_id") != "ga"    # 事件 A 失败、B 受理
    deliver_pending(store, sender_fail_a, batch=10, max_attempts=5, base_s=0.0)
    record("T5|连续不同事件独立送达（A 失败不影响 B 送达）",
           store.get_notification(ga)["status"] == "failed"
           and store.get_notification(gb)["status"] == "sent",
           f"a={store.get_notification(ga)['status']} b={store.get_notification(gb)['status']}")
    reca = store.claim_pending_notifications(limit=5, max_attempts=5, now=time.time() + 3600)
    if reca:
        store.mark_notification_sent(reca[0]["id"])
    retry = store.get_notification(ga)
    record("T5|同一事件重试保留行 id（attempts 累计、终态 sent）",
           bool(reca) and reca[0]["id"] == ga and retry["id"] == ga
           and retry["attempts"] == 2 and retry["status"] == "sent",
           f"claim_id={reca[0]['id'] if reca else '无'} row_id={retry['id']} "
           f"attempts={retry['attempts']} status={retry['status']}")

    # 红腿（目标契约）：入队携带 kind（事件分类）
    try:
        ka = store.enqueue_notification("sm11-t5:k", HOOK,
                                        {"request_id": "k", "text": "K"}, kind="overdue")
        record("T5|入队携带 kind='overdue'（事件分类契约 §2.2）",
               store.get_notification(ka)["kind"] == "overdue",
               f"kind={store.get_notification(ka)['kind']}")
    except TypeError as ex:
        record("T5|入队携带 kind='overdue'（事件分类契约 §2.2）",
               False, f"机制未实现（{ex}）")
    store.close()


# ── T6 子进程入口：领取→渠道受理（账本）→ mark 前杀进程 ────────────────────────
def _accept_then_die(db_path, ledger_path):
    try:
        sys.path.insert(0, str(REPO))
        from core.audit_store import AuditStore
        store = AuditStore(db_path)
        claimed = store.claim_pending_notifications(limit=1, max_attempts=5)
        if not claimed:
            os._exit(3)
        row = claimed[0]
        payload = json.loads(row.get("payload") or "{}")
        with open(ledger_path, "a", encoding="utf-8") as f:   # 渠道受理（外部事实：账本记录）
            f.write(json.dumps({"gen": 1, "payload": payload},
                               ensure_ascii=False, sort_keys=True) + "\n")
        os._exit(0)                                           # 崩溃点：受理后、mark 前
    except SystemExit:
        raise
    except Exception:
        os._exit(2)


def t6_kill_between_accept_and_mark(tmp_root):
    """T6 外部受理与本地提交间杀进程：at-least-once 投递边界实录（披露/守卫用例）。"""
    from core.audit_store import AuditStore
    store, db = fresh(tmp_root, "t6_kill")
    key = f"sm11-t6:{STAMP}"
    payload = {"request_id": f"SM11-T6-{STAMP}", "text": "值班通知（受理后崩溃重投边界）"}
    nid = store.enqueue_notification(key, HOOK, payload, request_id=payload["request_id"])
    ledger = Path(tmp_root) / f"t6_ledger_{STAMP}.jsonl"
    ledger.write_text("", encoding="utf-8")

    ctx = mp.get_context("spawn")
    p = ctx.Process(target=_accept_then_die, args=(db, str(ledger)))
    p.start()
    p.join(timeout=60)
    first = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln]
    record("T6|子进程渠道受理（账本 1 行）后进程被杀（mark 前，exit 码核对）",
           len(first) == 1 and p.exitcode == 0,
           f"ledger={len(first)} exitcode={p.exitcode}")

    store.reap_stale_sending(stale_s=0.001, now=time.time() + 200)      # 悬挂复位
    claimed2 = store.claim_pending_notifications(limit=1, max_attempts=5,
                                                 now=time.time() + 201)
    for row in claimed2:
        with open(ledger, "a", encoding="utf-8") as f:                  # 重投受理
            f.write(json.dumps({"gen": 2, "payload": json.loads(row["payload"])},
                               ensure_ascii=False, sort_keys=True) + "\n")
        store.mark_notification_sent(row["id"])

    lines = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln]
    payloads = [ln["payload"] for ln in lines]
    row = store.get_notification(nid)
    record("T6|at-least-once 边界：同一事件重投，渠道账本恰 2 次受理（不是 1 次也不是 0 次）",
           len(lines) == 2, f"deliveries={len(lines)}")
    record("T6|重投 payload 与首次逐字节相同且携带幂等键（下游按 idempotency_key 去重）",
           len(payloads) == 2 and payloads[0] == payloads[1]
           and payloads[0].get("idempotency_key") == key,
           f"payloads={json.dumps(payloads, ensure_ascii=False)[:200]}")
    record("T6|行 id 不变（同一事件重试保留 ID）、终态 sent、attempts 累计为 2",
           row["id"] == nid and row["status"] == "sent" and row["attempts"] == 2,
           f"id={row['id']} status={row['status']} attempts={row['attempts']}")
    store.close()


def t7_health_threshold(tmp_root):
    """T7 按 (kind,channel) 健康项：连败 3 次才 breached，成功清零。"""
    from core.audit_store import AuditStore
    store, _ = fresh(tmp_root, "t7_health")
    try:
        rows = [store.enqueue_notification(f"sm11-t7:{i}", HOOK,
                                           {"request_id": f"h{i}", "text": "t"},
                                           kind="overdue") for i in range(4)]
    except TypeError as ex:
        record("T7|按 (kind,channel) 健康项连败 3 次才报错（FAILURES_BEFORE_ERROR=3）",
               False, f"机制未实现（{ex}）")
        store.close()
        return
    now = time.time()

    def claim_and_fail(seq):
        c = store.claim_pending_notifications(limit=5, max_attempts=5, now=now + seq * 10)
        store.mark_notification_failed(c[0]["id"], "HTTP 500", 0.0, max_attempts=5)

    def bucket():
        h = store.notification_health()
        return [x for x in h if x.get("kind") == "overdue" and x.get("channel") == HOOK]

    claim_and_fail(0)
    claim_and_fail(1)
    b = bucket()
    record("T7|连败 2 次：未达阈值（不向管理层报错，per-row details 仍如实记录）",
           len(b) == 1 and b[0]["consecutive_failures"] == 2 and not b[0].get("breached_ts"),
           f"health={json.dumps(b, ensure_ascii=False, default=str)}")
    claim_and_fail(2)
    b = bucket()
    record("T7|连败第 3 次：达到阈值 → breached（管理层可见）",
           len(b) == 1 and b[0]["consecutive_failures"] >= 3 and bool(b[0].get("breached_ts")),
           f"health={json.dumps(b, ensure_ascii=False, default=str)}")
    c = store.claim_pending_notifications(limit=5, max_attempts=5, now=now + 100)
    if c:
        store.mark_notification_sent(c[0]["id"])
    b = bucket()
    record("T7|成功后连败计数清零（恢复即解除告警态）",
           len(b) == 1 and b[0]["consecutive_failures"] == 0,
           f"health={json.dumps(b, ensure_ascii=False, default=str)}")
    store.close()


def main():
    tmp_root = tempfile.mkdtemp(prefix="sm11_overdue_tx_")
    os.environ.update({
        "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",   # 本文件自驱动扫描，不起后台循环
        "SAFETYMIND_OUTBOX_ENABLED": "0",
        "PROMETHEUS_PORT": "0",
        "AUDIT_ENABLED": "1",
        "ESCALATION_WEBHOOK_URL": "",             # 渠道按行显式传参，不受环境影响
    })
    try:
        t6_kill_between_accept_and_mark(tmp_root)
        t1_two_process_scan(tmp_root)
        t2_injected_failure_recovery(tmp_root)
        t3_waiting_rows(tmp_root)
        t4_claim_generation_guard(tmp_root)
        t5_independent_events_and_retry_id(tmp_root)
        t7_health_threshold(tmp_root)
    finally:
        failed = RESULTS.count(False)
        total = len(RESULTS)
        print(f"\n{total - failed}/{total} PASSED")
        print(f"[SM-11 RED 环境] 临时目录（保留备查，进程退出即弃）: {tmp_root}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
