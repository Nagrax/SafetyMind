# -*- coding: utf-8 -*-
# SM-21：工单阶段动作矩阵（六状态全边 + 乐观并发 + transition 读写矩阵 + ?dev=1 零授权）——门禁层 G1/G2。
# 设计：scratch/sm-loop/handoff/SM-21/run-wf/decision.md（R1-R5，红灯先行，先于实现落盘）。
#
# 覆盖（全部走 FastAPI TestClient 真实 HTTP 端点，不起真实服务、零 LLM、零网络）：
#   A. 六状态 × 全边矩阵：6 同阶段 + 6×5 跨阶段 + 未知阶段 —— 7 条允许边逐条真实流转成功
#      （状态/时间线/allowed_stages 派生逐项断言），29 条禁止格全部 400 且状态零变化；
#   B. 过期页面提交：expected_from_stage 与服务端当前阶段不一致 → 409 + kind=stage_conflict
#      （零写动作，DB 六表零变化）；按返回的当前阶段重发 → 成功且 (from,to) 只落一条
#      stage_change（可恢复且不重复变更）；旧客户端（不带 expected）在工单已移动后仍被白名单拒绝；
#   C. transition 读写矩阵（SAFETYMIND_DEPLOYED=1 + SAFETYMIND_USERS）：无访问令牌(外墙)/
#      匿名(仅访问令牌)/本人(reporter 无管理令牌)/本人+管理令牌(reporter 主体)/他人+管理令牌/
#      verifier 主体/管理员主体/撤权主体 —— accept/reject 的角色边界与中间阶段的现有契约逐格钉死，
#      越权写探针断言 DB 六表零变化；
#   D. ?dev=1 不授予权限：携带 ?dev=1 的越权请求全部照旧 401/403/found=false，
#      正确鉴权下 dev 参数无害（服务端不读该参数）。
#
# 快照闸门注意：accept/reject→feedback 终态回退受 SM-20 mandate 续期校验；夹具工单一律经
# store 级 create_ticket(mandate=...) 绑定目录内主体（u-REP），回退格才可达。
# 铁律：存储全部走一次性临时目录，不触碰生产数据目录与仓库 data/；环境不可用退出码 2。
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TOKEN_REP, TOKEN_B, TOKEN_VER, TOKEN_ADM, TOKEN_C = (
    "sm21-tok-rep", "sm21-tok-b", "sm21-tok-ver", "sm21-tok-adm", "sm21-tok-c")
ACCESS, ADMIN = "sm21-access-1", "sm21-admin-1"
USERS_FULL = [
    {"token": TOKEN_REP, "subject_id": "u-REP", "roles": ["reporter"], "scopes": ["plant-a"]},
    {"token": TOKEN_B, "subject_id": "u-B", "roles": ["reporter"], "scopes": ["plant-b"]},
    {"token": TOKEN_VER, "subject_id": "u-VER", "roles": ["verifier"], "scopes": ["plant-a"]},
    {"token": TOKEN_ADM, "subject_id": "u-ADM", "roles": ["admin"], "scopes": ["*"]},
    {"token": TOKEN_C, "subject_id": "u-C", "roles": ["reporter"], "scopes": ["plant-c"]},
]
# 六状态与允许边（期望值独立于实现写死；实现必须逐格对齐 core/audit_store._STAGE_TRANSITIONS）
STAGES = ("brief", "discuss", "execute", "feedback", "accept", "reject")
ALLOWED = {
    "brief": {"discuss"},
    "discuss": {"execute"},
    "execute": {"feedback"},
    "feedback": {"accept", "reject"},
    "accept": {"feedback"},
    "reject": {"feedback"},
}
ALLOWED_STAGES_ORDERED = {
    "brief": ["discuss"], "discuss": ["execute"], "execute": ["feedback"],
    "feedback": ["accept", "reject"], "accept": ["feedback"], "reject": ["feedback"],
}
WALK = {
    "brief": [],
    "discuss": ["discuss"],
    "execute": ["discuss", "execute"],
    "feedback": ["discuss", "execute", "feedback"],
    "accept": ["discuss", "execute", "feedback", "accept"],
    "reject": ["discuss", "execute", "feedback", "reject"],
}
TABLES = ["audit_log", "upgrade_events", "tickets", "hazard_suggestions",
          "ticket_timeline", "notification_outbox"]
RID_SUG = "RID-SM21-SUG"   # 本人确认建议格的靶子

