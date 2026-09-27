# 审计/升级/工单（SQLite）测试：python tests/test_audit_tickets.py
# 覆盖：审计落盘、升级事件+webhook 实收、工单全生命周期（建→列→闭→闭环时长/超期）、
#       /chat 火警端到端（死 LLM 下 CRITICAL 模板 → 自动升级+自动建单）、管理端点鉴权。
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

    # 4) 报表
    st = store.stats()
    record("报表统计", st["tickets_open"] >= 1 and st["escalations_24h"] >= 1 and st["requests_24h"] >= 1,
           json.dumps(st))
    hook_srv.shutdown()

    # 5) /chat 端到端（死 LLM）：CRITICAL 模板 → 自动升级+自动建单+审计；管理端点鉴权
    os.environ.update({"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_API_KEY": "dummy", "API_HOST": "0.0.0.0",
                       "ADMIN_TOKEN": "test-admin-123", "AUDIT_DB_PATH": tmp_db,
                       "ESCALATION_WEBHOOK_URL": ""})
    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as client:
        r = client.post("/chat", json={"message": "车间着火了怎么办", "user_id": "e2e", "conv_id": "e2e1"})
        ok = r.status_code == 200 and r.json()["escalated"] is True
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

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
