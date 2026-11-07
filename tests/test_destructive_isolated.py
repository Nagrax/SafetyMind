# -*- coding: utf-8 -*-
# SM-01 第三片：破坏性动作在 runner 隔离库上真执行（G1+G2 门禁层）。
# 设计：scratch/sm-loop/handoff/SM-01/slice3-design.md
# 机制移植自 CarlcareDOC backend/scripts/destructive_eval.py（"确认之后到底发生了什么"
# 只能在可丢弃的库上真跑），代码为本仓库独立实现，不搬代码。
#
# 与 tests/test_role_matrix_api.py 的分工：矩阵逐格验"谁能动"，本文件验"动了之后库
# 里真实发生了什么"——同一确认的幂等重放、过期确认的冲突口径与可回放、跨主体确认、
# 凭据撤销后的操作、重复建单去重，每个用例前后做 DB 快照对比，断言零意外漂移。
#
# 安全边界（守卫在文件最顶部，先于任何产品导入）：
#   - AUDIT_DB_PATH 必须处于隔离目录形态（路径中含 iso 目录组件，即 runner 的
#     <run-root>/iso/<tag>/audit.db；与 baseline_run.py 的 STORAGE_ENV_KEYS
#     "必须落在 run-root 之内"判据等价——iso 目录本身就是 run-root 的子目录）；
#   - 不满足直接 exit 2（环境不可用/非隔离），绝不连到真实库上跑破坏性序列，
#     也绝不把环境故障伪装成业务失败；
#   - 存储全部落在隔离库所在目录（conversations/chroma 缺省时钉到 audit.db 同级），
#     不清理 iso 目录（一次运行内多个脚本共享，整个 iso 目录由 runner 丢弃）。
#
# 用例（D=快照对比断言零意外漂移）：
#   S1 同一确认幂等重放（同收据）：重复 POST /hazard/confirm 同一 request_id → 404
#      （建议一次性，既有契约）且不产生第二张工单、六表零漂移；
#   S2 过期版本确认返回冲突且冲突行可回放：确认已过 24h 的建议 → 409 + 建议行落
#      expired 痕（不是永远滞留 pending）；重新上报同收据后可正常确认建单；
#   S3 跨主体确认被拒：他人令牌确认本人建议 → 403，伪造 body.user_id 也不能越权，
#      六表零漂移；
#   S4 撤权后操作被拒：主体目录移除令牌后确认 → 403（令牌即匿名），六表零漂移，
#      建议保持 pending 可被后续合法流程处理；
#   S5 重复建单去重：同用户 24h 内同实体第二次确认 → merged=true 合并既有单，
#      不新建工单（tickets 表零新增），时间线留 repeat_report 事件。
#
# 用法: 由 runner 执行（heavy 组）；手动复跑须自设隔离形态 AUDIT_DB_PATH，例如
#   set AUDIT_DB_PATH=D:/claudecode/scratch/<run-root>/iso/<tag>/audit.db
# 退出码：0 = 全部通过；1 = 有断言失败；2 = 环境不可用或非隔离库。
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# ── 守卫：非隔离库形态直接拒跑（先于一切产品导入，绝不触碰真实数据）─────────
def _iso_guard() -> str:
    raw = (os.environ.get("AUDIT_DB_PATH") or "").strip()
    if not raw:
        return "AUDIT_DB_PATH 未设置（本文件只允许在 runner 隔离库上真执行）"
    try:
        parts = [p.lower() for p in Path(os.path.abspath(raw)).parts]
    except Exception as ex:
        return f"AUDIT_DB_PATH 不可解析: {ex}"
    if "iso" not in parts:
        return ("AUDIT_DB_PATH 不在隔离目录形态（路径中无 iso 目录组件）: "
                f"{raw} —— 破坏性序列只允许在 <run-root>/iso/<tag>/ 下的库真执行")
    return ""


_GUARD_MSG = _iso_guard()
if _GUARD_MSG:
    print(f"[REJECT] {_GUARD_MSG}")
    sys.exit(2)

AUDIT_DB = os.environ["AUDIT_DB_PATH"]
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# 主体目录（撤权 = 从目录移除令牌；产品逐请求解析 env，即时生效）
TOKEN_A, TOKEN_B, TOKEN_C = "sm01-tok-a", "sm01-tok-b", "sm01-tok-c"
USERS_FULL = [
    {"token": TOKEN_A, "subject_id": "u-A", "roles": ["reporter"], "scopes": ["plant-a"]},
    {"token": TOKEN_B, "subject_id": "u-B", "roles": ["reporter"], "scopes": ["plant-b"]},
    {"token": TOKEN_C, "subject_id": "u-C", "roles": ["reporter"], "scopes": ["plant-c"]},
]