RESULTS = []
CELLS = []
TMP = Path(tempfile.mkdtemp(prefix="sm21_stage_matrix_", dir="D:/claudecode/scratch/sm-loop"))


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def cell(name, ok, detail=""):
    CELLS.append(bool(ok))
    record(name, ok, detail)


def hdr(user_token=""):
    h = {"X-Access-Token": ACCESS}
    if user_token:
        h["X-User-Token"] = user_token
    return h


def ahdr(user_token=TOKEN_ADM):
    h = hdr(user_token)
    h["X-Admin-Token"] = ADMIN
    return h


def db_snapshot():
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
    """快照之后新落库的 audit_log 行（按快照前后总数差取最新若干行）。"""
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
    return list(reversed(rows))


def check_transition_rejection(before, want_actor, want_target):
    """越权流转断言（2026-11-09 拒绝审计落地后口径，RF-3 已修复）：五业务表零漂移 +
    audit_log 恰一条 ticket_transition.rejected 行（actor=服务端解析主体、target=工单 id）
    ——拒绝也是授权事件（设计 scratch/sm-loop/handoff/SM-20/run-wf/mandate-contract.md §4）。
    旧口径"DB 零变化"是拒绝不留痕时代的最严格形态；审计落地后"五业务表零漂移+恰一条
    授权事件行"是新的最严格形态。返回 (ok, detail) 由调用方落格断言。"""
    delta = db_delta(before, db_snapshot())
    rows = audit_rows_between(before)
    ok = set(delta) == {"audit_log"} and len(rows) == 1 \
        and rows[0].get("action") == "ticket_transition.rejected" \
        and rows[0].get("user_id") == want_actor \
        and rows[0].get("target") == str(want_target) \
        and rows[0].get("reason") == "verifier_role_required"
    detail = f"delta={delta} row={rows[:1]}"
    return ok, detail


def sug_payload(summary, area):
    return {
        "decision": {"action": "suggest", "severity": "major", "summary": summary,
                     "dept": "设备动力部", "deadline_hours": 72, "basis": "SM-21 阶段矩阵夹具"},
        "brief": {"intent": "hazard_report", "urgency": "medium",
                  "entities": {"area": [area]}, "message_digest": summary,
                  "routing_reason": "sm21-stage-matrix-fixture"},
    }


