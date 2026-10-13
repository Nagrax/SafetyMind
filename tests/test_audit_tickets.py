# 审计/升级/工单（SQLite）测试：python tests/test_audit_tickets.py
# 覆盖：审计落盘、升级事件+webhook 实收、工单全生命周期（建→列→闭→闭环时长/超期）、
#       五阶段回路（brief→discuss→execute→feedback→accept/reject，含回退与非法跳转拒绝）、
#       需求简报落库（intent/urgency/实体/摘要/routing_reason/request_id）、完整时间线、
#       工单进度查询（get_ticket_progress / GET /tickets/progress/{request_id}，用户端克制暴露）、
#       旧两态数据非破坏迁移（open→execute / closed→accept）、
#       /chat 火警端到端（死 LLM 下 CRITICAL 模板 → 自动升级+简报建单+建单时 webhook 实收）、
#       新端点（transition/timeline）鉴权与语义、管理端点鉴权。
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    tmp_db = "data/_audit_test.db"
    Path(tmp_db).unlink(missing_ok=True)
    os.environ["AUDIT_DB_PATH"] = tmp_db

    from core.audit_store import AuditStore
    store = AuditStore(tmp_db)

    # 1) 审计落盘 + 查询
    store.audit_request("r1", "u1", "c1", "动火作业票怎么办理", "work_permit", "medium", False, 42.5)
    rows = store.list_audit()
    record("审计落盘与查询", len(rows) == 1 and rows[0]["request_id"] == "r1" and rows[0]["escalated"] == 0)

    # 2) 升级事件 + webhook 实收（本地 HTTP 收端）
    received = []
    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(json.loads(body))
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *a): pass
    hook_srv = HTTPServer(("127.0.0.1", 8971), Hook)
    threading.Thread(target=hook_srv.serve_forever, daemon=True).start()

    from core.audit_store import notify_escalation_blocking
    uid = store.add_upgrade("r2", "u1", "c2", "车间着火了怎么办", "emergency_response")
    ok = notify_escalation_blocking("http://127.0.0.1:8971/hook", {
        "request_id": "r2", "text": "【SafetyMind 应急升级】编号 r2"})
    store.mark_notified(uid, "webhook")
    ups = store.list_upgrades()
    record("升级事件落盘", len(ups) == 1 and ups[0]["notified"] == 1)
    record("webhook 实收含编号", bool(received) and received[0].get("text", {}).get("content", "").find("r2") >= 0)
    record("值班确认 ack", store.ack_upgrade(uid) and store.list_upgrades(unacked_only=True) == [])

    # 3) 工单全生命周期 + 闭环时长 + 超期
    t1 = store.create_ticket("动火隐患整改", type_="hazard", due_ts=time.time() - 3600)  # 已超期
    t2 = store.create_ticket("应急升级: 车间着火", type_="emergency", source_request_id="r2")
    open_t = store.list_tickets(status="open")
    record("工单创建与列表", len([t for t in open_t if t["id"] in (t1, t2)]) == 2)
    record("超期标记", any(t["id"] == t1 and t["overdue"] for t in open_t))
    time.sleep(0.05)
    store.close_ticket(t1)
    closed = [t for t in store.list_tickets() if t["id"] == t1][0]
    record("闭环时长计算", closed["status"] == "closed" and closed["closure_hours"] >= 0)

    # 3.5) 五阶段回路（OCTO Loop）：需求简报落库 + 顺序流转 + 回退 + 非法跳转拒绝 + 时间线
    loop_tid = store.create_ticket(
        "反应釜超温应急整改", type_="emergency", priority="critical", source_request_id="r3",
        brief={"intent": "emergency_response", "urgency": "critical",
               "entities": {"area": ["一车间"], "chemical": []},
               "message_digest": "反应釜超温 联锁动作",
               "routing_reason": "CRITICAL 紧急事态，模板化应急响应（零 LLM 依赖）",
               "request_id": "r3"})
    tk = store.get_ticket(loop_tid)
    record("建单落需求简报", tk["stage"] == "brief" and tk["stage_label"] == "需求简报"
           and tk["intent"] == "emergency_response" and tk["urgency"] == "critical"
           and tk["entities"] == {"area": ["一车间"], "chemical": []}
           and tk["message_digest"] == "反应釜超温 联锁动作"
           and tk["routing_reason"].startswith("CRITICAL") and tk["source_request_id"] == "r3",
           json.dumps(tk, ensure_ascii=False, default=str))
    record("简报含 request_id（时间线 detail）",
           store.get_ticket_timeline(loop_tid)[0]["detail"].get("request_id") == "r3")

    nf = store.transition_ticket(99999, "discuss")
    bad_skip = store.transition_ticket(loop_tid, "execute", actor="a", note="跳过研讨")   # 跨阶段
    bad_jump = store.transition_ticket(loop_tid, "accept")                                # 跨阶段闭环
    bad_unknown = store.transition_ticket(loop_tid, "flying")                             # 未知阶段
    bad_same = store.transition_ticket(loop_tid, "brief")                                 # 重复同阶段
    record("非法跳转/未知阶段/同阶段拒绝",
           nf.get("kind") == "not_found" and bad_skip.get("kind") == "bad_transition"
           and bad_jump.get("kind") == "bad_transition" and bad_unknown.get("kind") == "bad_stage"
           and bad_same.get("kind") == "bad_transition"
           and store.get_ticket(loop_tid)["stage"] == "brief")

    seq = [store.transition_ticket(loop_tid, s, actor="值班员", note=n)["ok"] for s, n in (
        ("discuss", "值班员确认现场"), ("execute", "抢修方案已定"), ("feedback", "第一轮反馈: 需补充MSDS"))]
    record("顺序流转 brief→discuss→execute→feedback", all(seq))

    r = store.transition_ticket(loop_tid, "accept", actor="安环部", note="整改到位，验收通过")
    tk = store.get_ticket(loop_tid)
    record("验收通过闭环(accept→closed)", r["ok"] and tk["stage"] == "accept"
           and tk["status"] == "closed" and "closure_hours" in tk)

    back = store.transition_ticket(loop_tid, "feedback", actor="值班员", note="验收后追加: 复燃风险")
    rej = store.transition_ticket(loop_tid, "reject", actor="安环部", note="复核不合格")
    redo = store.transition_ticket(loop_tid, "feedback", actor="班组", note="返工后重新提交")
    re_acc = store.transition_ticket(loop_tid, "accept", actor="安环部", note="二次验收通过")
    record("accept/reject 可回退 feedback 再闭环", back["ok"] and rej["ok"] and redo["ok"]
           and re_acc["ok"] and store.get_ticket(loop_tid)["status"] == "closed")

    tl = store.get_ticket_timeline(loop_tid)
    acts = [e["action"] for e in tl]
    stages = [e["to_stage"] for e in tl]
    store.record_ticket_event(loop_tid, "notify_webhook", actor="system",
                              note="值班 webhook 已实收（需求简报建单时发出）",
                              to_stage="brief", detail={"request_id": "r3"})
    tl2 = store.get_ticket_timeline(loop_tid)
    record("时间线完整（顺序+批注+时间戳+详情）",
           acts[0] == "create" and acts.count("stage_change") == 8
           and stages[:5] == ["brief", "discuss", "execute", "feedback", "accept"]
           and stages[-1] == "accept"
           and all(e["ts"] > 0 and e["actor"] and e["note"] for e in tl if e["action"] == "stage_change")
           and tl2[-1]["action"] == "notify_webhook" and tl2[-1]["detail"].get("request_id") == "r3",
           json.dumps(tl, ensure_ascii=False, default=str))
    store.transition_ticket(loop_tid, "feedback", actor="测试", note="重开以验证旧 close 接口")
    record("旧 close 接口视同验收完成", store.close_ticket(loop_tid) is True
           and store.close_ticket(loop_tid) is False  # 重复闭环幂等拒绝
           and store.get_ticket(loop_tid)["stage"] == "accept"
           and store.get_ticket_timeline(loop_tid)[-1]["action"] == "legacy_close")
    m = store.create_ticket("手动工单无简报")
    tkm = store.get_ticket(m)
    record("手动建单最小简报", tkm["stage"] == "brief" and tkm["routing_reason"] == "管理员手动建单")

    # 3.55) 工单进度查询（用户端升级卡片数据源）：按 source_request_id 查
    #       五阶段进度骨架 + 最新批注；克制暴露——不下发完整时间线/简报细节。
    prog_done = store.get_ticket_progress("r3")
    record("进度查询:闭环工单(verdict=accept+四步全done+无steps批注)",
           bool(prog_done) and prog_done["found"] is True and prog_done["stage"] == "accept"
           and prog_done["verdict"]["state"] == "done" and prog_done["verdict"]["stage"] == "accept"
           and all(s["state"] == "done" for s in prog_done["steps"])
           and "timeline" not in prog_done
           and not any("note" in s for s in prog_done["steps"])
           and all(k not in prog_done for k in ("entities", "message_digest", "routing_reason")),
           json.dumps(prog_done, ensure_ascii=False, default=str) if prog_done else "返回 None")
    prog_mid_tid = store.create_ticket(
        "泄漏扩散应急处置", type_="emergency", priority="critical", source_request_id="r_prog",
        brief={"intent": "emergency_response", "urgency": "critical", "entities": {},
               "message_digest": "泄漏扩散", "routing_reason": "CRITICAL 紧急事态",
               "request_id": "r_prog"})
    store.transition_ticket(prog_mid_tid, "discuss", actor="值班员", note="开始研讨处置方案")
    prog = store.get_ticket_progress("r_prog")
    record("进度查询:在办工单(brief done/discuss current/其余 pending+最新批注)",
           bool(prog) and prog["stage"] == "discuss" and prog["stage_label"] == "过程研讨"
           and [s["state"] for s in prog["steps"]] == ["done", "current", "pending", "pending"]
           and prog["verdict"]["state"] == "pending"
           and prog["latest_event"] and prog["latest_event"]["note"] == "开始研讨处置方案"
           and prog["latest_event"]["actor"] == "值班员" and prog["latest_event"]["ts"] > 0,
           json.dumps(prog, ensure_ascii=False, default=str) if prog else "返回 None")
    record("进度查询:未知编号回 None(端点据此 found=false)",
           store.get_ticket_progress("r-no-such-id") is None)

    # 3.6) 旧数据兼容：两态旧库非破坏迁移（open→execute / closed→accept）
    import sqlite3 as _sq3
    legacy_db = "data/_audit_legacy_test.db"
    Path(legacy_db).unlink(missing_ok=True)
    lcon = _sq3.connect(legacy_db)
    lcon.executescript("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ts REAL NOT NULL,
        title TEXT NOT NULL,
        type TEXT DEFAULT 'hazard',
        priority TEXT DEFAULT 'normal',
        status TEXT DEFAULT 'open',
        assignee TEXT DEFAULT '',
        source_request_id TEXT DEFAULT '',
        due_ts REAL,
        closed_ts REAL
    );
    """)
    lcon.execute("INSERT INTO tickets (created_ts, title, type, status, due_ts) VALUES (?,?,?,?,?)",
                 (time.time() - 86400, "旧在办工单", "hazard", "open", time.time() + 3600))
    lcon.execute("INSERT INTO tickets (created_ts, title, type, status, closed_ts) VALUES (?,?,?,?,?)",
                 (time.time() - 86400, "旧闭环工单", "hazard", "closed", time.time() - 3600))
    lcon.commit()
    old_ids = [r[0] for r in lcon.execute("SELECT id FROM tickets ORDER BY id")]
    lcon.close()
    store_lg = AuditStore(legacy_db)
    tk_open = store_lg.get_ticket(old_ids[0])
    tk_closed = store_lg.get_ticket(old_ids[1])
    record("旧 open→execute / closed→accept",
           tk_open["stage"] == "execute" and tk_open["status"] == "open"
           and tk_closed["stage"] == "accept" and tk_closed["status"] == "closed",
           json.dumps([tk_open, tk_closed], ensure_ascii=False, default=str))
    tl_old = store_lg.get_ticket_timeline(old_ids[0])
    record("旧数据迁移留痕", bool(tl_old) and tl_old[0]["action"] == "legacy_stage")
    record("旧在办工单可继续流转", store_lg.transition_ticket(old_ids[0], "feedback", actor="admin", note="旧单续转")["ok"]
           and store_lg.get_ticket(old_ids[0])["stage"] == "feedback")
    record("旧闭环工单可回退重做", store_lg.transition_ticket(old_ids[1], "feedback", actor="admin", note="重做")["ok"]
           and store_lg.get_ticket(old_ids[1])["status"] == "open")
    record("旧库列表/报表可用", all("stage" in t and "overdue" in t
                                   for t in store_lg.list_tickets())
           and store_lg.stats()["tickets_open"] >= 1)
    store_lg.close()

    # 4) 报表
    st = store.stats()
    record("报表统计", st["tickets_open"] >= 1 and st["escalations_24h"] >= 1 and st["requests_24h"] >= 1,
           json.dumps(st))
    hook_srv.shutdown()

    # 5) /chat 端到端（死 LLM）：CRITICAL 模板 → 自动升级+简报建单+建单时 webhook；新端点；鉴权
    os.environ.update({"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_API_KEY": "dummy", "API_HOST": "0.0.0.0",
                       "ADMIN_TOKEN": "test-admin-123", "AUDIT_DB_PATH": tmp_db,
                       "ESCALATION_WEBHOOK_URL": "http://127.0.0.1:8972/hook"})
    received2 = []  # 建单（需求简报）时点的 webhook 收端

    class Hook2(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received2.append(json.loads(body))
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *a): pass

    hook2_srv = HTTPServer(("127.0.0.1", 8972), Hook2)
    threading.Thread(target=hook2_srv.serve_forever, daemon=True).start()

    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as client:
        r = client.post("/chat", json={"message": "车间着火了怎么办", "user_id": "e2e", "conv_id": "e2e1"})
        ok = r.status_code == 200 and r.json()["escalated"] is True
        rid = r.json().get("request_id", "") if ok else ""
        record("E2E 火警触发升级", ok, f"code={r.status_code}")
        r = client.get("/audit", headers={"X-Admin-Token": "wrong"})
        record("管理端点拒绝无效令牌", r.status_code == 401)
        r = client.get("/audit", headers={"X-Admin-Token": "test-admin-123"})
        ok = r.status_code == 200 and any(e["escalated"] == 1 for e in r.json()["entries"])
        record("审计含升级记录", ok, f"code={r.status_code}")
        r = client.get("/audit/stats", headers={"X-Admin-Token": "test-admin-123"})
        st = r.json()
        record("E2E 报表(升级/自动建单)", r.status_code == 200 and st["escalations_24h"] >= 1 and st["tickets_open"] >= 1,
               json.dumps(st))
        r = client.post("/tickets", json={"title": "手动工单", "due_hours": 48},
                        headers={"X-Admin-Token": "test-admin-123"})
        record("手动建工单(48h限期)", r.status_code == 200 and r.json().get("ticket_id", 0) > 0)
        r = client.get("/tickets", headers={"X-Admin-Token": "test-admin-123"})
        record("工单列表含超期字段", all("overdue" in t for t in r.json()["tickets"]))

        # 5.1) 升级自动建单 = 需求简报（brief 阶段）一次性落库
        r = client.get("/tickets", headers={"X-Admin-Token": "test-admin-123"})
        tk = next((t for t in r.json()["tickets"] if t.get("source_request_id") == rid), None)
        ok = (tk is not None and tk["stage"] == "brief" and tk["type"] == "emergency"
              and tk["intent"] == "emergency_response" and tk["urgency"] == "critical"
              and isinstance(tk["entities"], dict)
              and "车间着火" in (tk["message_digest"] or "")
              and bool(tk["routing_reason"]))
        record("E2E 简报建单(阶段/intent/urgency/摘要/routing)", ok,
               json.dumps(tk, ensure_ascii=False, default=str) if tk else "未找到升级工单")

        # 5.2) webhook 在建单（需求简报）时发出且实收，升级事件标记已通知（行为不回退）
        # 轮询同时等两件事：webhook 实收 与 mark_notified 落库——两者之间有
        # 天然间隙（hook 返回后服务端才标记），只等实收就断言 notified 会偶发红灯
        deadline = time.time() + 10
        notified_rows = []
        while time.time() < deadline:
            if received2:
                notified_rows = [u for u in store.list_upgrades() if u["request_id"] == rid]
                if notified_rows and notified_rows[0]["notified"] == 1:
                    break
            time.sleep(0.2)
        record("E2E 建单时 webhook 实收", bool(received2) and rid in received2[0]["text"]["content"],
               f"received2={received2}")
        record("E2E 升级事件已标记通知", bool(notified_rows) and notified_rows[0]["notified"] == 1)

        # 5.3) GET timeline：建单事件在首、含简报详情与 webhook 通知事件
        r = client.get(f"/tickets/{tk['id']}/timeline", headers={"X-Admin-Token": "test-admin-123"})
        body = r.json()
        tl = body.get("timeline", [])
        deadline = time.time() + 10
        while time.time() < deadline and not any(e["action"] == "notify_webhook" for e in tl):
            time.sleep(0.2)
            tl = client.get(f"/tickets/{tk['id']}/timeline",
                            headers={"X-Admin-Token": "test-admin-123"}).json().get("timeline", [])
        record("E2E 时间线=简报+通知", r.status_code == 200 and body["ticket"]["id"] == tk["id"]
               and tl[0]["action"] == "create" and tl[0]["to_stage"] == "brief"
               and tl[0]["detail"].get("request_id") == rid
               and any(e["action"] == "notify_webhook" for e in tl),
               json.dumps(tl, ensure_ascii=False, default=str))

        # 5.4) transition 端点：合法流转 200 / 非法跳转 400 / 不存在 404 / 无效令牌 401
        h = {"X-Admin-Token": "test-admin-123"}
        r = client.post(f"/tickets/{tk['id']}/transition", json={"to_stage": "discuss", "actor": "值班员", "note": "开始研讨"}, headers=h)
        ok1 = r.status_code == 200 and r.json()["ticket"]["stage"] == "discuss"
        r = client.post(f"/tickets/{tk['id']}/transition", json={"to_stage": "feedback"}, headers=h)
        ok2 = r.status_code == 400  # discuss→feedback 跨阶段
        r = client.post("/tickets/999999/transition", json={"to_stage": "discuss"}, headers=h)
        ok3 = r.status_code == 404
        r = client.post(f"/tickets/{tk['id']}/transition", json={"to_stage": "execute"},
                        headers={"X-Admin-Token": "wrong"})
        ok4 = r.status_code == 401
        record("E2E transition 端点四态", ok1 and ok2 and ok3 and ok4,
               f"legal={r.status_code}|{ok1}|{ok2}|{ok3}|{ok4}")
        r = client.get("/tickets/999999/timeline", headers=h)
        record("E2E timeline 不存在回 404", r.status_code == 404)

        # 5.45) GET /tickets/progress/{request_id}：用户端升级卡片数据源。
        #        免管理令牌（编号即凭据）、克制暴露（进度骨架+最新批注，无时间线/简报细节）。
        r = client.post(f"/tickets/{tk['id']}/transition",
                        json={"to_stage": "execute", "actor": "维修班", "note": "已派单维修班现场处置"},
                        headers=h)
        record("E2E 进度前置流转 discuss→execute", r.status_code == 200
               and r.json()["ticket"]["stage"] == "execute", f"code={r.status_code}")
        body = {}
        deadline = time.time() + 5
        while time.time() < deadline:  # 等 webhook 通知事件与流转事件都落定后再断言
            body = client.get(f"/tickets/progress/{rid}").json()
            if (body.get("latest_event") or {}).get("note") == "已派单维修班现场处置":
                break
            time.sleep(0.2)
        ok = (client.get(f"/tickets/progress/{rid}").status_code == 200
              and body.get("found") is True and body.get("ticket_id") == tk["id"]
              and body.get("stage") == "execute" and body.get("stage_label") == "执行产出"
              and [s["state"] for s in body.get("steps", [])] == ["done", "done", "current", "pending"]
              and body.get("verdict", {}).get("state") == "pending"
              and body.get("latest_event", {}).get("note") == "已派单维修班现场处置"
              and body.get("latest_event", {}).get("actor") == "维修班"
              and "timeline" not in body
              and not any("note" in s for s in body.get("steps", []))
              and all(k not in body for k in ("entities", "message_digest", "routing_reason")))
        record("E2E 进度端点(免管钥/五阶段骨架/最新批注/不下发简报细节)", ok,
               json.dumps(body, ensure_ascii=False, default=str))
        r = client.get("/tickets/progress/no-such-request-id")
        record("E2E 进度端点未知编号 found=false", r.status_code == 200 and r.json().get("found") is False,
               f"code={r.status_code} body={r.text[:80]}")
        r = client.get(f"/api/python/tickets/progress/{rid}")
        record("E2E 进度端点 /api/python 别名", r.status_code == 200 and r.json().get("found") is True,
               f"code={r.status_code}")

        # 5.5) 旧 close 端点行为不回退：任意阶段闭环且视同验收完成
        r = client.post(f"/tickets/{tk['id']}/close", headers=h)
        tl_last = client.get(f"/tickets/{tk['id']}/timeline", headers=h).json()["timeline"][-1]
        record("E2E 旧 close 兼容映射 accept", r.json().get("closed") is True
               and client.get(f"/tickets/{tk['id']}/timeline", headers=h).json()["ticket"]["stage"] == "accept"
               and tl_last["action"] == "legacy_close")
        prog = client.get(f"/tickets/progress/{rid}").json()
        record("E2E 闭环后进度 verdict=accept", prog.get("verdict", {}).get("state") == "done"
               and prog.get("verdict", {}).get("stage") == "accept",
               json.dumps(prog, ensure_ascii=False, default=str))
    hook2_srv.shutdown()

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
