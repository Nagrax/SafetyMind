# -*- coding: utf-8 -*-
"""通知 outbox 跨进程抢领回归测试（多 worker 形态）：python tests/test_outbox_multiworker.py

背景（已复现的真实缺陷）：`claim_pending_notifications` 原实现只靠**进程内**
`threading.Lock` + "SELECT 后逐行 UPDATE"，跨进程（uvicorn --workers N）时两进程各自持锁、
WAL 下 SELECT 不阻塞写 → 同一批 pending 行被两个消费者同时领走，同一幂等键重复投递。
修复：`BEGIN IMMEDIATE`（SQLite 写锁）串行化"选出→置位" + 条件 UPDATE 校验 rowcount。

本测试用**多进程 + barrier**直接驱动两个 AuditStore 消费者（无需真起服务），
验证同一批待发行不重复领取；并用假 sender 验证幂等键只被投递一次。
退出码 0 = 全过；独立 `_test` 库，工程目录不留残留。
"""
import json
import multiprocessing as mp
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

N_ROWS = 200
N_CONSUMERS = 3

RESULTS = []
_DBS = []


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def fresh(name):
    """独立 *_test 库（与桌面端 data/audit.db 隔离）。"""
    db = f"data/{name}.db"
    for p in (db, db + "-wal", db + "-shm"):
        Path(p).unlink(missing_ok=True)
    _DBS.append(db)
    from core.audit_store import AuditStore
    return AuditStore(db), db


# ── 子进程入口（spawn：模块级函数，按引用 pickle）──────────────────────────────

def _claim_worker(db_path, barrier, q, limit):
    from core.audit_store import AuditStore
    store = AuditStore(db_path)      # 各自独立连接；先 open 再 barrier，排除初始化耗时干扰
    barrier.wait()
    got = store.claim_pending_notifications(limit=limit, max_attempts=5)
    q.put([int(r["id"]) for r in got])
    store.close()


def _deliver_worker(db_path, barrier, ledger_path, lock):
    """假 sender：把幂等键写入共享账本（文件锁保护）——同一键出现两次即重复投递。"""
    import time
    from core.audit_store import AuditStore
    from core.notification_outbox import deliver_pending
    store = AuditStore(db_path)
    barrier.wait()

    def sender(url, payload):
        time.sleep(0.002)                     # 模拟网络耗时，拉长两个消费者重叠窗口
        with lock:
            with open(ledger_path, "a", encoding="utf-8") as f:
                f.write(payload.get("key", "") + "\n")
        return True

    deliver_pending(store, sender, batch=N_ROWS, base_s=3600.0)
    store.close()


def test_multiprocess_claim_exclusive():
    store, db = fresh("_outbox_mw_test_claim")
    for i in range(N_ROWS):
        store.enqueue_notification(f"mw:{i}", "http://hook.local/x", {"i": i})
    before = store.notification_stats()
    store.close()

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(N_CONSUMERS)
    q = ctx.Queue()
    ps = [ctx.Process(target=_claim_worker, args=(db, barrier, q, N_ROWS))
          for _ in range(N_CONSUMERS)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=120)

    ids = []
    while not q.empty():
        ids.extend(q.get())

    from core.audit_store import AuditStore
    check = AuditStore(db)
    stats = check.notification_stats()
    rows = [r for r in check.list_notifications(limit=N_ROWS + 50)]
    check.close()

    record(f"{N_CONSUMERS} 进程同批抢领：总领取数 == 预置行数（全部领走且不重复）",
           before.get("pending") == N_ROWS and len(ids) == N_ROWS and len(set(ids)) == N_ROWS,
           f"before={before.get('pending')} claimed_total={len(ids)} unique={len(set(ids))}")
    record("跨进程抢领不产生重复 id（修复前此处会是 200+ 重复）",
           len(ids) - len(set(ids)) == 0, f"duplicated={len(ids) - len(set(ids))}")
    record("领取后每行 attempts 恰为 1、状态 sending（未被同一键重复领取）",
           all(r["attempts"] == 1 and r["status"] == "sending" for r in rows)
           and stats.get("sending") == N_ROWS and stats.get("pending") == 0,
           f"stats={stats}")


def test_multiprocess_delivery_once():
    """两个消费者各跑一次 deliver_pending（假 sender 写共享账本）：每个幂等键只投递一次。"""
    store, db = fresh("_outbox_mw_test_deliver")
    keys = [f"once:{i}" for i in range(40)]
    for i, k in enumerate(keys):
        store.enqueue_notification(k, "http://hook.local/x", {"key": k, "i": i})
    store.close()

    ledger = Path("data/_outbox_mw_test_ledger.jsonl")
    ledger.write_text("", encoding="utf-8")

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    lock = ctx.Lock()
    ps = [ctx.Process(target=_deliver_worker, args=(db, barrier, str(ledger), lock))
          for _ in range(2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=120)

    lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln]
    dup = len(lines) - len(set(lines))
    record("两消费者投递：每个幂等键只送达一次（账本无重复键）",
           len(lines) == len(keys) and dup == 0, f"delivered={len(lines)} dup={dup}")
    ledger.unlink(missing_ok=True)

    from core.audit_store import AuditStore
    check = AuditStore(db)
    stats = check.notification_stats()
    check.close()
    record("40 条全部 sent 且无一条被领取两次（attempts 全为 1）",
           stats.get("sent") == len(keys) and stats.get("sending") == 0,
           str(stats))


def test_reap_and_reclaim_still_works():
    """既有语义不因修复回退：悬挂 sending 复位后可再领；sent 行不会被再领。"""
    store, _ = fresh("_outbox_mw_test_reap")
    sid = store.enqueue_notification("reap:1", "http://hook.local/a", {"k": "reap"})
    first = store.claim_pending_notifications(limit=5, max_attempts=5)
    record("首次领取 1 条且转 sending", len(first) == 1 and first[0]["attempts"] == 1)
    record("已有 sending 行不会被再次领取", store.claim_pending_notifications(limit=5) == [])
    store.mark_notification_sent(sid)
    record("sent 行不会被再次领取",
           store.claim_pending_notifications(limit=5, now=9e9) == []
           and store.get_notification(sid)["status"] == "sent")
    store.close()


def test_begin_immediate_does_not_corrupt_other_writes():
    """修复引入的 BEGIN IMMEDIATE 不影响其它写路径（建单+同事务通知、审计写入仍正常）。"""
    store, _ = fresh("_outbox_mw_test_mix")
    tid = store.create_ticket("混合写入工单", type_="hazard", source_request_id="mix-rid")
    store.audit_request("mix-rid", "u", "c", "消息", "other", "low", False, 1.0)
    store.claim_pending_notifications(limit=5, max_attempts=5)
    store.transition_ticket(tid, "discuss", actor="admin")
    ticket = store.get_ticket(tid)
    record("修复后建单/审计/流转/领取混跑均正常（无事务残留异常）",
           ticket is not None and ticket.get("stage") == "discuss"
           and len(store.list_audit(limit=5)) >= 1,
           json.dumps(ticket, ensure_ascii=False, default=str)[:160])
    store.close()


def main():
    os.environ.setdefault("AUDIT_DB_PATH", "data/_outbox_mw_test_claim.db")
    test_reap_and_reclaim_still_works()
    test_begin_immediate_does_not_corrupt_other_writes()
    test_multiprocess_claim_exclusive()
    test_multiprocess_delivery_once()

    for db in _DBS:
        for p in (db, db + "-wal", db + "-shm"):
            Path(p).unlink(missing_ok=True)
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