# 本人确认成功后的业务副作用（逐表钉死：多一表少一表都算行为漂移）
CONFIRM_DELTA = {"hazard_suggestions", "tickets", "ticket_timeline", "notification_outbox"}
MERGE_DELTA = {"hazard_suggestions", "ticket_timeline"}   # 去重合并：不新建工单
EXPIRE_DELTA = {"hazard_suggestions"}                     # 过期冲突：只落 expired 痕
TABLES = ["audit_log", "upgrade_events", "tickets", "hazard_suggestions",
          "ticket_timeline", "notification_outbox"]
STAMP = time.strftime("%H%M%S")   # 收据按运行戳隔离，共享隔离库内互不串扰

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def hdr(user_token=""):
    return {"X-Access-Token": os.environ["ACCESS_TOKEN"], "X-User-Token": user_token}


def db_snapshot():
    """六表全量快照（只读连接，canonical JSON）：写前后对比的唯一依据。"""
    con = sqlite3.connect(f"file:{AUDIT_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {t: json.dumps([dict(r) for r in con.execute(f"SELECT * FROM {t}")],
                              sort_keys=True, ensure_ascii=False, default=str)
                for t in TABLES}
    finally:
        con.close()


def db_delta(before, after):
    return sorted(t for t in TABLES if before[t] != after[t])


def sug_payload(summary, area):
    return {
        "decision": {"action": "suggest", "severity": "major", "summary": summary,
                     "dept": "设备动力部", "deadline_hours": 72, "basis": "破坏性演练夹具"},
        "brief": {"intent": "hazard_report", "urgency": "medium",
                  "entities": {"area": [area]}, "message_digest": summary,
                  "routing_reason": "sm01-destructive-drill"},
    }


