# -*- coding: utf-8 -*-
# 闭环发现 api/main.py::no-audit-on-rejection（2026-11-09，severity=low）回归：
# 被拒越权写此前不留任何审计行（DB 六表快照前后零变化，RF-3 实测口径），审计员无法
# 从库内看到拒绝始末。设计：scratch/sm-loop/handoff/SM-20/run-wf/mandate-contract.md §4
# "一般越权写的拒绝审计"——拒绝也是授权事件：主体已解析但越权的写拒绝在 audit_log 落
# 一行（actor/action/target/reason/request_id），与快照闸门的 timeline 留痕同构；
# 五张业务表保持零漂移；只记授权元数据，绝不落他人数据内容（建议/工单正文）。
#
# 覆盖（全部走 FastAPI TestClient 真实 HTTP 端点，不起真实服务、零 LLM、零网络）：
#   R1 跨主体确认（无键路径）→ 403 且五业务表零漂移 + audit_log 恰一条
#      hazard_confirm.rejected 行（actor/target/reason/request_id 逐字段钉死，
#      行内不含建议 summary/content 文本——不泄露他人数据内容）
#   R2 匿名主体确认 → 403 + 一条拒绝行（actor="anonymous"，撤权同口径）
#   R3 幂等键路径跨主体（X-Idempotency-Key + 他人令牌）→ 403 + 一条拒绝行
#   R4 reporter 主体 accept 流转 → 403 + 一条 ticket_transition.rejected 行
#      （target=工单 id，reason=verifier_role_required；工单行与时间线零漂移）
#   R5 reporter 主体旧入口 close → 403 + 一条 ticket_close.rejected 行
#   R6 拒绝后归属人确认同一建议 → 200（正向闭环：拒绝不消费建议）
#   R7 审计写失败不破坏拒绝语义（fail-safe：record_auth_rejection 注入异常 → 仍 403）
#   R8 同主体重放已处理建议 → 404 且六表零漂移（负控：非越权拒绝不写审计行）
#
# 红灯证据：scratch/sm-loop/handoff/findings-cleanup/api_main.py--no-audit-on-rejection/
# 铁律：存储全部走一次性临时目录（审计库钉到 tmp），不触碰生产数据目录与仓库 data/；
# 环境不可用退出码 2，绝不伪装成业务失败。
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TOKEN_A, TOKEN_B, TOKEN_ADM = "arx-tok-a", "arx-tok-b", "arx-tok-adm"
ACCESS, ADMIN = "arx-access-1", "arx-admin-1"
USERS_FULL = [
    {"token": TOKEN_A, "subject_id": "u-A", "roles": ["reporter"], "scopes": ["plant-a"]},
    {"token": TOKEN_B, "subject_id": "u-B", "roles": ["reporter"], "scopes": ["plant-b"]},
    {"token": TOKEN_ADM, "subject_id": "u-ADM", "roles": ["admin", "verifier"], "scopes": ["*"]},
]
RID = "RID-ARX-1"   # u-A 建议，保持 pending（越权确认靶子）
SUG_SUMMARY = "己区灭火器压力表归零"   # 建议正文：任何拒绝审计行都不得携带

# 六表口径与 test_role_matrix_api/test_destructive_isolated 一致：
# audit_log 允许恰增一条拒绝行，其余五张业务表必须零漂移
BUSINESS_TABLES = ["upgrade_events", "tickets", "hazard_suggestions",
                   "ticket_timeline", "notification_outbox"]
TABLES = ["audit_log"] + BUSINESS_TABLES

RESULTS = []
TMP = Path(tempfile.mkdtemp(prefix="sm_auth_rejection_", dir="D:/claudecode/scratch/sm-loop"))


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def hdr(user_token=""):
    h = {"X-Access-Token": ACCESS}
    if user_token:
        h["X-User-Token"] = user_token
    return h


def ahdr(user_token=""):
    h = hdr(user_token)
    h["X-Admin-Token"] = ADMIN
    return h


def db_snapshot():
    """六表全量快照（只读连接，canonical JSON）：写前后对比的唯一依据。"""
    con = sqlite3.connect(f"file:{TMP / 'audit.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {t: json.dumps([dict(r) for r in con.execute(f"SELECT * FROM {t}")],
                              sort_keys=True, ensure_ascii=False, default=str)
                for t in TABLES}
    finally:
        con.close()


def db_delta(before, after):
    return sorted(t for t in TABLES if before[t] != after[t])


def audit_rows_between(before_snapshot):
    """快照之后新落库的 audit_log 行（按快照前后总数差取最新若干行，独立读取以断言逐字段）。"""
    n0 = len(json.loads(before_snapshot["audit_log"]))
    con = sqlite3.connect(f"file:{TMP / 'audit.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        total = con.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
        new_n = max(total - n0, 0)
        if not new_n:
            return []
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (new_n,))]
    finally:
        con.close()
    return list(reversed(rows))   # 按落库顺序返回（旧行在前）


