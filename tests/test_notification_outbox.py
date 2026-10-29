# -*- coding: utf-8 -*-
# 通知 outbox（持久待发送记录 + 送达/确认状态）单元测试：python tests/test_notification_outbox.py
# 覆盖：幂等键去重（重复投递不新增）、工单与待发送记录同事务落库（含非法 payload 落库前拒绝）、
#       并发领取原子且不重复（threading 两/三线程同一库）、失败→退避→到期再领→尝试上限停在 failed、
#       sent/ack/replay 状态机、sender 抛异常/返回 False（模拟断网与业务错误码）→ failed 且 last_error 可见、
#       进程被终止导致 sending 悬挂 → reap_stale_sending 复位后可重投、
#       未配置渠道（channel=''）永不被领取且 stats 可见 pending、payload JSON 损坏记失败不静默丢弃、
#       管理/接收端点（GET /notifications、POST replay/ack、/api/python 别名、未初始化 503）。
# 全程使用假 sender，不发任何真实网络请求。退出码 0 = 全过。
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []
_DBS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def fresh(name):
    """独立 *_test 库（与桌面端 data/audit.db 隔离），返回 (store, db_path)。"""
    db = f"data/{name}.db"
    for p in (db, db + "-wal", db + "-shm"):
        Path(p).unlink(missing_ok=True)
    _DBS.append(db)
    from core.audit_store import AuditStore
    return AuditStore(db), db


def test_dedupe_and_same_txn():
    from core.notification_outbox import backoff_seconds
    store, _ = fresh("_outbox_test_main")

    k = "upgrade:1:webhook"
    i1 = store.enqueue_notification(k, "http://hook.local/a", {"request_id": "r1", "text": "t"})
    i2 = store.enqueue_notification(k, "http://hook.local/a", {"request_id": "r1", "text": "t"})
    stats = store.notification_stats()
    record("幂等键去重(同键两次入队只一行且返回既有 id)",
           i1 == i2 and stats.get("pending") == 1, f"i1={i1} i2={i2} stats={stats}")

    tid = store.create_ticket(
        "应急升级: 泄漏扩散", type_="emergency", priority="critical", source_request_id="r2",
        brief={"intent": "emergency_response", "urgency": "critical", "entities": {},
               "message_digest": "泄漏扩散", "routing_reason": "CRITICAL", "request_id": "r2"},
        notification={"idempotency_key": "upgrade:9:webhook", "channel": "http://hook.local/a",
                      "payload": {"request_id": "r2", "text": "x"}, "request_id": "r2",
                      "upgrade_id": 9})
    rows = {r["idempotency_key"]: r for r in store.list_notifications()}
    row = rows["upgrade:9:webhook"]
    record("建单与待发送记录同事务落库(工单行+outbox 行同时存在且关联 ticket_id)",
           store.get_ticket(tid) is not None and row["ticket_id"] == tid
           and row["status"] == "pending" and row["attempts"] == 0,
           json.dumps(row, ensure_ascii=False, default=str))

    n_before = len(store.list_notifications())
    store.create_ticket("普通隐患工单", type_="hazard")
    record("create_ticket 不传 notification 时旧行为不变（不产生 outbox 行）",
           len(store.list_notifications()) == n_before)

    raised = False
    try:
        store.create_ticket("坏通知工单", notification={
            "idempotency_key": "bad:1", "channel": "", "payload": {"x": object()}})
    except Exception:
        raised = True
    keys = [r["idempotency_key"] for r in store.list_notifications()]
    titles = [t["title"] for t in store.list_tickets(limit=200)]
    record("非法 payload 落库前拒绝（不留半成品工单/outbox 行）",
           raised and "bad:1" not in keys and not any("坏通知" in t for t in titles))

    record("指数退避计算与上限封顶",
           backoff_seconds(1) == 5.0 and backoff_seconds(2) == 10.0
           and backoff_seconds(3) == 20.0 and backoff_seconds(7) == 300.0,
           f"{[backoff_seconds(n) for n in (1, 2, 3, 7)]}")
    store.close()