def backdate_suggestion(request_id: str, hours: float) -> None:
    """把建议的落库时间回拨（在可丢弃的隔离库上真改，模拟 24h 前的上报）。"""
    con = sqlite3.connect(AUDIT_DB, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("UPDATE hazard_suggestions SET created_ts=? WHERE request_id=?",
                    (time.time() - hours * 3600, request_id))
        con.commit()
    finally:
        con.close()


def main():
    # ── 环境必须先于 import 钉死（api.main 导入时 load_dotenv 不覆盖已显式设置的变量）──
    iso_dir = Path(AUDIT_DB).parent
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
        "SAFETYMIND_OUTBOX_ENABLED": "0",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "SAFETYMIND_CONVERSATIONS_DIR": os.environ.get("SAFETYMIND_CONVERSATIONS_DIR")
                                       or str(iso_dir / "conversations"),
        "CHROMA_HOST": "127.0.0.1",
        "CHROMA_PORT": "8973",
        "CHROMA_PERSIST_DIRECTORY": os.environ.get("CHROMA_PERSIST_DIRECTORY")
                                    or str(iso_dir / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": os.environ.get("CHROMA_KB_PERSIST_DIRECTORY")
                                       or str(iso_dir / "kb_chroma"),
        "PROMETHEUS_PORT": "0",
        "AUDIT_ENABLED": "1",
        "SAFETYMIND_DEPLOYED": "1",
        "ACCESS_TOKEN": "sm01-destr-access",
        "ADMIN_TOKEN": "sm01-destr-admin",
        "API_HOST": "0.0.0.0",
    })
    os.environ["SAFETYMIND_USERS"] = json.dumps(USERS_FULL, ensure_ascii=False)
    os.environ.setdefault("AUDIT_DB_PATH", AUDIT_DB)

    try:
        from fastapi.testclient import TestClient
    except Exception as ex:
        print(f"[REJECT] 环境不可用（fastapi TestClient 缺失）: {ex}")
        return 2
    try:
        from core.audit_store import AuditStore
        import api.main as api_main
    except Exception as ex:
        print(f"[REJECT] 环境不可用（产品模块导入失败）: {type(ex).__name__}: {ex}")
        return 2
    try:
        audit = AuditStore(db_path=AUDIT_DB)   # runner 隔离库（守卫已确保 iso 形态）
    except Exception as ex:
        print(f"[REJECT] 环境不可用（隔离审计库创建失败）: {type(ex).__name__}: {ex}")
        return 2

    client = TestClient(api_main.app)   # 不进 lifespan：夹具组件即全部依赖
    api_main._audit = audit

    def confirm(rid, tok, user_id):
        return client.post("/hazard/confirm",
                           json={"request_id": rid, "user_id": user_id}, headers=hdr(tok))

    def tickets_for(rid):
        con = sqlite3.connect(f"file:{AUDIT_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return con.execute("SELECT COUNT(*) AS n FROM tickets WHERE source_request_id=?",
                               (rid,)).fetchone()["n"]
        finally:
            con.close()

    try:
        # ══ S1 同一确认幂等重放（同收据）══════════════════════════════════════
        rid1 = f"SM01-DESTR-S1-{STAMP}"
        audit.save_hazard_suggestion(rid1, "u-A", f"conv-{rid1}",
                                     sug_payload("甲区灭火器压力表归零", "甲区"))
        b = db_snapshot()
        r1 = confirm(rid1, TOKEN_A, "u-A")
        body1 = r1.json() if r1.status_code == 200 else {}
        t1 = (body1.get("ticket_id") or 0)
        delta1 = db_delta(b, db_snapshot())
        record("S1|本人确认 → 200 建单且四表精确落库",
               r1.status_code == 200 and body1.get("ok") is True
               and body1.get("merged") is False and t1 > 0 and set(delta1) == CONFIRM_DELTA,
               f"code={r1.status_code} body={r1.text[:120]} delta={delta1}")
        b = db_snapshot()
        r2 = confirm(rid1, TOKEN_A, "u-A")
        delta2 = db_delta(b, db_snapshot())
        record("S1|同收据重放 → 404（建议一次性既有契约）且六表零漂移",
               r2.status_code == 404 and not delta2,
               f"code={r2.status_code} delta={delta2} body={r2.text[:120]}")
        record("S1|重放不产生第二张工单（同收据仍只有一张单）",
               tickets_for(rid1) == 1 and (audit.find_ticket_by_request(rid1) or 0) == t1,
               f"tickets_for={tickets_for(rid1)} t1={t1}")

        # ══ S2 过期版本确认返回冲突且冲突行可回放 ═════════════════════════════
        rid2 = f"SM01-DESTR-S2-{STAMP}"
        audit.save_hazard_suggestion(rid2, "u-A", f"conv-{rid2}",
                                     sug_payload("乙区配电柜绝缘垫缺失", "乙区"))
        backdate_suggestion(rid2, 25.0)   # 隔离库上真改：上报时间回拨 25h（> 24h 有效期）
        b = db_snapshot()
        r3 = confirm(rid2, TOKEN_A, "u-A")
        delta3 = db_delta(b, db_snapshot())
        row = audit.get_hazard_suggestion(rid2)
        record("S2|过期版本确认 → 409 冲突（设计口径，非 404 不存在）",
               r3.status_code == 409,
               f"code={r3.status_code} body={r3.text[:160]}")
        record("S2|过期冲突落 expired 痕（建议行不再滞留 pending）且其余表零漂移",
               row is None and set(delta3) == EXPIRE_DELTA,
               f"get_hazard_suggestion={row is not None} delta={delta3}")
        b = db_snapshot()
        r4 = confirm(rid2, TOKEN_A, "u-A")
        delta4 = db_delta(b, db_snapshot())
        record("S2|过期冲突重放 → 仍 409（幂等冲突）且六表零漂移",
               r4.status_code == 409 and not delta4,
               f"code={r4.status_code} delta={delta4}")
        audit.save_hazard_suggestion(rid2, "u-A", f"conv-{rid2}",
                                     sug_payload("乙区配电柜绝缘垫缺失（重新上报）", "乙区"))
        row2 = audit.get_hazard_suggestion(rid2)
        record("S2|冲突行可回放：重新上报同收据 → 建议复位 pending",
               row2 is not None and row2.get("request_id") == rid2,
               f"row2={'None' if row2 is None else 'pending 复位失败'}")
        b = db_snapshot()
        r5 = confirm(rid2, TOKEN_A, "u-A")
        body5 = r5.json() if r5.status_code == 200 else {}
        t2 = (body5.get("ticket_id") or 0)
        delta5 = db_delta(b, db_snapshot())
        record("S2|回放后确认 → 200 建新单（可回放闭环）且四表精确落库",
               r5.status_code == 200 and body5.get("merged") is False and t2 > 0
               and t2 != t1 and set(delta5) == CONFIRM_DELTA,
               f"code={r5.status_code} t2={t2} t1={t1} delta={delta5}")

        # ══ S3 跨主体确认被拒 ═════════════════════════════════════════════════
        rid3 = f"SM01-DESTR-S3-{STAMP}"
        audit.save_hazard_suggestion(rid3, "u-A", f"conv-{rid3}",
                                     sug_payload("丙区安全出口指示灯熄灭", "丙区"))
        for label, uid in (("他人自己的 user_id", "u-B"), ("伪造归属 user_id=u-A", "u-A")):
            b = db_snapshot()
            r = confirm(rid3, TOKEN_B, uid)
            delta = db_delta(b, db_snapshot())
            record(f"S3|跨主体确认（{label}）→ 403 且六表零漂移",
                   r.status_code == 403 and not delta,
                   f"code={r.status_code} delta={delta} body={r.text[:120]}")
        record("S3|被拒后建议保持 pending（归属人仍可正常确认）",
               audit.get_hazard_suggestion(rid3) is not None,
               "get_hazard_suggestion=None（建议被意外消费）")
        b = db_snapshot()
        r = confirm(rid3, TOKEN_A, "u-A")
        record("S3|归属人本人确认同一建议 → 200（证明 403 是归属判定而非建议损坏）",
               r.status_code == 200 and (r.json() or {}).get("ok") is True,
               f"code={r.status_code} body={r.text[:120]}")

        # ══ S4 撤权后操作被拒 ═════════════════════════════════════════════════
        rid4 = f"SM01-DESTR-S4-{STAMP}"
        audit.save_hazard_suggestion(rid4, "u-C", f"conv-{rid4}",
                                     sug_payload("丁区梯子踏板开裂", "丁区"))
        os.environ["SAFETYMIND_USERS"] = json.dumps(
            [u for u in USERS_FULL if u["token"] != TOKEN_C], ensure_ascii=False)
        b = db_snapshot()
        r = confirm(rid4, TOKEN_C, "u-C")
        delta = db_delta(b, db_snapshot())
        record("S4|撤权后确认 → 403（令牌失效即匿名）且六表零漂移",
               r.status_code == 403 and not delta,
               f"code={r.status_code} delta={delta} body={r.text[:120]}")
        record("S4|撤权后建议保持 pending（可由合法流程接手，不因撤权丢数据）",
               audit.get_hazard_suggestion(rid4) is not None,
               "get_hazard_suggestion=None（建议被意外消费）")
        b = db_snapshot()
        r = confirm(rid4, TOKEN_C, "")
        delta = db_delta(b, db_snapshot())
        record("S4|撤权后不带 user_id 重试 → 仍 403 且六表零漂移（body 不能补回身份）",
               r.status_code == 403 and not delta,
               f"code={r.status_code} delta={delta}")

        # ══ S5 重复建单去重 ═══════════════════════════════════════════════════
        rid5a = f"SM01-DESTR-S5A-{STAMP}"
        rid5b = f"SM01-DESTR-S5B-{STAMP}"
        audit.save_hazard_suggestion(rid5a, "u-A", f"conv-{rid5a}",
                                     sug_payload("戊区危化品柜未上锁", "戊区"))
        audit.save_hazard_suggestion(rid5b, "u-A", f"conv-{rid5b}",
                                     sug_payload("戊区危化品柜未上锁（重复上报）", "戊区"))
        r = confirm(rid5a, TOKEN_A, "u-A")
        body_a = r.json() if r.status_code == 200 else {}
        t5 = (body_a.get("ticket_id") or 0)
        record("S5|首报确认 → 200 新建工单",
               r.status_code == 200 and body_a.get("merged") is False and t5 > 0,
               f"code={r.status_code} body={r.text[:120]}")
        b = db_snapshot()
        r = confirm(rid5b, TOKEN_A, "u-A")
        body_b = r.json() if r.status_code == 200 else {}
        delta = db_delta(b, db_snapshot())
        merged_id = (body_b.get("ticket_id") or 0)
        tl = audit.get_ticket_timeline(t5) or []
        repeat = [e for e in tl if e.get("action") == "repeat_report"
                  and (e.get("detail") or {}).get("request_id") == rid5b]
        record("S5|同用户 24h 内同实体重复确认 → 200 merged=true 且合并到既有单",
               r.status_code == 200 and body_b.get("merged") is True and merged_id == t5,
               f"code={r.status_code} body={r.text[:120]} merged_id={merged_id} t5={t5}")
        record("S5|去重不新建工单（tickets 表零新增，b 单无自己的工单行）且两表精确落库",
               tickets_for(rid5b) == 0 and set(delta) == MERGE_DELTA,
               f"tickets_for_b={tickets_for(rid5b)} delta={delta}")
        record("S5|既有单时间线留 repeat_report 事件（含重复上报收据）",
               bool(repeat), f"timeline_actions={[e.get('action') for e in tl][-6:]}")

        # ══ 收口 ══════════════════════════════════════════════════════════════
        record("收口|五个破坏性序列全部执行（S1 幂等重放 / S2 过期冲突+回放 / "
               "S3 跨主体 / S4 撤权 / S5 去重）",
               len(RESULTS) >= 15 and all(RESULTS),
               f"checks={len(RESULTS)} failed={RESULTS.count(False)}")
    finally:
        try:
            audit.close()
        except Exception:
            pass
        api_main._audit = None
        # 隔离库不清理（一次运行内多脚本共享，整个 iso 目录由 runner 统一丢弃）

    failed = RESULTS.count(False)
    total = len(RESULTS)
    print(f"\n{total - failed}/{total} PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