def sug_payload():
    return {
        "decision": {"action": "suggest", "severity": "major", "summary": SUG_SUMMARY,
                     "dept": "设备动力部", "deadline_hours": 72, "basis": "拒绝审计夹具"},
        "brief": {"intent": "hazard_report", "urgency": "medium",
                  "entities": {"area": ["己区"]}, "message_digest": SUG_SUMMARY,
                  "routing_reason": "auth-rejection-audit-fixture"},
    }


def check_rejection_row(tag, before, expect):
    """公共断言：403 后五业务表零漂移 + audit_log 恰一条行且逐字段符合 expect，
    行内不携带建议正文（不泄露他人数据内容）。expect 缺键的字段不作单字段断言。"""
    delta = db_delta(before, db_snapshot())
    ok = set(delta) == {"audit_log"}
    detail = f"delta={delta}"
    rows = audit_rows_between(before) if ok else []
    if ok:
        ok = len(rows) == 1
        detail += f" rows={len(rows)}"
    if ok:
        row = rows[0]
        for field, want in expect.items():
            if str(row.get(field, "")) != str(want):
                ok = False
                detail += f" {field}={row.get(field)!r}!={want!r}"
        if ok and SUG_SUMMARY in json.dumps(row, ensure_ascii=False, default=str):
            ok = False
            detail += " 泄露建议正文（summary 出现在审计行）"
    record(tag, ok, detail)
    return ok