def test_concurrent_claim():
    store, _ = fresh("_outbox_test_conc")
    for i in range(6):
        store.enqueue_notification(f"bulk:{i}", "http://hook.local/a", {"i": i})
    pending_before = store.notification_stats().get("pending")

    claimed, lock = [], threading.Lock()

    def worker():
        got = store.claim_pending_notifications(limit=3, max_attempts=5)
        with lock:
            claimed.extend(got)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = [r["id"] for r in claimed]
    record("并发领取原子：两个/三个线程不重复领取同一行、总数不超行数",
           pending_before == 6 and len(ids) == 6 and len(set(ids)) == 6
           and store.notification_stats().get("sending") == 6
           and store.notification_stats().get("pending") == 0,
           f"pending_before={pending_before} claimed={len(ids)} unique={len(set(ids))}")
    record("领取行含 idempotency_key/channel/payload/attempts 且 attempts 已 +1",
           all(r["attempts"] == 1 and r["idempotency_key"] and r["channel"]
               and isinstance(r["payload"], str) for r in claimed))
    store.close()


def test_retry_backoff_and_cap():
    store, _ = fresh("_outbox_test_retry")
    sid = store.enqueue_notification("retry:1", "http://hook.local/a", {"n": 1})

    r1 = store.claim_pending_notifications(limit=5, max_attempts=3, now=time.time())
    record("首次领取 attempts=1 且状态转 sending",
           len(r1) == 1 and r1[0]["id"] == sid and r1[0]["attempts"] == 1
           and store.get_notification(sid)["status"] == "sending")

    store.mark_notification_failed(sid, "HTTP 500", 30.0)
    n = store.get_notification(sid)
    record("失败→failed 且 last_error/退避时间可见",
           n["status"] == "failed" and "HTTP 500" in n["last_error"]
           and abs(n["next_attempt_ts"] - (time.time() + 30.0)) < 3.0,
           json.dumps(n, ensure_ascii=False, default=str))

    record("退避未到期不再被领取",
           store.claim_pending_notifications(limit=5, max_attempts=3, now=time.time()) == [])

    r2 = store.claim_pending_notifications(limit=5, max_attempts=3, now=time.time() + 31)
    record("退避到期后可再次领取 attempts=2", len(r2) == 1 and r2[0]["id"] == sid and r2[0]["attempts"] == 2)
    store.mark_notification_failed(sid, "超时", 5.0)

    r3 = store.claim_pending_notifications(limit=5, max_attempts=3, now=time.time() + 3600)
    record("第三次领取 attempts=3（达 max_attempts）", len(r3) == 1 and r3[0]["attempts"] == 3)
    store.mark_notification_failed(sid, "超时", 5.0)

    record("尝试达上限后停在 failed 供人工重放且不再被领取",
           store.get_notification(sid)["status"] == "failed"
           and store.claim_pending_notifications(limit=5, max_attempts=3, now=time.time() + 99999) == []
           and store.notification_stats().get("failed") == 1)
    store.close()


