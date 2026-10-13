# 隐患工单闭环测试：python tests/test_hazard_tickets.py
# 覆盖：
#   A. hazard_gate.decide 触发矩阵：HIGH+ 自动 / MEDIUM 建议卡 / LOW 与纯咨询不触发 /
#      escalated 跳过（应急通道互斥）/ 总开关关闭 / 重大定级（台账三大类+危化品或HIGH）/
#      派发规则（电气→设备动力部）/ 无类别命中按一般登记；
#   B. HTTP 端到端（死 LLM + patch 闸门裁决，消除意图分类随机性）：
#      - /chat suggested → 暂存 + 响应确认卡字段 → POST /hazard/confirm 建单 + webhook 实收
#      - 重复 confirm / 未知 request_id → 404（建议一次性）
#      - /chat auto → auto_ticket 响应 + 工单落库（severity/dept/user_id/due_ts）
#      - 同用户同实体 24h 重复上报 → merged 合并不新建（repeat_report 时间线）
#      - PATCH /tickets/{id} 研讨改判留痕（admin 鉴权 + 404/400 分支）
#      - GET /tickets/overdue、GET /tickets/progress/{request_id}
#   C. 真实链路互斥：CRITICAL 火警消息走应急通道（escalated），闸门不重复建单。
# 铁律 9：全部存储走 tempfile 临时目录，严禁与桌面端共享 data/chroma、data/kb_chroma。
import json
import os
import sys
import tempfile
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
    tmp = tempfile.mkdtemp(prefix="hazard_test_")
    os.environ.update({
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "API_HOST": "0.0.0.0",
        "ADMIN_TOKEN": "test-admin-123",
        "AUDIT_DB_PATH": str(Path(tmp) / "audit.db"),
        "PREFERENCE_DB_PATH": str(Path(tmp) / "pref.db"),
        "SAFETYMIND_MEMORY_SNAPSHOT": str(Path(tmp) / "mem.json"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(Path(tmp) / "convs"),
        "CHROMA_PERSIST_DIRECTORY": str(Path(tmp) / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(Path(tmp) / "kb_chroma"),
        "SAFETYMIND_EMBEDDING": "ngram",   # 零下载离线嵌入，测试不碰 HF 缓存
        "SAFETYMIND_CLARIFY": "0",
        "AUDIT_ENABLED": "1",
    })

    # ── A. 闸门矩阵（纯函数，不起服务）─────────────────────────────────────
    from core import hazard_gate

    def d(intent, urg, ents=None, msg="", esc=False, enabled=None):
        return hazard_gate.decide(intent, urg, ents or {}, msg, esc, enabled)

    r = d("hazard_report", 3, {}, "罐区巡检发现3号储罐底部渗漏")
    record("矩阵: HIGH 隐患上报→自动建单",
           r is not None and r["action"] == "auto" and r["severity"] == "major"
           and r["category"] == "易燃易爆设施及场所" and r["dept"] == "安环部",
           json.dumps(r, ensure_ascii=False))
    r = d("hazard_report", 2, {}, "巡检发现隐患：配电室电缆绝缘老化")
    record("矩阵: MEDIUM 隐患上报→建议卡（电气派设备动力部）",
           r is not None and r["action"] == "suggest" and r["severity"] == "general"
           and r["dept"] == "设备动力部" and r["deadline_hours"] == 72,
           json.dumps(r, ensure_ascii=False))
    record("矩阵: 设备报警 HIGH→自动", d("equipment_alarm", 3, {}, "反应器超温报警")["action"] == "auto")
    r = d("hazard_inspection", 3, {}, "罐区隐患排查")
    record("矩阵: 隐患排查永不自动（HIGH 也只建议）",
           r is not None and r["action"] == "suggest")
    record("矩阵: 纯咨询不触发", d("general_consult", 3, {}, "什么是双重预防机制") is None)
    record("矩阵: LOW 不触发", d("hazard_report", 1, {}, "发现隐患应该怎么上报") is None)
    record("矩阵: escalated 跳过（应急通道互斥）",
           d("hazard_report", 4, {}, "甲醇储罐大量泄漏", esc=True) is None)
    record("矩阵: 总开关关闭", d("hazard_report", 3, {}, "罐区渗漏", enabled=False) is None)
    r = d("hazard_report", 2, {"chemical": ["甲醇"]}, "罐区阀门有点渗漏")
    record("矩阵: 重大倾向类+危化品实体（MEDIUM）→重大",
           r is not None and r["severity"] == "major", json.dumps(r, ensure_ascii=False))
    r = d("hazard_report", 3, {}, "办公室文件柜挡住了走廊")
    record("矩阵: 无类别命中按一般登记留依据",
           r is not None and r["severity"] == "general" and "未匹配" in r["basis"],
           json.dumps(r, ensure_ascii=False))

    # ── B. HTTP 端到端（TestClient + patch 闸门裁决）────────────────────────
    received = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(json.loads(body))
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    hook_srv = HTTPServer(("127.0.0.1", 8974), Hook)
    threading.Thread(target=hook_srv.serve_forever, daemon=True).start()
    os.environ["ESCALATION_WEBHOOK_URL"] = "http://127.0.0.1:8974/hook"

    from fastapi.testclient import TestClient
    from api import main as api_main

    admin = {"X-Admin-Token": "test-admin-123"}
    fake_suggest = {"action": "suggest", "severity": "general", "category": "电气系统",
                    "basis": "《爆炸危险环境电力装置设计规范》GB50058（测试种子）",
                    "dept": "设备动力部", "deadline_hours": 72,
                    "summary": "电气系统隐患：配电室电缆绝缘老化"}
    fake_auto = {"action": "auto", "severity": "major", "category": "易燃易爆设施及场所",
                 "basis": "GB（测试种子）", "dept": "安环部", "deadline_hours": 24,
                 "summary": "易燃易爆设施及场所隐患：罐区3号储罐底部渗漏"}
    orig_decide = hazard_gate.decide

    with TestClient(api_main.app) as client:
        # B1 suggested 全链路：/chat 暂存 → 确认建单 → webhook → 进度可见
        hazard_gate.decide = lambda *a, **k: dict(fake_suggest)
        r = client.post("/chat", json={"message": "配电室电缆绝缘老化，有隐患",
                                       "user_id": "u-hz1", "conv_id": "c-hz1"})
        body = r.json()
        record("E2E 建议卡: 响应携带确认卡字段",
               r.status_code == 200 and body["hazard_action"] == "suggested"
               and body["hazard_ticket_id"] == 0
               and body["hazard_suggestion"]["dept"] == "设备动力部"
               and body["hazard_suggestion"]["deadline_hours"] == 72,
               json.dumps({k: body.get(k) for k in ("hazard_action", "hazard_suggestion")}, ensure_ascii=False))
        rid1 = body.get("request_id", "")
        record("E2E 建议卡: 契约字段不回退（旧字段齐全）",
               bool(body.get("request_id")) and "escalated" in body and "routing_reason" in body
               and "intent_source_scores" in body)
        r = client.post("/hazard/confirm", json={"request_id": rid1, "user_id": "u-hz1"})
        ok = r.status_code == 200 and r.json()["ok"] is True and r.json()["merged"] is False
        tid1 = r.json().get("ticket_id", 0) if ok else 0
        record("E2E 确认建单", ok and tid1 > 0, f"code={r.status_code} body={r.text[:200]}")
        r = client.post("/hazard/confirm", json={"request_id": rid1, "user_id": "u-hz1"})
        record("E2E 重复确认 404（建议一次性）", r.status_code == 404, f"code={r.status_code}")
        r = client.post("/hazard/confirm", json={"request_id": "no-such-rid"})
        record("E2E 未知编号 404", r.status_code == 404, f"code={r.status_code}")
        t = client.get(f"/tickets/progress/{rid1}").json()
        record("E2E 进度端点可见（brief 阶段起步）",
               t.get("found") and t.get("ticket_id") == tid1 and t.get("stage") == "brief",
               json.dumps(t, ensure_ascii=False)[:200])

        # B2 auto 全链路 + 去重合并（实体可抽取：位号 P-101 是去重键——
        # 实体抽取规则要求 位号/编号 或 ≥2 汉字前缀的区域名）
        hazard_gate.decide = lambda *a, **k: dict(fake_auto)
        r = client.post("/chat", json={"message": "位号P-101储罐底部渗漏，需要处理",
                                       "user_id": "u-hz2", "conv_id": "c-hz2"})
        body = r.json()
        tid2 = body.get("hazard_ticket_id", 0)
        record("E2E 高危自动建单",
               r.status_code == 200 and body["hazard_action"] == "auto_ticket" and tid2 > 0,
               json.dumps({k: body.get(k) for k in ("hazard_action", "hazard_ticket_id")}))
        tickets = {t["id"]: t for t in client.get("/tickets", headers=admin).json()["tickets"]}
        t2 = tickets.get(tid2, {})
        record("E2E 工单字段落库（定级/派发/上报人/限期）",
               t2.get("type") == "hazard" and t2.get("severity") == "major"
               and t2.get("responsible_dept") == "安环部" and t2.get("user_id") == "u-hz2"
               and t2.get("priority") == "high" and t2.get("due_ts", 0) > time.time(),
               json.dumps(t2, ensure_ascii=False)[:300])
        r = client.post("/chat", json={"message": "位号P-101储罐底部还在渗漏，越来越明显了",
                                       "user_id": "u-hz2", "conv_id": "c-hz2"})
        body = r.json()
        record("E2E 24h 同实体重复上报合并",
               body.get("hazard_action") == "merged" and body.get("hazard_ticket_id") == tid2,
               json.dumps({k: body.get(k) for k in ("hazard_action", "hazard_ticket_id")}))
        timeline = client.get(f"/tickets/{tid2}/timeline", headers=admin).json()["timeline"]
        record("E2E 合并留 repeat_report 时间线",
               any(e["action"] == "repeat_report" for e in timeline))

        # B3 PATCH 改判 + 鉴权 + 分支
        r = client.patch(f"/tickets/{tid1}", json={"severity": "major", "note": "研讨改判：涉及配电室"}, headers=admin)
        ok = r.status_code == 200 and r.json()["ok"] and r.json()["ticket"]["severity"] == "major"
        record("PATCH 研讨改判", ok, f"code={r.status_code} body={r.text[:200]}")
        timeline = client.get(f"/tickets/{tid1}/timeline", headers=admin).json()["timeline"]
        record("PATCH 留痕（patch 事件含前后值）",
               any(e["action"] == "patch" and e["detail"].get("severity", {}).get("from") == "general"
                   for e in timeline))
        record("PATCH 无令牌 401/403", client.patch(f"/tickets/{tid1}", json={"severity": "general"}).status_code in (401, 403))
        record("PATCH 不存在 404", client.patch("/tickets/99999", json={"severity": "general"}, headers=admin).status_code == 404)
        record("PATCH 空字段 400", client.patch(f"/tickets/{tid1}", json={}, headers=admin).status_code == 400)

        # B4 逾期清单（改一个过期限期）
        r = client.patch(f"/tickets/{tid1}", json={"due_hours": -1, "note": "测试逾期"}, headers=admin)
        overdue = client.get("/tickets/overdue", headers=admin).json()["tickets"]
        record("逾期清单包含刚过期工单", any(t["id"] == tid1 for t in overdue))
        r = client.get("/tickets/overdue")
        record("逾期清单需管理令牌", r.status_code in (401, 403))

        # B5 真实链路互斥：火警（未 patch）走应急通道，闸门不建隐患单
        hazard_gate.decide = orig_decide
        r = client.post("/chat", json={"message": "车间着火了怎么办", "user_id": "u-hz3"})
        body = r.json()
        record("互斥: CRITICAL 走应急通道，闸门不触发",
               r.status_code == 200 and body["escalated"] is True
               and body["hazard_action"] == "none" and body["hazard_suggestion"] is None,
               json.dumps({k: body.get(k) for k in ("escalated", "hazard_action")}))

        # webhook 收端断言：至少 2 条隐患工单通知（确认建单 + 自动建单），文本带派发信息
        deadline = time.time() + 10
        while time.time() < deadline and len(received) < 2:
            time.sleep(0.2)
        hz_texts = [x.get("text", {}).get("content", "") for x in received
                    if "隐患工单" in x.get("text", {}).get("content", "")]
        record("webhook 派单通知实收",
               len(hz_texts) >= 2 and any("设备动力部" in t for t in hz_texts)
               and any("安环部" in t for t in hz_texts),
               f"收到 {len(hz_texts)} 条隐患通知")

    hook_srv.shutdown()

    passed = sum(1 for x in RESULTS if x)
    print(f"\n{passed}/{len(RESULTS)} PASSED")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