def main():
    # 环境必须先于 import 钉死（api.main 导入时 load_dotenv 不覆盖已显式设置的变量）
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
        "SAFETYMIND_OUTBOX_ENABLED": "0",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "AUDIT_DB_PATH": str(TMP / "audit.db"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(TMP / "convs"),
        "CHROMA_HOST": "127.0.0.1",
        "CHROMA_PORT": "8973",
        "CHROMA_PERSIST_DIRECTORY": str(TMP / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(TMP / "kb_chroma"),
        "PROMETHEUS_PORT": "0",
        "AUDIT_ENABLED": "1",
        "SAFETYMIND_DEPLOYED": "1",
        "ACCESS_TOKEN": ACCESS,
        "ADMIN_TOKEN": ADMIN,
        "API_HOST": "0.0.0.0",
    })
    os.environ["SAFETYMIND_USERS"] = json.dumps(USERS_FULL, ensure_ascii=False)

    try:
        from fastapi.testclient import TestClient
    except Exception as ex:
        print(f"[REJECT] 环境不可用（fastapi TestClient 缺失）: {ex}")
        shutil.rmtree(str(TMP), ignore_errors=True)
        return 2
    try:
        from core.audit_store import AuditStore
        import api.main as api_main
    except Exception as ex:
        print(f"[REJECT] 环境不可用（产品模块导入失败）: {type(ex).__name__}: {ex}")
        shutil.rmtree(str(TMP), ignore_errors=True)
        return 2
    try:
        audit = AuditStore(db_path=str(TMP / "audit.db"))
    except Exception as ex:
        print(f"[REJECT] 环境不可用（审计库创建失败）: {type(ex).__name__}: {ex}")
        shutil.rmtree(str(TMP), ignore_errors=True)
        return 2

    client = TestClient(api_main.app)   # 不进 lifespan：夹具组件即全部依赖
    api_main._audit = audit
    audit.save_hazard_suggestion(RID, "u-A", f"conv-{RID}", sug_payload())

    def confirm(rid, tok, user_id="u-A", idem_key=""):
        h = hdr(tok)
        if idem_key:
            h["X-Idempotency-Key"] = idem_key
        return client.post("/hazard/confirm",
                           json={"request_id": rid, "user_id": user_id}, headers=h)

    try:
        # ══ R1 跨主体确认（无键路径）：403 + 五业务表零漂移 + 恰一条拒绝行 ══════
        b = db_snapshot()
        r = confirm(RID, TOKEN_B, "u-B")
        record("R1|他人(u-B)确认 u-A 建议 → 403", r.status_code == 403,
               f"code={r.status_code} body={r.text[:120]}")
        check_rejection_row("R1|五业务表零漂移 + audit_log 恰一条 hazard_confirm.rejected 行"
                            "（actor/target/reason/request_id 逐字段钉死，无建议正文）",
                            b, {"user_id": "u-B", "action": "hazard_confirm.rejected",
                                "target": RID, "reason": "cross_subject", "request_id": RID})

        # ══ R2 匿名主体确认：403 + 一条拒绝行（actor=anonymous）════════════════
        b = db_snapshot()
        r = confirm(RID, "")
        record("R2|匿名主体确认 → 403", r.status_code == 403,
               f"code={r.status_code} body={r.text[:120]}")
        check_rejection_row("R2|拒绝行 actor=anonymous（未解析主体如实留痕）",
                            b, {"user_id": "anonymous", "action": "hazard_confirm.rejected",
                                "target": RID, "reason": "cross_subject"})

        # ══ R3 幂等键路径跨主体：403 + 一条拒绝行 ═════════════════════════════
        b = db_snapshot()
        r = confirm(RID, TOKEN_B, "u-B", idem_key="arx-idem-1")
        record("R3|幂等键+他人令牌确认 → 403", r.status_code == 403,
               f"code={r.status_code} body={r.text[:120]}")
        check_rejection_row("R3|幂等 forbidden 路径同样留一条拒绝行（事务回滚零业务行）",
                            b, {"user_id": "u-B", "action": "hazard_confirm.rejected",
                                "target": RID, "reason": "cross_subject"})

        # ══ 夹具：归属人确认建单并推进到 feedback（R4/R5 的靶单）═══════════════
        r = confirm(RID, TOKEN_A, "u-A")
        tid = (r.json() or {}).get("ticket_id", 0) if r.status_code == 200 else 0
        record("夹具|归属人(u-A)确认 → 200 建单（拒绝不消费建议，正向闭环 = R6）",
               r.status_code == 200 and (r.json() or {}).get("ok") is True and tid > 0,
               f"code={r.status_code} body={r.text[:120]}")
        for stage in ("discuss", "execute", "feedback"):
            r = client.post(f"/tickets/{tid}/transition",
                            json={"to_stage": stage, "actor": "u-ADM", "note": ""},
                            headers=ahdr(TOKEN_ADM))
            if r.status_code != 200:
                break
        record("夹具|管理员把工单推进到 feedback（accept/reject 的合法前置阶段）",
               r.status_code == 200, f"code={r.status_code} body={r.text[:160]}")

        # ══ R4 reporter 主体 accept 流转 → 403 + 一条 ticket_transition.rejected ═
        b = db_snapshot()
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-A", "note": "越权探针"},
                        headers=ahdr(TOKEN_A))
        record("R4|reporter 主体 accept 流转 → 403", r.status_code == 403,
               f"code={r.status_code} body={r.text[:120]}")
        check_rejection_row("R4|拒绝行 action=ticket_transition.rejected、target=工单id、"
                            "reason=verifier_role_required",
                            b, {"user_id": "u-A", "action": "ticket_transition.rejected",
                                "target": str(tid), "reason": "verifier_role_required",
                                "request_id": ""})

        # ══ R5 reporter 主体旧入口 close → 403 + 一条 ticket_close.rejected ═════
        b = db_snapshot()
        r = client.post(f"/tickets/{tid}/close",
                        json={"note": "越权探针"}, headers=ahdr(TOKEN_A))
        record("R5|reporter 主体 close → 403", r.status_code == 403,
               f"code={r.status_code} body={r.text[:120]}")
        check_rejection_row("R5|拒绝行 action=ticket_close.rejected、target=工单id",
                            b, {"user_id": "u-A", "action": "ticket_close.rejected",
                                "target": str(tid), "reason": "verifier_role_required"})

        # ══ R7 审计写失败不破坏拒绝语义（fail-safe）════════════════════════════
        rid7 = "RID-ARX-7"
        audit.save_hazard_suggestion(rid7, "u-A", f"conv-{rid7}", sug_payload())
        b = db_snapshot()
        original = audit.record_auth_rejection
        def _boom(*a, **k):
            raise RuntimeError("注入的审计写故障")
        audit.record_auth_rejection = _boom
        try:
            r = confirm(rid7, TOKEN_B, "u-B")
        finally:
            audit.record_auth_rejection = original
        record("R7|record_auth_rejection 注入异常 → 仍 403（拒绝语义不被审计故障改写）",
               r.status_code == 403, f"code={r.status_code} body={r.text[:160]}")
        record("R7|注入故障期间零审计行、五业务表零漂移",
               not db_delta(b, db_snapshot()),
               f"delta={db_delta(b, db_snapshot())}")

        # ══ R8 负控：同主体重放已处理建议 → 404 且六表零漂移（非越权拒绝不写行）══
        b = db_snapshot()
        r = confirm(RID, TOKEN_A, "u-A")
        record("R8|同主体重放已处理建议 → 404（建议一次性既有契约）",
               r.status_code == 404, f"code={r.status_code} body={r.text[:120]}")
        record("R8|重放六表零漂移（重放不是越权拒绝，不落审计行）",
               not db_delta(b, db_snapshot()),
               f"delta={db_delta(b, db_snapshot())}")

        # ══ 收口 ══════════════════════════════════════════════════════════════
        record("收口|R1-R8 全部通过（403 越权写=五业务表零漂移+恰一条拒绝行；"
               "非越权拒绝零漂移；审计故障不改写拒绝语义）",
               len(RESULTS) >= 10 and all(RESULTS),
               f"checks={len(RESULTS)} failed={RESULTS.count(False)}")
    finally:
        try:
            audit.close()
        except Exception:
            pass
        api_main._audit = None
        shutil.rmtree(str(TMP), ignore_errors=True)

    failed = RESULTS.count(False)
    total = len(RESULTS)
    print(f"\n{total - failed}/{total} PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