def main():
    # 环境必须先于 import 钉死（api.main 导入时 load_dotenv 不覆盖已显式设置的变量）
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_EMERGENCY_FASTPATH": "1",
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
        from core import admission as admission_mod
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

    def make_mandated_ticket(tag):
        """store 级夹具：绑定目录内主体（u-REP）的快照单（终态回退格才可达——SM-20 续期闸门）。"""
        mandate = admission_mod.mandate_snapshot(
            source_type="hazard_confirm", strength="explicit", subject_id="u-REP",
            scopes=["plant-a"], rule_id="sm21.fixture", basis="confirm",
            proposal_id=f"sm21-{tag}", evidence_ids=["sm21-fixture"])
        return audit.create_ticket(f"SM-21 阶段矩阵工单 {tag}", type_="hazard",
                                   user_id="u-REP", mandate=mandate)

    def walk(tid, stage):
        for s in WALK[stage]:
            r = client.post(f"/tickets/{tid}/transition",
                            json={"to_stage": s, "actor": "u-ADM",
                                  "note": "矩阵步进" if s == "accept" else ""},
                            headers=ahdr())
            assert r.status_code == 200, f"夹具步进失败 {tid}→{s}: {r.status_code} {r.text[:160]}"

    def timeline_of(tid):
        r = client.get(f"/tickets/{tid}/timeline", headers=ahdr())
        assert r.status_code == 200, f"timeline 读取失败: {r.status_code}"
        return r.json()

    def stage_changes(timeline, frm="", to=""):
        return [e for e in timeline["timeline"]
                if e.get("action") == "stage_change"
                and (not frm or e.get("from_stage") == frm)
                and (not to or e.get("to_stage") == to)]

    try:
        # ══ A. 六状态 × 全边矩阵（37 格）══════════════════════════════════════
        # 每个 from_stage 独立工单：允许边消耗本格工单，禁止格断言后状态/时间线零变化。
        unknown_probed = False
        for frm in STAGES:
            tickets = {to: make_mandated_ticket(f"edge-{frm}-{to}") for to in STAGES if to != frm}
            for tid in tickets.values():
                walk(tid, frm)
            # 每阶段的 allowed_stages 派生格（读管理端 timeline 的 ticket 面）
            probe_tid = next(iter(tickets.values()))
            tl = timeline_of(probe_tid)
            cell(f"阶段矩阵|allowed_stages|{frm}",
                 tl["ticket"].get("allowed_stages") == ALLOWED_STAGES_ORDERED[frm],
                 f"got={tl['ticket'].get('allowed_stages')} "
                 f"want={ALLOWED_STAGES_ORDERED[frm]}")
            for to in STAGES:
                if to == frm:
                    tid = next(iter(tickets.values()))
                    before = db_snapshot()
                    n_tl = len(timeline_of(tid)["timeline"])
                    r = client.post(f"/tickets/{tid}/transition",
                                    json={"to_stage": to, "actor": "u-ADM",
                                          "note": "同阶段探针"}, headers=ahdr())
                    ok = (r.status_code == 400 and "已处于" in str(r.json().get("detail", ""))
                          and len(timeline_of(tid)["timeline"]) == n_tl
                          and not db_delta(before, db_snapshot()))
                    cell(f"阶段矩阵|同阶段|{frm}→{frm} → 400 且零变化", ok,
                         f"code={r.status_code} body={r.text[:120]}")
                    continue
                tid = tickets[to]
                before = db_snapshot()
                r = client.post(f"/tickets/{tid}/transition",
                                json={"to_stage": to, "actor": "u-ADM",
                                      "note": "矩阵验收" if to == "accept" else ""},
                                headers=ahdr())
                if to in ALLOWED[frm]:
                    body = r.json() if r.status_code == 200 else {}
                    tk = body.get("ticket") or {}
                    want_status = "closed" if to in ("accept", "reject") else "open"
                    ok = (r.status_code == 200 and body.get("ok") is True
                          and tk.get("stage") == to and tk.get("status") == want_status
                          and tk.get("allowed_stages") == ALLOWED_STAGES_ORDERED[to]
                          and len(stage_changes(timeline_of(tid), frm, to)) == 1)
                    cell(f"阶段矩阵|允许边|{frm}→{to} → 200 且状态/时间线/allowed_stages 正确",
                         ok, f"code={r.status_code} body={r.text[:160]}")
                else:
                    ok = (r.status_code == 400
                          and not db_delta(before, db_snapshot()))
                    cell(f"阶段矩阵|禁止边|{frm}→{to} → 400 且零变化", ok,
                         f"code={r.status_code} body={r.text[:120]}")
                if not unknown_probed:
                    before_u = db_snapshot()
                    ru = client.post(f"/tickets/{tid}/transition",
                                     json={"to_stage": "flying", "actor": "u-ADM"},
                                     headers=ahdr())
                    ok = (ru.status_code == 400
                          and "未知阶段" in str(ru.json().get("detail", ""))
                          and not db_delta(before_u, db_snapshot()))
                    cell("阶段矩阵|未知阶段|→flying → 400 未知阶段", ok,
                         f"code={ru.status_code} body={ru.text[:120]}")
                    unknown_probed = True

        # ══ C. transition 读写矩阵（在 feedback 靶单上逐格；越权写探针断言 DB 零变化）══
        def feedback_target(tag):
            tid = make_mandated_ticket(tag)
            walk(tid, "feedback")
            return tid

        # C1 外墙：无访问令牌 → 401
        tid = feedback_target("c1")
        b = db_snapshot()
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-REP", "note": "越权探针"})
        cell("transition 矩阵|外墙|无访问令牌 → 401 且 DB 零变化",
             r.status_code == 401 and not db_delta(b, db_snapshot()), f"code={r.status_code}")

        # C2 匿名（仅访问令牌，无管理令牌）→ 401
        b = db_snapshot()
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-REP", "note": "越权探针"},
                        headers=hdr())
        cell("transition 矩阵|匿名(仅访问令牌) → 401 且 DB 零变化",
             r.status_code == 401 and not db_delta(b, db_snapshot()), f"code={r.status_code}")

        # C3 本人（reporter 主体，无管理令牌）→ 401（流转不是用户动作，即使是自己的单）
        b = db_snapshot()
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-REP", "note": "越权探针"},
                        headers=hdr(TOKEN_REP))
        cell("transition 矩阵|本人(reporter 无管理令牌) → 401 且 DB 零变化",
             r.status_code == 401 and not db_delta(b, db_snapshot()), f"code={r.status_code}")

        # C4 管理令牌 + reporter 主体 → accept 403（处理者不能验收）
        b = db_snapshot()
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-REP", "note": "越权探针"},
                        headers=ahdr(TOKEN_REP))
        c4_ok, c4_detail = check_transition_rejection(b, "u-REP", tid)
        cell("transition 矩阵|本人(reporter+管理令牌) → accept 403 且五业务表零漂移+恰一条拒绝行",
             r.status_code == 403 and c4_ok,
             f"code={r.status_code} body={r.text[:120]} {c4_detail}")

        # C5 管理令牌 + 他人(reporter u-B) 主体 → accept 403
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-B", "note": "越权探针"},
                        headers=ahdr(TOKEN_B))
        cell("transition 矩阵|他人(reporter+管理令牌) → accept 403",
             r.status_code == 403, f"code={r.status_code}")

        # C6 管理令牌 + verifier 主体 → accept 空批注 400；带批注 200（正向对照）
        b = db_snapshot()
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-VER", "note": "  "},
                        headers=ahdr(TOKEN_VER))
        cell("transition 矩阵|verifier|accept 空批注 → 400 且 DB 零变化",
             r.status_code == 400 and not db_delta(b, db_snapshot()),
             f"code={r.status_code} body={r.text[:120]}")
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-VER", "note": "整改到位，验收通过"},
                        headers=ahdr(TOKEN_VER))
        body = r.json() if r.status_code == 200 else {}
        cell("transition 矩阵|verifier|accept 带批注 → 200 且 closed（正向对照）",
             r.status_code == 200 and body.get("ticket", {}).get("stage") == "accept"
             and body.get("ticket", {}).get("status") == "closed",
             f"code={r.status_code} body={r.text[:160]}")

        # C7 管理令牌 + admin 主体 → reject 正向（feedback→reject）
        tid = feedback_target("c7")
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "reject", "actor": "u-ADM", "note": "复核不合格"},
                        headers=ahdr())
        body = r.json() if r.status_code == 200 else {}
        cell("transition 矩阵|管理员主体|reject → 200 且 closed（正向对照）",
             r.status_code == 200 and body.get("ticket", {}).get("stage") == "reject",
             f"code={r.status_code} body={r.text[:160]}")

        # C8 撤权：verifier 凭据从目录移除后，accept 立即 403
        os.environ["SAFETYMIND_USERS"] = json.dumps(
            [u for u in USERS_FULL if u["token"] != TOKEN_VER], ensure_ascii=False)
        tid = feedback_target("c8")
        b = db_snapshot()
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-VER", "note": "撤权后探针"},
                        headers=ahdr(TOKEN_VER))
        c8_ok, c8_detail = check_transition_rejection(b, "anonymous", tid)
        cell("transition 矩阵|撤权(verifier 移出目录) → accept 403 且五业务表零漂移+恰一条拒绝行",
             r.status_code == 403 and c8_ok,
             f"code={r.status_code} body={r.text[:120]} {c8_detail}")
        os.environ["SAFETYMIND_USERS"] = json.dumps(USERS_FULL, ensure_ascii=False)

        # C9 中间阶段推进：reporter 主体 + 管理令牌 → 200（现状契约：中间阶段只认管理令牌）
        tid = make_mandated_ticket("c9")
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "discuss", "actor": "u-REP", "note": ""},
                        headers=ahdr(TOKEN_REP))
        cell("transition 矩阵|中间阶段|reporter+管理令牌 brief→discuss → 200（现状契约）",
             r.status_code == 200, f"code={r.status_code} body={r.text[:120]}")

        # C10 store 级 allowed_transitions 派生（additive 字段的唯一出处）
        tid = make_mandated_ticket("c10")
        tk = audit.get_ticket(tid)
        cell("store 级|get_ticket(brief).allowed_stages == ['discuss']",
             tk is not None and tk.get("allowed_stages") == ["discuss"],
             f"got={tk and tk.get('allowed_stages')}")

        # ══ B. 过期页面提交冲突：可恢复且不重复变更 ═════════════════════════════
        tid = feedback_target("b1")
        # B1 页面过期（视图停在 execute，实际已 feedback）→ 409 + 实际阶段，DB 零变化
        b = db_snapshot()
        n_tl = len(timeline_of(tid)["timeline"])
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-ADM", "note": "过期页面提交",
                              "expected_from_stage": "execute"},
                        headers=ahdr())
        try:
            detail = r.json().get("detail") or {}
        except Exception:
            detail = {}
        cell("过期页面|stale expected_from_stage=execute → 409 stage_conflict 且零变化",
             r.status_code == 409 and detail.get("kind") == "stage_conflict"
             and detail.get("from_stage") == "feedback" and detail.get("expected") == "execute"
             and len(timeline_of(tid)["timeline"]) == n_tl
             and not db_delta(b, db_snapshot()),
             f"code={r.status_code} detail={str(detail)[:160]}")

        # B2 恢复：按 409 给出的当前阶段重发 → 200，(feedback→accept) 恰一条 stage_change
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "accept", "actor": "u-ADM", "note": "刷新后重提",
                              "expected_from_stage": "feedback"},
                        headers=ahdr())
        tl = timeline_of(tid)
        cell("过期页面|恢复重提 expected=feedback → 200 且 (feedback→accept) 恰一条 stage_change",
             r.status_code == 200 and len(stage_changes(tl, "feedback", "accept")) == 1
             and tl["ticket"].get("stage") == "accept",
             f"code={r.status_code} body={r.text[:160]} stage_changes={len(stage_changes(tl, 'feedback', 'accept'))}")

        # B3 旧客户端（不带 expected）：工单已 accept 后提交过期目标 discuss → 400 零变化
        b = db_snapshot()
        r = client.post(f"/tickets/{tid}/transition",
                        json={"to_stage": "discuss", "actor": "u-ADM", "note": "旧客户端过期提交"},
                        headers=ahdr())
        cell("过期页面|旧客户端(无 expected)提交已过期目标 → 400 且零变化",
             r.status_code == 400 and not db_delta(b, db_snapshot()),
             f"code={r.status_code} body={r.text[:120]}")

        # ══ D. ?dev=1 不授予权限 ═══════════════════════════════════════════════
        b = db_snapshot()
        r = client.get("/tickets?dev=1", headers=hdr())
        cell("dev=1|GET /tickets?dev=1 匿名 → 401", r.status_code == 401, f"code={r.status_code}")

        tid = make_mandated_ticket("d2")
        b = db_snapshot()
        r = client.patch(f"/tickets/{tid}?dev=1", json={"severity": "general"},
                         headers=hdr(TOKEN_REP))
        cell("dev=1|PATCH /tickets/{id}?dev=1 reporter → 401 且 DB 零变化",
             r.status_code == 401 and not db_delta(b, db_snapshot()), f"code={r.status_code}")

        audit.save_hazard_suggestion(RID_SUG, "u-REP", "conv-sm21-1",
                                     sug_payload("戊区灭火器压力表归零", "戊区"))
        b = db_snapshot()
        r = client.post("/hazard/confirm?dev=1",
                        json={"request_id": RID_SUG, "user_id": "u-REP"}, headers=hdr())
        d1_rows = audit_rows_between(b)
        cell("dev=1|POST /hazard/confirm?dev=1 匿名(仅访问令牌) → 403 且五业务表零漂移+恰一条拒绝行",
             r.status_code == 403
             and set(db_delta(b, db_snapshot())) == {"audit_log"}
             and len(d1_rows) == 1
             and d1_rows[0].get("action") == "hazard_confirm.rejected"
             and d1_rows[0].get("user_id") == "anonymous"
             and d1_rows[0].get("target") == RID_SUG,
             f"code={r.status_code} body={r.text[:120]} rows={d1_rows[:1]}")

        r = client.get(f"/tickets/progress/{RID_SUG}?dev=1", headers=hdr())
        cell("dev=1|GET /tickets/progress?dev=1 匿名(仅访问令牌) → 200 且 found=false",
             r.status_code == 200 and r.json().get("found") is False,
             f"code={r.status_code} body={r.text[:120]}")

        r = client.get("/tickets?dev=1&limit=5", headers=ahdr())
        cell("dev=1|正确鉴权下 dev 参数无害 → 200", r.status_code == 200,
             f"code={r.status_code}")

        # ══ 收口：矩阵格计数与全绿 ════════════════════════════════════════════
        # 预期格数：A=6×(1 派生 + 1 同阶段 + 5 跨阶段) + 未知 1 = 43；C=11（C6 含空批注与
        # 正向对照两格）；B=3；D=5 → 62 格 + 收口自检 1 格
        closure_ok = len(CELLS) == 62 and all(CELLS)
        CELLS.append(closure_ok)
        record("矩阵收口|43 阶段格 + 11 transition 主体格 + 3 过期页面格 + 5 dev=1 格 = 62 格全格断言",
               closure_ok, f"cells={len(CELLS) - 1} all_ok={all(CELLS[:-1])}")
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