def test_sent_ack_replay_state_machine():
    store, _ = fresh("_outbox_test_state")
    sid = store.enqueue_notification("state:1", "http://hook.local/a", {"request_id": "s1", "text": "t"})
    store.claim_pending_notifications(limit=5, max_attempts=5)
    record("领取后 status=sending（未送达不得写成 sent）", store.get_notification(sid)["status"] == "sending")

    record("mark_notification_sent 置 sent+sent_ts",
           store.mark_notification_sent(sid) is True
           and store.get_notification(sid)["status"] == "sent"
           and store.get_notification(sid)["sent_ts"] > 0)
    record("sent 与 ack 是两件独立事实（sent 不等于人工已确认）",
           store.get_notification(sid)["acked_ts"] is None
           and [r["id"] for r in store.list_notifications(status="sent")] == [sid])
    record("ack_notification 置 ack+acked_ts（接收方/人工确认）",
           store.ack_notification(sid, actor="值班员") is True
           and store.get_notification(sid)["status"] == "ack"
           and store.get_notification(sid)["acked_ts"] > 0
           and store.notification_stats().get("ack") == 1)

    record("replay_notification 复位为 pending 且清空 attempts/退避/错误",
           store.replay_notification(sid) is True
           and store.get_notification(sid)["status"] == "pending"
           and store.get_notification(sid)["attempts"] == 0
           and store.get_notification(sid)["next_attempt_ts"] == 0
           and store.get_notification(sid)["last_error"] == "")
    record("人工重放后可再次被领取（重新投递）",
           len(store.claim_pending_notifications(limit=5, max_attempts=5)) == 1)

    record("不存在 id 的 sent/ack/replay 返回 False",
           store.mark_notification_sent(999999) is False
           and store.ack_notification(999999) is False
           and store.replay_notification(999999) is False)
    store.close()


def test_deliver_pending_with_fake_sender():
    from core.notification_outbox import deliver_pending

    store, _ = fresh("_outbox_test_deliver")
    ok_id = store.enqueue_notification("d1", "http://hook.local/ok", {"request_id": "a", "text": "t"})
    boom_id = store.enqueue_notification("d2", "http://hook.local/boom", {"request_id": "b", "text": "t"})
    false_id = store.enqueue_notification("d3", "http://hook.local/false", {"request_id": "c", "text": "t"})

    def sender(url, payload):
        if url.endswith("boom"):
            raise RuntimeError("connection refused")   # 模拟断网
        if url.endswith("false"):
            return False                               # 模拟 500 / 200 但业务错误码
        return True

    res = deliver_pending(store, sender, batch=10, base_s=3600.0)
    record("deliver_pending 汇总结构（claimed/sent/failed/reaped/details）",
           res["claimed"] == 3 and res["sent"] == 1 and res["failed"] == 2 and res["reaped"] == 0
           and isinstance(res["details"], list) and len(res["details"]) == 3,
           json.dumps(res, ensure_ascii=False, default=str))
    record("投递成功→sent（渠道已受理）", store.get_notification(ok_id)["status"] == "sent")
    record("sender 抛异常（断网）→failed 且 last_error 可见",
           store.get_notification(boom_id)["status"] == "failed"
           and "connection refused" in store.get_notification(boom_id)["last_error"],
           store.get_notification(boom_id)["last_error"])
    record("sender 返回 False（渠道拒绝）→failed 且 last_error 可见",
           store.get_notification(false_id)["status"] == "failed"
           and bool(store.get_notification(false_id)["last_error"]),
           store.get_notification(false_id)["last_error"])

    # 未配置渠道（channel=''）永不被领取，但 pending 与原因可见
    none_id = store.enqueue_notification("d4", "", {"request_id": "d", "text": "t"})
    res2 = deliver_pending(store, sender, batch=10, base_s=3600.0)
    n = store.get_notification(none_id)
    record("未配置渠道：入队但永不被领取(pending+last_error='未配置通知渠道')",
           res2["claimed"] == 0 and n["status"] == "pending"
           and n["last_error"] == "未配置通知渠道" and n["channel_configured"] is False
           and store.notification_stats().get("pending") == 1,
           json.dumps(n, ensure_ascii=False, default=str))

    # payload JSON 损坏：记失败不静默丢弃
    bad_id = store.enqueue_notification("d5", "http://hook.local/ok", {"request_id": "e", "text": "t"})
    con = sqlite3.connect(store._path)
    con.execute("UPDATE notification_outbox SET payload=? WHERE id=?", ("{broken", bad_id))
    con.commit()
    con.close()
    res3 = deliver_pending(store, sender, batch=10, base_s=3600.0)
    nb = store.get_notification(bad_id)
    record("payload JSON 损坏→记失败不静默丢弃",
           res3["claimed"] == 1 and res3["failed"] == 1 and nb["status"] == "failed"
           and "JSON" in nb["last_error"],
           json.dumps(nb, ensure_ascii=False, default=str))

    # 渠道 URL 不下发（list 只回是否已配置）
    dumped = json.dumps(store.list_notifications(), ensure_ascii=False)
    record("对外列表不下发完整渠道 URL / payload 原文",
           "hook.local" not in dumped and '"payload"' not in dumped)

    record("stats 覆盖各状态计数与 failed 条数",
           set(store.notification_stats()) >= {"pending", "sending", "sent", "ack", "failed"}
           and store.notification_stats()["failed"] >= 2,
           json.dumps(store.notification_stats(), ensure_ascii=False))
    store.close()


