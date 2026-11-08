# -*- coding: utf-8 -*-
"""SM-40 只读 watchdog 回归测试：python tests/test_watchdog.py

覆盖（对应任务卡 SM-40 三条验收，决策规则 scratch/sm-loop/handoff/SM-40/run-wf/decision.md）：
  A/T1+T9  首扫恰一次新告警（9 事实码 10 事件），事件带 WD-EV-* ID/source_ts/责任范围/建议，
           二扫状态未变零新事件（静默）；扫描对八张业务表零漂移（只写自有 watchdog 两表）。
  B/T2     重启（全新 Watchdog 实例、同库）状态未变保持静默。
  C/T3     两个调度者（两个独立 AuditStore 连接 + 线程 barrier 并行 scan）恰一次新告警。
  A2/T4    恢复后恰一次 recovered 事件，再次扫描静默。
  D/T5     存储整体不可用 → status=unavailable、零写入、零异常外抛；存储恢复后不重复
           告警、不虚假恢复（fail-closed）。
  E/T6     SOP 治理层缺失（ledger/manager 均不可用）→ WD-SOP-UNAVAILABLE 持久事实，
           配置缺失不静默绿。
  F/T8     巡检主体（X-Watchdog-Token）：GET /watchdog 200；全部管理写端点（工单流转/
           改字段/通知重放/SOP 发布）401 且业务表零漂移；管理员 token 正向对照 200。

模拟渠道约束：全程不配置 ESCALATION_WEBHOOK_URL，通知行用空渠道（waiting）或
claim→mark_notification_failed 注入失败，绝不触发真实值班消息。
退出码 0 = 全过；独立 *_test 库，工程目录不留残留。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = Path(tempfile.mkdtemp(prefix="sm40_wd_"))
_DBS = []
RESULTS = []

# 环境钉死必须先于 api.main 导入（load_dotenv 不覆盖已显式设置的变量）。
os.environ.update({
    "SAFETYMIND_EMBEDDING": "ngram",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    "ANTHROPIC_API_KEY": "dummy",
    "PROMETHEUS_PORT": "0",
    "SAFETYMIND_DEPLOYED": "1",
    "ADMIN_TOKEN": "sm40-admin-tok",
    "SAFETYMIND_WATCHDOG_TOKEN": "sm40-wd-tok",
    "SAFETYMIND_SKILL_EVO_DIR": str(TMP / "evo"),
    "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
    "SAFETYMIND_OUTBOX_ENABLED": "0",
    "SAFETYMIND_WATCHDOG_ENABLED": "0",   # 端点测试不进 lifespan；进程内不另起后台循环
    "AUDIT_ENABLED": "1",
    "ESCALATION_WEBHOOK_URL": "",          # 模拟渠道约束：显式置空，绝不触达真实值班
})

# 八张业务表（巡检只读红线快照面）；watchdog 自有表不计入（其写入是合法簿记）。
BUSINESS_TABLES = ["audit_log", "upgrade_events", "tickets", "hazard_observations",
                   "hazard_suggestions", "ticket_timeline", "notification_outbox",
                   "notification_health"]


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def fresh(name):
    db = str(TMP / f"{name}.db")
    for p in (db, db + "-wal", db + "-shm"):
        Path(p).unlink(missing_ok=True)
    _DBS.append(db)
    from core.audit_store import AuditStore
    return AuditStore(db), db


def db_snapshot(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {t: json.dumps([dict(r) for r in con.execute(f"SELECT * FROM {t}")],
                              sort_keys=True, ensure_ascii=False, default=str)
                for t in BUSINESS_TABLES}
    finally:
        con.close()


def db_delta(before, after):
    return sorted(t for t in BUSINESS_TABLES if before[t] != after[t])


def make_watchdog(audit, db, **kw):
    """标准探针 Watchdog：stuck_s=-1（全部 open 单视为卡单，确定性）；SOP 三件套按需。"""
    from core.watchdog import Watchdog
    return Watchdog(audit, stuck_s=kw.pop("stuck_s", -1.0), **kw)


def seed_sop_drift():
    """版本漂移夹具：发布指针非空 + 全新生效视图未应用（真实形态：指针动了而同步失败）。"""
    from core.skill_loader import SkillManager, SkillPublicationLedger
    root = TMP / "skills_drift"
    root.mkdir(parents=True, exist_ok=True)
    ledger = SkillPublicationLedger(skills_root=str(root), data_dir=str(root))
    ledger.register_baseline()
    mgr = SkillManager(root_dir=str(root), max_prompt_chars=10 ** 9)   # 未 apply_published
    return ledger, mgr


def seed_evo_pending():
    from core.skill_evolution import SkillEvolution
    evo = SkillEvolution(skills_root=str(TMP / "skills_evo"))
    evo.append_candidate({"title": "巡检夹具候选", "instruction": "无",
                          "when_to_use": "无", "keywords": ["巡检"]})
    return evo


def seed_conditions(audit):
    """种齐五类事实：waiting、failed、failed_exhausted、渠道 breached、逾期单、普通单。
    返回 (overdue_tid, stuck_tid, exhausted_nid, breached_health)。模拟渠道约束：
    失败经 mark_notification_failed 注入，绝不发起真实投递。"""
    audit.enqueue_notification("sm40:waiting", "", {"k": "w"})            # 空渠道 → waiting
    audit.enqueue_notification("sm40:failed", "http://hook.local/f", {"k": "f"})
    exh_nid = audit.enqueue_notification("sm40:exh", "http://hook.local/x", {"k": "e"})
    c = audit.claim_pending_notifications(limit=1, max_attempts=5)[0]      # id ASC → failed 行
    audit.mark_notification_failed(c["id"], "SM-40 模拟渠道不可达", 3600.0,
                                   max_attempts=5, generation=c["claim_generation"])
    c2 = audit.claim_pending_notifications(limit=1, max_attempts=5)[0]     # exh 行 → 耗尽终态
    audit.mark_notification_failed(c2["id"], "SM-40 模拟渠道不可达", 3600.0,
                                   max_attempts=1, generation=c2["claim_generation"])
    for _ in range(2):   # 渠道健康桶连败达 FAILURES_BEFORE_ERROR=3 → breached（幂等注入）
        audit.mark_notification_failed(exh_nid, "SM-40 模拟渠道不可达", 3600.0, max_attempts=1)
    health = [h for h in audit.notification_health() if h.get("breached_ts")]
    overdue_tid = audit.create_ticket("SM-40 逾期夹具", type_="hazard", due_ts=time.time() - 3600)
    stuck_tid = audit.create_ticket("SM-40 卡单夹具", type_="hazard")
    return overdue_tid, stuck_tid, exh_nid, health


# ── A：首扫恰一次 + 事实结构 + 只读零漂移 + 恢复 ─────────────────────────────
def section_a():
    audit, db = fresh("sm40_wd_a")
    ledger, mgr = seed_sop_drift()
    evo = seed_evo_pending()
    overdue_tid, stuck_tid, _exh, _health = seed_conditions(audit)

    before = db_snapshot(db)
    wd = make_watchdog(audit, db, skill_ledger=ledger, skill_manager=mgr, skill_evolution=evo)
    rep1 = wd.scan()
    after = db_snapshot(db)

    expected = {
        ("WD-NOTIFY-WAITING", "waiting"),
        ("WD-NOTIFY-FAILED", "failed"),
        ("WD-NOTIFY-EXHAUSTED", "exhausted"),
        ("WD-NOTIFY-CHANNEL", "notify:http://hook.local/x"),
        ("WD-TICKET-OVERDUE", f"ticket:{overdue_tid}"),
        ("WD-TICKET-STUCK", f"ticket:{overdue_tid}"),
        ("WD-TICKET-STUCK", f"ticket:{stuck_tid}"),
        ("WD-SKILL-CANDIDATES", "pending"),
        ("WD-SOP-DRIFT", "pointer"),
    }
    got_alerts = {(e["fact_code"], e["key"]) for e in rep1["events"]
                  if e.get("kind") == "alert"}
    record("A1|首扫：9 事实码 10 键全部恰一次 alert（等待/失败/耗尽/渠道/逾期/卡单/审批/漂移）",
           rep1["status"] == "ok" and got_alerts == expected,
           f"status={rep1['status']} 缺失={sorted(expected - got_alerts)} "
           f"多出={sorted(got_alerts - expected)} events={rep1['events']}")

    ev = rep1["events"][0] if rep1["events"] else {}
    record("A2|事件结构：WD-EV-* ID + source_ts/event_ts + detail（事件 ID 稳定可追溯）",
           bool(rep1["events"]) and str(ev.get("event_id", "")).startswith("WD-EV-")
           and ev.get("source_ts", 0) > 0 and ev.get("event_ts", 0) >= ev.get("source_ts", 1)
           and str(ev.get("detail", "")) != "",
           f"ev={ev}")

    facts = {(f["fact_code"], f["key"]): f for f in rep1["facts"]}
    record("A3|事实结构：每事实带 owner_scope（责任范围）+ suggestion（处理建议），非空不重复",
           all(f.get("owner_scope") and f.get("suggestion") for f in facts.values())
           and len({f["owner_scope"] for f in facts.values()}) >= 3,
           f"owners={sorted({f['owner_scope'] for f in facts.values()})}")

    record("A4|只读红线：一次扫描八张业务表零漂移（只写自有 watchdog_state/events）",
           db_delta(before, after) == [],
           f"delta={db_delta(before, after)}")
    st = audit.watchdog_state_all()
    record("A5|状态确已持久化（A4 非空跑）且 since_ts 有值",
           len(st) == len(expected) and all(r["since_ts"] > 0 for r in st),
           f"state_rows={len(st)}")

    rep2 = wd.scan()
    record("A6|状态未变 → 二扫零新事件（静默）",
           rep2["status"] == "ok" and rep2["events"] == [],
           f"events={rep2['events']}")

    # T4 恢复：补配置 → waiting 恢复 → 恰一次 recovered
    audit.adopt_waiting_notifications("http://hook.local/x")
    rep3 = wd.scan()
    rec_events = [e for e in rep3["events"] if e.get("kind") == "recovered"]
    record("A7|恢复后恰一次 recovered 事件（WD-NOTIFY-WAITING）",
           len(rec_events) == 1 and rec_events[0]["fact_code"] == "WD-NOTIFY-WAITING",
           f"recovered={rec_events}")
    rep4 = wd.scan()
    record("A8|恢复后再扫零新事件（恢复事件也去重）",
           rep4["events"] == [], f"events={rep4['events']}")
    audit.close()
    return db


# ── B：重启静默 ───────────────────────────────────────────────────────────────
def section_b():
    audit, db = fresh("sm40_wd_b")
    audit.enqueue_notification("sm40:bw", "", {"k": "w"})
    wd1 = make_watchdog(audit, db)
    r1 = wd1.scan()
    audit.close()
    b_alerts = {(e["fact_code"], e["key"]) for e in r1["events"] if e["kind"] == "alert"}
    record("B1|重启前首扫 waiting 事实在告警集中（本段未接 SOP 三件套，"
           "WD-SOP-UNAVAILABLE 同时告警属设计预期）",
           ("WD-NOTIFY-WAITING", "waiting") in b_alerts, str(sorted(b_alerts)))

    audit2 = None
    from core.audit_store import AuditStore
    audit2 = AuditStore(db)
    wd2 = make_watchdog(audit2, db)          # 全新实例（模拟进程重启，状态只来自库）
    r2 = wd2.scan()
    record("B2|重启后状态未变 → 零新事件（告警状态从库恢复，不重复告警）",
           r2["status"] == "ok" and r2["events"] == [], f"events={r2['events']}")
    audit2.close()


# ── C：双调度者并行恰一次 ────────────────────────────────────────────────────
def section_c():
    from core.audit_store import AuditStore
    from core.watchdog import Watchdog
    _audit, db = fresh("sm40_wd_c")
    seed = AuditStore(db)
    seed.enqueue_notification("sm40:cw", "", {"k": "w"})
    seed.close()

    ctx = {"events": [[], []], "errs": [None, None]}
    barrier = threading.Barrier(2)

    def worker(i):
        try:
            a = AuditStore(db)
            w = Watchdog(a, stuck_s=-1.0)
            barrier.wait(timeout=30)
            ctx["events"][i] = w.scan()["events"]
            a.close()
        except Exception as ex:                       # 线程内异常必须显式判负，不得静默吞
            ctx["errs"][i] = f"{type(ex).__name__}: {ex}"
            try:
                barrier.wait(timeout=5)
            except Exception:
                pass

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    record("C1|双调度者并行扫描零异常（BEGIN IMMEDIATE 不互踩）",
           ctx["errs"] == [None, None], f"errs={ctx['errs']}")

    all_alerts = [(e["fact_code"], e["key"]) for evs in ctx["events"] for e in evs
                  if e.get("kind") == "alert"]
    distinct = set(all_alerts)
    record("C2|两调度者并行 → 每个事实恰一次新告警（无重复），且 waiting 在告警集中"
           "（写锁内判态，串行化去重；未接 SOP 三件套 → WD-SOP-UNAVAILABLE 同样恰一次）",
           len(all_alerts) == len(distinct) and ("WD-NOTIFY-WAITING", "waiting") in distinct
           and ("WD-SOP-UNAVAILABLE", "ledger") in distinct,
           f"alerts={all_alerts}")

    check = AuditStore(db)
    w3 = Watchdog(check, stuck_s=-1.0)
    r3 = w3.scan()
    check.close()
    record("C3|并行之后三扫零新事件（与单调度者同一静默语义）",
           r3["events"] == [], f"events={r3['events']}")


# ── D：存储失效 fail-closed + 恢复不误报（故障/恢复演练核心序列）─────────────
class _BrokenStore:
    """存储不可用探针：巡检用到的每个方法都抛（模拟库文件损坏/锁死）。"""
    def __getattr__(self, name):
        raise RuntimeError("SM-40 演练：存储不可用")


def section_d():
    audit, db = fresh("sm40_wd_d")
    audit.enqueue_notification("sm40:dw", "", {"k": "w"})
    wd = make_watchdog(audit, db)
    r1 = wd.scan()
    alert_set1 = {(e["fact_code"], e["key"]) for e in r1["events"] if e["kind"] == "alert"}
    ev_count_after_alert = len(audit.watchdog_events_recent(limit=100))

    # 白盒故障注入面（与 SM-10 _enqueue_locked 注入同款实例属性替换）：
    # 巡检只经 self._audit 触达存储，替换它即模拟库文件损坏/锁死。
    wd._audit = _BrokenStore()
    r2 = wd.scan()
    record("D1|存储不可用 → status=unavailable，不外抛异常",
           r2["status"] == "unavailable" and r2["events"] == [],
           f"status={r2['status']} events={r2['events']}")
    record("D2|失效期间零写入（不误报恢复也不重复告警，fail-closed）",
           len(audit.watchdog_events_recent(limit=100)) == ev_count_after_alert,
           f"events_now={len(audit.watchdog_events_recent(limit=100))} "
           f"before={ev_count_after_alert}")
    record("D3|失效可追溯：unknown 事实含错误串与来源时间（来源时间>0 即可核对时序）",
           any(str(e.get("error", "")) and e.get("source_ts", 0) > 0
               for e in (r2.get("unknown") or [])) and (r2.get("unknown") or []) != [],
           f"unknown={r2.get('unknown')}")

    wd._audit = audit
    r3 = wd.scan()
    record("D4|存储恢复 → 续扫正常且不重复告警（状态在库，恰一次语义跨失效保持）",
           r3["status"] == "ok" and r3["events"] == [], f"events={r3['events']}")

    audit.adopt_waiting_notifications("http://hook.local/x")
    r4 = wd.scan()
    rec = [e for e in r4["events"] if e.get("kind") == "recovered"
           and e.get("fact_code") == "WD-NOTIFY-WAITING"]
    record("D5|失效窗口内已修复 → 恢复后恰一次 recovered（不虚假恢复、不吞恢复）",
           len(rec) == 1 and rec[0]["fact_code"] == "WD-NOTIFY-WAITING",
           f"recovered={rec}")
    record("D6|演练序列自洽：首轮 waiting 确在告警集（D1-D5 对照基准）",
           ("WD-NOTIFY-WAITING", "waiting") in alert_set1, f"alerts={sorted(alert_set1)}")
    audit.close()


# ── E：SOP 治理缺失报未知/不可用 ─────────────────────────────────────────────
def section_e():
    audit, db = fresh("sm40_wd_e")
    wd = make_watchdog(audit, db, skill_ledger=None, skill_manager=None)
    r1 = wd.scan()
    got = {(f["fact_code"], f["key"]) for f in r1["facts"]}
    record("E1|SOP 治理层缺失 → WD-SOP-UNAVAILABLE 持久事实（配置缺失不静默绿）",
           ("WD-SOP-UNAVAILABLE", "ledger") in got, f"facts={sorted(got)}")
    f = next((f for f in r1["facts"] if f["fact_code"] == "WD-SOP-UNAVAILABLE"), {})
    record("E2|不可用事实同样带责任范围与处理建议",
           bool(f.get("owner_scope")) and bool(f.get("suggestion")), f"f={f}")
    r2 = wd.scan()
    record("E3|不可用事实同样去重（不重复刷屏）",
           r2["events"] == [], f"events={r2['events']}")
    audit.close()


# ── F：巡检主体 API 面 ───────────────────────────────────────────────────────
def section_f():
    try:
        from fastapi.testclient import TestClient
    except Exception as ex:
        record("F0|fastapi TestClient 可用", False, str(ex))
        return
    import api.main as api_main
    from core.watchdog import Watchdog

    audit, db = fresh("sm40_wd_f")
    tid = audit.create_ticket("SM-40 巡检主体夹具", type_="hazard")
    nid = audit.enqueue_notification("sm40:fw", "", {"k": "w"})
    wd = make_watchdog(audit, db)
    api_main._audit = audit
    api_main._watchdog = wd
    client = TestClient(api_main.app)      # 不进 lifespan：夹具组件即全部依赖

    adm = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}
    wdtok = {"X-Watchdog-Token": os.environ["SAFETYMIND_WATCHDOG_TOKEN"]}

    b = db_snapshot(db)
    r = client.get("/watchdog")
    record("F1|巡检报告匿名 → 401（部署形态锁定，防裸奔）",
           r.status_code == 401 and db_delta(b, db_snapshot(db)) == [],
           f"code={r.status_code}")
    r = client.get("/watchdog", headers={"X-Watchdog-Token": "wrong"})
    record("F2|巡检报告错 token → 401", r.status_code == 401, f"code={r.status_code}")

    wd.scan()   # 模拟后台循环首拍已发生（端点只读最近一次扫描，不自带采集）
    b = db_snapshot(db)
    r = client.get("/watchdog", headers=wdtok)
    body = r.json() if r.status_code == 200 else {}
    record("F3|巡检主体 GET /watchdog → 200，含 status/facts/events_recent/self_stats 且扫描计数≥1",
           r.status_code == 200
           and {"status", "facts", "events_recent", "self_stats"} <= set(body.keys())
           and body.get("self_stats", {}).get("scan_count", 0) >= 1
           and float(body.get("self_stats", {}).get("last_scan_duration_ms", -1)) >= 0,
           f"code={r.status_code} keys={sorted(body.keys())} stats={body.get('self_stats')}")
    record("F4|巡检读取零业务表写入（读端点自身也是只读）",
           db_delta(b, db_snapshot(db)) == [], f"delta={db_delta(b, db_snapshot(db))}")

    def probe(name, send):
        b = db_snapshot(db)
        resp = send()
        delta = db_delta(b, db_snapshot(db))
        record(name, resp.status_code in (401, 403) and not delta,
               f"code={resp.status_code} delta={delta} body={resp.text[:120]}")

    probe("F5|巡检主体改工单（POST transition，借道管理头）→ 401 且零漂移",
          lambda: client.post(f"/tickets/{tid}/transition",
                              json={"to_stage": "discuss"},
                              headers={"X-Admin-Token": os.environ["SAFETYMIND_WATCHDOG_TOKEN"]}))
    probe("F6|巡检主体改工单字段（PATCH /tickets/{id}）→ 401 且零漂移",
          lambda: client.patch(f"/tickets/{tid}", json={"priority": "high"},
                               headers={"X-Admin-Token": os.environ["SAFETYMIND_WATCHDOG_TOKEN"]}))
    probe("F7|巡检主体重放通知（POST /notifications/{id}/replay）→ 401 且零漂移",
          lambda: client.post(f"/notifications/{nid}/replay",
                              headers={"X-Admin-Token": os.environ["SAFETYMIND_WATCHDOG_TOKEN"]}))
    probe("F8|巡检主体发布 SOP（POST /skills/revisions/{rid}/publish）→ 401 且零漂移",
          lambda: client.post("/skills/revisions/rev-nonexistent/publish",
                              json={"base_revision": "base-x", "approver": "wd"},
                              headers={"X-Admin-Token": os.environ["SAFETYMIND_WATCHDOG_TOKEN"]}))

    r = client.post(f"/tickets/{tid}/transition", json={"to_stage": "discuss"}, headers=adm)
    record("F9|正向对照：管理员 token 流转工单 → 200（拒绝是主体边界而非端点坏死）",
           r.status_code == 200, f"code={r.status_code} body={r.text[:120]}")
    r = client.get("/watchdog", headers=adm)
    record("F10|管理员（无巡检 token）GET /watchdog → 200（管理员本可巡检）",
           r.status_code == 200, f"code={r.status_code}")
    audit.close()


def main():
    print(f"== SM-40 watchdog 回归 ==")
    section_a()
    section_b()
    section_c()
    section_d()
    section_e()
    section_f()

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    shutil.rmtree(str(TMP), ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