def test_reap_stale_sending():
    store, _ = fresh("_outbox_test_reap")
    sid = store.enqueue_notification("reap:1", "http://hook.local/a", {"request_id": "z", "text": "t"})
    store.claim_pending_notifications(limit=5, max_attempts=5)   # 领取后不 mark = 进程被终止

    record("新领取的 sending 行不会被误复位",
           store.reap_stale_sending(stale_s=120.0, now=time.time()) == 0)

    reaped = store.reap_stale_sending(stale_s=0.001, now=time.time() + 60)
    n = store.get_notification(sid)
    record("悬挂 sending 超时后复位为 pending（attempts 保留）",
           reaped == 1 and n["status"] == "pending" and n["attempts"] == 1,
           json.dumps(n, ensure_ascii=False, default=str))
    again = store.claim_pending_notifications(limit=5, max_attempts=5, now=time.time() + 60)
    record("复位后可重新投递（attempts 继续累计）",
           len(again) == 1 and again[0]["attempts"] == 2)
    store.close()


def test_endpoints():
    os.environ["API_HOST"] = "127.0.0.1"   # 本地形态：管理端免令牌（部署形态鉴权由既有测试覆盖）
    store, _ = fresh("_outbox_test_api")
    sid = store.enqueue_notification("api:1", "http://hook.local/a", {"request_id": "api", "text": "t"})

    import api.main as m
    m._audit = store
    from fastapi.testclient import TestClient
    client = TestClient(m.app)   # 不进 lifespan：仅验证端点接线与别名

    r = client.get("/notifications")
    body = r.json() if r.status_code == 200 else {}
    record("GET /notifications 返回列表 + stats",
           r.status_code == 200 and "notifications" in body and "stats" in body
           and any(n["idempotency_key"] == "api:1" for n in body["notifications"]),
           f"code={r.status_code} body={r.text[:160]}")

    r = client.post(f"/notifications/{sid}/replay")
    record("POST /notifications/{id}/replay", r.status_code == 200 and r.json().get("ok") is True,
           f"code={r.status_code} body={r.text[:120]}")
    r = client.post(f"/notifications/{sid}/ack")
    record("POST /notifications/{id}/ack", r.status_code == 200 and r.json().get("ok") is True,
           f"code={r.status_code} body={r.text[:120]}")
    r = client.post("/notifications/999999/replay")
    record("POST replay 未知 id 回 ok=false", r.status_code == 200 and r.json().get("ok") is False)

    r = client.get("/api/python/notifications")
    record("GET /api/python/notifications 别名", r.status_code == 200, f"code={r.status_code}")

    m._audit = None
    r = client.get("/notifications")
    record("未初始化 _audit 回 503", r.status_code == 503, f"code={r.status_code}")
    store.close()


def main():
    os.environ["AUDIT_DB_PATH"] = "data/_outbox_test_main.db"
    test_dedupe_and_same_txn()
    test_concurrent_claim()
    test_retry_backoff_and_cap()
    test_sent_ack_replay_state_machine()
    test_deliver_pending_with_fake_sender()
    test_reap_stale_sending()
    test_endpoints()

    for db in _DBS:                       # 工程目录不留残留
        for p in (db, db + "-wal", db + "-shm"):
            Path(p).unlink(missing_ok=True)
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
