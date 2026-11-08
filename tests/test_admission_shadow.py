# -*- coding: utf-8 -*-
# SM-20 红灯测试：独立准入三档（off/observe/auto）、影子裁决与不可变授权快照。
# python tests/test_admission_shadow.py（由 runner heavy 组执行）
#
# 契约源（先于实现定死）：scratch/sm-loop/handoff/SM-20/decision.md
# 机制参考：SafetyMind/docs/对标笔记_dsh-superagent_2026-11-06.md P0-A1/P0-A2（dsh 原型
#   off/observe/auto 三档 + mandate 快照，按本仓库主体与 SQLite 独立实现）；
#   SafetyMind-落地交接-20261107/行动指南.md §2（旁路影子对照=现行写入继续+只增裁决数据）
#   与 §5 影子对照表（CRITICAL/显式转人工三档全部按现行路径处理）。
#
# 用例（红灯 = 在当前产品上真实失败；逐条记录现状）：
#   T0 旋钮契约：SAFETYMIND_ADMISSION_MODE 默认 off、非法值回 off、policy_version 默认 adm-1；
#   T1 影子（observe，E2E /chat×2）：off 与 observe 两档业务行级一致（tickets/outbox/
#       timeline/suggestions/upgrade_events），hazard_observations 是唯一增量且含
#       would_accept+定级+依据条款+命中大类+派发/限期+policy 版本+现行动作对照列；
#   T2 auto 档：model_hint 裁决拒（不建单）、intent/urgency/safety_net 准（显式转人工与
#       CRITICAL 零弱化）、CRITICAL 快速分流在 mode=auto 下建单/escalated/fastpath 全保留、
#       主体范围收窄裁决、api 建单分支接线；
#   T3 快照：tickets 加列 mandate_json/policy_version、create_ticket(mandate=) 同事务、
#       mandate_status 派生、应急快速分流/确认卡消费建单带快照、改判/合并/幂等重放零覆盖；
#   T4 续期：legacy 拒回退+留痕、有效快照回退放行、policy 换版/撤权/过期/跨主体（含 admin
#       豁免）裁决、reject→feedback 同受校验、补授权后可回退；
#   T5 escalation_basis 断言存在（确定性优先级 + 四出口 basis + ChatResponse 透传）。
#
# 预期首跑 RED：T1 影子数据、T2 裁决单元与接线、T3/T4 机制项；守卫项（业务一致、CRITICAL
# 零弱化、escalation_basis 语义）当前产品应真——防实现时弱化。
#
# 隔离：全部存储走 tempfile 临时目录（对齐 test_hazard_tx_atomic），工程目录不留残留。
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

STAMP = __import__("datetime").datetime.now().strftime("%H%M%S%f")
RESULTS = []
PV1, PV2 = "adm-1", "adm-2"

# 闸门裁决夹具（确定性的 HIGH+ auto：major/易燃易爆大类/安环部/24h）
FAKE_AUTO = {"action": "auto", "severity": "major", "category": "易燃易爆设施及场所",
             "basis": "GB 50016（sm20 红灯夹具）", "dept": "安环部", "deadline_hours": 24,
             "summary": "易燃易爆设施及场所隐患：罐区储罐底部渗漏"}

MANDATE_OK = {
    "schema_version": 1, "policy_version": PV1, "source_type": "hazard_gate_auto",
    "strength": "inferred", "subject_id": "u-A", "scopes": ["plant-a"],
    "rule_id": "hazard_gate.auto:HIGH@adm-1", "basis": "hazard_gate_auto",
    "proposal_id": "sm20-mand", "decided_ts": time.time(), "evidence_ids": ["audit:sm20-mand"],
}


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


def qall(db_path, sql, args=()):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def ticket_cols(db_path):
    return {r["name"] for r in qall(db_path, "PRAGMA table_info(tickets)")}


# ── T0/T2/T4 单元面：准入模块（当前产品缺失 → 逐条 RED）─────────────────────
def t0_t2_admission_unit():
    adm = None
    try:
        from core import admission as adm_mod
        adm = adm_mod
    except Exception as ex:
        record("T0|core/admission.py 准入模块存在（三档开关与裁决入口）",
               False, f"机制缺失（ImportError: {ex}）")

    if adm is not None:
        os.environ.pop("SAFETYMIND_ADMISSION_MODE", None)
        record("T0|mode 默认 off（默认安全=现行为）", adm.admission_mode() == "off",
               f"got={adm.admission_mode()!r}")
        os.environ["SAFETYMIND_ADMISSION_MODE"] = "junk-mode"
        record("T0|非法 mode 回退 off（fail-safe）", adm.admission_mode() == "off",
               f"got={adm.admission_mode()!r}")
        os.environ["SAFETYMIND_ADMISSION_MODE"] = "observe"
        record("T0|mode=observe 可读", adm.admission_mode() == "observe",
               f"got={adm.admission_mode()!r}")
        os.environ.pop("SAFETYMIND_ADMISSION_POLICY_VERSION", None)
        record("T0|policy_version 默认 adm-1", adm.policy_version() == "adm-1",
               f"got={adm.policy_version()!r}")
        os.environ["SAFETYMIND_ADMISSION_MODE"] = "off"   # 后续段恢复默认

        record("T2|确定性谓词：model_hint 非确定性",
               adm.deterministic_basis("model_hint") is False)
        record("T2|确定性谓词：intent/urgency/safety_net 均为确定性",
               all(adm.deterministic_basis(b) for b in ("intent", "urgency", "safety_net")))

        r = adm.adjudicate(mode="auto", basis="model_hint")
        record("T2|auto 档 model_hint → 拒绝（would_accept=False，随机判断不进授权链）",
               r.get("would_accept") is False and r.get("decision") == "reject"
               and "model_hint" in str(r.get("reason", "")), f"got={r}")
        r = adm.adjudicate(mode="auto", basis="intent")
        record("T2|auto 档显式转人工（basis=intent）→ 准入", r.get("would_accept") is True,
               f"got={r}")
        record("T2|auto 档 CRITICAL（urgency/safety_net）→ 准入（零弱化）",
               adm.adjudicate(mode="auto", basis="urgency").get("would_accept") is True
               and adm.adjudicate(mode="auto", basis="safety_net").get("would_accept") is True)
        r = adm.adjudicate(mode="observe", basis="model_hint")
        record("T2|observe 档 model_hint → 影子记录 would_accept=False（差异列）",
               r.get("would_accept") is False, f"got={r}")
        r = adm.adjudicate(mode="observe", basis="urgency")
        record("T2|observe 档 CRITICAL → 影子记录 would_accept=True（现行路径照旧）",
               r.get("would_accept") is True, f"got={r}")
        r = adm.adjudicate(mode="off", basis="model_hint")
        record("T2|off 档 → 旁路（passthrough，不裁决不拦截）",
               r.get("decision") == "passthrough", f"got={r}")
        os.environ["SAFETYMIND_ADMISSION_AUTO_SUBJECTS"] = "u-approved"
        r = adm.adjudicate(mode="auto", basis="hazard_gate_auto",
                           gate_decision=dict(FAKE_AUTO), subject_id="u-other")
        record("T2|auto 档主体不在批准范围 → 拒自动（降级建议/复核，零自动单）",
               r.get("would_accept") is False and "subject" in str(r.get("reason", "")),
               f"got={r}")
        r = adm.adjudicate(mode="auto", basis="hazard_gate_auto",
                           gate_decision=dict(FAKE_AUTO), subject_id="u-approved")
        record("T2|auto 档批准主体 + 闸门确定性命中 → 准入（定级/大类/派发/限期随裁决）",
               r.get("would_accept") is True, f"got={r}")
        os.environ.pop("SAFETYMIND_ADMISSION_AUTO_SUBJECTS", None)
    else:
        for name in ("T2|确定性谓词", "T2|auto 档裁决（model_hint 拒 / 显式与 CRITICAL 准）",
                     "T2|observe 档影子裁决", "T2|off 档旁路", "T2|主体范围收窄裁决"):
            record(name, False, "机制缺失（core.admission 不存在）")

    # ── T4 单元：mandate_revalidate 裁决原因集 ──
    def rv_unit(adm_mod):
        now = time.time()
        base = dict(MANDATE_OK)
        cases = [
            ("T4|有效快照 → ok", {"current_policy": PV1, "m": base}, "ok"),
            ("T4|策略换版 → policy_changed",
             {"current_policy": PV2, "m": base}, "policy_changed"),
            ("T4|撤权 → revoked",
             {"current_policy": PV1, "m": base, "subject_check": lambda s: False}, "revoked"),
            ("T4|过期 → expired",
             {"current_policy": PV1, "m": {**base, "decided_ts": now - 400 * 3600}}, "expired"),
            ("T4|跨主体操作 → cross_subject",
             {"current_policy": PV1, "m": base, "operator_subject": "u-B"}, "cross_subject"),
            ("T4|admin 操作他主体快照 → 豁免 ok",
             {"current_policy": PV1, "m": base, "operator_subject": "u-B",
              "operator_is_admin": True}, "ok"),
            ("T4|schema 版本不识别 → bad_schema",
             {"current_policy": PV1, "m": {**base, "schema_version": 99}}, "bad_schema"),
            ("T4|快照缺失 → legacy", {"current_policy": PV1, "m": None}, "legacy"),
        ]
        for name, kw, want in cases:
            r = adm_mod.mandate_revalidate(mandate=kw["m"], current_policy=kw["current_policy"],
                                           operator_subject=kw.get("operator_subject", ""),
                                           operator_is_admin=bool(kw.get("operator_is_admin")),
                                           subject_check=kw.get("subject_check"), now=now)
            record(name, r.get("reason") == want and (r.get("ok") is (want == "ok")),
                   f"got={r}")

    if adm is not None:
        rv_unit(adm)
    else:
        for name in ("T4|有效快照 → ok", "T4|策略换版 → policy_changed", "T4|撤权 → revoked",
                     "T4|过期 → expired", "T4|跨主体操作 → cross_subject",
                     "T4|admin 操作他主体快照 → 豁免 ok", "T4|schema 版本不识别 → bad_schema",
                     "T4|快照缺失 → legacy"):
            record(name, False, "机制缺失（core.admission.mandate_revalidate 不存在）")


# ── T2b CRITICAL 快速分流在 mode=auto 下零弱化（真实 _emergency_fast_path，无 LLM）──
def t2_critical_zero_weakening():
    os.environ["SAFETYMIND_ADMISSION_MODE"] = "auto"   # 强制 auto 夹具
    import core.emergency as em
    import api.main as m
    from core.audit_store import AuditStore

    db = str(Path(tempfile.mkdtemp(prefix="sm20_crit_")) / "crit.db")
    store = AuditStore(db)
    m._audit = store
    m._memory = None
    m._conv_store = None

    async def run():
        sig = em.precheck("液氨钢瓶泄漏正在扩散")
        req = m.ChatRequest(message="液氨钢瓶泄漏正在扩散", user_id="u-crit", conv_id="c-crit")
        return await m._emergency_fast_path(req, sig, time.perf_counter(),
                                            request_id=f"sm20-crit-{STAMP}")

    rid = f"sm20-crit-{STAMP}"
    resp = asyncio.run(run())
    record("T2|mode=auto 下 CRITICAL 快速分流零弱化（escalated/fastpath/紧急意图全保留）",
           resp.escalated is True and resp.emergency_fastpath is True
           and resp.intent == "emergency_response" and resp.urgency == "critical",
           f"escalated={resp.escalated} fastpath={getattr(resp, 'emergency_fastpath', None)}")
    tid = store.find_ticket_by_request(rid, "emergency")
    tk = store.get_ticket(tid) if tid else None
    record("T2|mode=auto 下 CRITICAL 仍真实建单（brief 阶段/critical 优先级，准入不阻塞应急）",
           bool(tk) and tk["type"] == "emergency" and tk["priority"] == "critical"
           and tk["stage"] == "brief", f"ticket={json.dumps(tk, ensure_ascii=False, default=str)[:160]}")
    ob = q1(db, "SELECT COUNT(*) AS n FROM notification_outbox WHERE kind='escalate'")
    record("T2|mode=auto 下 CRITICAL escalate 通知照常入队", bool(ob) and ob["n"] == 1,
           f"escalate_rows={ob['n'] if ob else '?'}")
    store.close()
    os.environ["SAFETYMIND_ADMISSION_MODE"] = "off"


# ── T3/T4 存储面：快照列/同事务/覆盖矩阵/终态回退续期 ────────────────────────
def t3_t4_snapshot_and_revalidation():
    from core.audit_store import AuditStore, idem_hash

    db = str(Path(tempfile.mkdtemp(prefix="sm20_snap_")) / "snap.db")
    store = AuditStore(db)

    # ── 列迁移（_migrate_locked 范式）──
    cols = ticket_cols(db)
    record("T3|tickets 非破坏加列 mandate_json", "mandate_json" in cols, f"cols={sorted(cols)}")
    record("T3|tickets 非破坏加列 policy_version", "policy_version" in cols,
           f"cols={sorted(cols)}")

    # ── create_ticket(mandate=) 同事务 ──
    mandate = dict(MANDATE_OK, proposal_id=f"sm20-create-{STAMP}",
                   evidence_ids=[f"audit:sm20-create-{STAMP}"])
    tid = None
    try:
        tid = store.create_ticket(
            "易燃易爆设施及场所隐患：快照同事务夹具", type_="hazard",
            source_request_id=mandate["proposal_id"],
            brief={"intent": "hazard_report", "urgency": "high", "entities": {"area": ["甲区"]},
                   "message_digest": "罐区储罐底部渗漏", "routing_reason": "sm20-t3",
                   "request_id": mandate["proposal_id"]},
            actor="system", user_id=mandate["subject_id"],
            severity="major", responsible_dept="安环部", mandate=mandate)
    except TypeError as ex:
        record("T3|create_ticket 支持 mandate 参数并与工单行同事务写入", False,
               f"机制缺失（TypeError: {ex}）")
    except sqlite3.OperationalError as ex:
        record("T3|create_ticket 支持 mandate 参数并与工单行同事务写入", False,
               f"机制缺失（OperationalError: {ex}）")
    if tid:
        row = q1(db, "SELECT mandate_json, policy_version FROM tickets WHERE id=?", (tid,))
        try:
            stored = json.loads(row["mandate_json"]) if row and row["mandate_json"] else None
        except (ValueError, TypeError):
            stored = None
        record("T3|建单落库 mandate_json 与 policy_version（同一次 commit，非后补 UPDATE）",
               bool(row) and isinstance(stored, dict)
               and stored.get("policy_version") == mandate["policy_version"]
               and stored.get("subject_id") == mandate["subject_id"]
               and (row["policy_version"] or "") == mandate["policy_version"],
               f"row={dict(row) if row else None}")
        got = store.get_ticket(tid)
        record("T3|_ticket_dict 派生 mandate_status=bound", got.get("mandate_status") == "bound",
               f"mandate_status={got.get('mandate_status')!r}")

        # ── 覆盖矩阵：改判 / 合并 / 重放 ──
        before = q1(db, "SELECT mandate_json, policy_version FROM tickets WHERE id=?", (tid,))
        store.update_ticket_fields(tid, {"severity": "general"}, actor="admin", note="改判")
        after = q1(db, "SELECT mandate_json, policy_version FROM tickets WHERE id=?", (tid,))
        record("T3|人工改判（update_ticket_fields）零覆盖快照",
               dict(before) == dict(after) and store.get_ticket(tid).get("mandate_status") == "bound",
               f"before={dict(before)} after={dict(after)}")
        store.record_ticket_event(tid, "repeat_report", actor="u-A", note="合并夹具",
                                  detail={"request_id": "sm20-merge"})
        after2 = q1(db, "SELECT mandate_json, policy_version FROM tickets WHERE id=?", (tid,))
        record("T3|合并（repeat_report）零覆盖快照", dict(before) == dict(after2))
    else:
        for name in ("T3|建单落库 mandate_json 与 policy_version（同一次 commit，非后补 UPDATE）",
                     "T3|_ticket_dict 派生 mandate_status=bound",
                     "T3|人工改判（update_ticket_fields）零覆盖快照",
                     "T3|合并（repeat_report）零覆盖快照"):
            record(name, False, "前置失败：create_ticket 不支持 mandate")

    # ── 应急快速分流建单带快照（真实入口）──
    rid_e = f"sm20-emg-{STAMP}"
    try:
        res = store.reserve_emergency_ticket(
            idem_key=f"emergency:{rid_e}:webhook", target=rid_e,
            request_hash=idem_hash({"rid": rid_e}), user_id="u-crit", conv_id="c-crit",
            message="液氨钢瓶泄漏正在扩散", intent_value="emergency_response",
            title="应急升级: sm20 快照夹具",
            brief={"intent": "emergency_response", "urgency": "critical", "entities": {},
                   "message_digest": "液氨钢瓶泄漏", "routing_reason": "sm20", "request_id": rid_e},
            channel="", payload={"request_id": rid_e, "text": "sm20"})
        tk_e = store.get_ticket(int(res.get("ticket_id") or 0)) if res.get("ticket_id") else None
        record("T3|应急快速分流建单带快照（source_type=emergency_fastpath，explicit）",
               bool(tk_e) and tk_e.get("mandate_status") == "bound"
               and json.loads(q1(db, "SELECT mandate_json FROM tickets WHERE id=?",
                                 (tk_e["id"],))["mandate_json"]).get("source_type")
               == "emergency_fastpath",
               f"mandate_status={tk_e.get('mandate_status') if tk_e else None}")
    except Exception as ex:
        record("T3|应急快速分流建单带快照（source_type=emergency_fastpath，explicit）",
               False, f"机制缺失（{type(ex).__name__}: {ex}）")

    # ── 确认卡消费建单带快照 + 幂等重放零覆盖 ──
    rid_c = f"sm20-conf-{STAMP}"
    store.save_hazard_suggestion(
        rid_c, "u-A", "c-conf",
        {"decision": {**FAKE_AUTO, "action": "suggest"},
         "brief": {"intent": "hazard_report", "urgency": "medium", "entities": {"area": ["乙区"]},
                   "message_digest": "配电柜绝缘垫缺失", "routing_reason": "sm20-t3",
                   "request_id": rid_c}})
    key_c = f"sm20-conf-key-{STAMP}"
    try:
        res = store.consume_hazard_suggestion(
            request_id=rid_c, user_id="u-A", actor="u-A",
            idem_action="hazard_confirm", idem_subject="u-A", idem_key=key_c,
            request_hash=idem_hash({"subject": "u-A", "conv_id": "c-conf"}), require_owner="u-A",
            new_ticket_fn=lambda d, b: {"title": d.get("summary", "确认卡建单"),
                                        "priority": "normal", "due_ts": time.time() + 72 * 3600,
                                        "severity": d.get("severity", ""),
                                        "responsible_dept": d.get("dept", "")},
            notify_fn=None)
        tid_c = int(res.get("ticket_id") or 0)
        tk_c = store.get_ticket(tid_c) if tid_c else None
        record("T3|确认卡消费建单带快照（source_type=hazard_confirm，explicit，主体绑定上报人）",
               bool(tk_c) and tk_c.get("mandate_status") == "bound"
               and json.loads(q1(db, "SELECT mandate_json FROM tickets WHERE id=?",
                                 (tid_c,))["mandate_json"]).get("subject_id") == "u-A",
               f"mandate_status={tk_c.get('mandate_status') if tk_c else None}")
        m_before = q1(db, "SELECT mandate_json FROM tickets WHERE id=?", (tid_c,))
        store.consume_hazard_suggestion(
            request_id=rid_c, user_id="u-A", actor="u-A",
            idem_action="hazard_confirm", idem_subject="u-A", idem_key=key_c,
            request_hash=idem_hash({"subject": "u-A", "conv_id": "c-conf"}), require_owner="u-A",
            new_ticket_fn=lambda d, b: {}, notify_fn=None)
        m_after = q1(db, "SELECT mandate_json FROM tickets WHERE id=?", (tid_c,))
        record("T3|幂等重放（同键重试）零覆盖快照", dict(m_before) == dict(m_after))
    except Exception as ex:
        record("T3|确认卡消费建单带快照（source_type=hazard_confirm，explicit，主体绑定上报人）",
               False, f"机制缺失（{type(ex).__name__}: {ex}）")
        record("T3|幂等重放（同键重试）零覆盖快照", False, "前置失败")

    # ── T4 终态回退续期（真实 transition_ticket）──
    def seed_ticket(stage, status):
        cur = store._con.execute(
            "INSERT INTO tickets (created_ts, title, status, stage, type, user_id)"
            " VALUES (?, ?, ?, ?, 'hazard', 'u-A')",
            (time.time(), f"sm20 续期夹具 {stage} {STAMP}", status, stage))
        store._con.commit()
        return int(cur.lastrowid)

    # T4a legacy：无快照旧单，accept→feedback 必须拒绝并留痕
    tid_l = seed_ticket("accept", "closed")
    r = store.transition_ticket(tid_l, "feedback", actor="admin", note="回退夹具")
    record("T4|legacy 旧单 accept→feedback 被拒（kind=mandate_legacy，不静默复活）",
           r.get("ok") is False and r.get("kind") == "mandate_legacy",
           f"现状：ok={r.get('ok')} kind={r.get('kind')}")
    tl = store.get_ticket_timeline(tid_l) or []
    record("T4|拒绝回退时 timeline 落 mandate_reauth_required 痕（可审计）",
           any(e.get("action") in ("mandate_reauth_required", "mandate_legacy") for e in tl),
           f"actions={[e.get('action') for e in tl]}")

    # T4b 有效快照：accept→feedback 放行
    tid_v = seed_ticket("accept", "closed")
    try:
        store._con.execute(
            "UPDATE tickets SET mandate_json=?, policy_version=? WHERE id=?",
            (json.dumps(MANDATE_OK, ensure_ascii=False), PV1, tid_v))
        store._con.commit()
    except sqlite3.OperationalError as ex:
        record("T4|有效快照下 accept→feedback 放行", False,
               f"前置缺失（无 mandate_json 列，OperationalError: {ex}）")
        record("T4|reject→feedback 同受续期校验（两种终态回退都覆盖）", False,
               "前置缺失（无 mandate_json 列）")
    else:
        r = store.transition_ticket(tid_v, "feedback", actor="admin", note="续期放行夹具")
        record("T4|有效快照下 accept→feedback 放行", r.get("ok") is True, f"got={r}")
        tid_r = seed_ticket("reject", "closed")
        store._con.execute(
            "UPDATE tickets SET mandate_json=?, policy_version=? WHERE id=?",
            (json.dumps(MANDATE_OK, ensure_ascii=False), PV1, tid_r))
        store._con.commit()
        r2 = store.transition_ticket(tid_r, "feedback", actor="admin", note="驳回重做夹具")
        record("T4|reject→feedback 同受续期校验（两种终态回退都覆盖）",
               r2.get("ok") is True, f"got={r2}")
        # 换版后同一张单不能再次回退复活
        os.environ["SAFETYMIND_ADMISSION_POLICY_VERSION"] = PV2
        try:
            tid_v2 = seed_ticket("accept", "closed")
            store._con.execute(
                "UPDATE tickets SET mandate_json=?, policy_version=? WHERE id=?",
                (json.dumps(MANDATE_OK, ensure_ascii=False), PV1, tid_v2))
            store._con.commit()
            r3 = store.transition_ticket(tid_v2, "feedback", actor="admin")
            record("T4|策略换版（policy_version 漂移）→ 拒绝回退（kind=mandate_stale/policy_changed）",
                   r3.get("ok") is False and r3.get("kind") in ("mandate_stale", "mandate_legacy"),
                   f"got={r3}")
        finally:
            os.environ["SAFETYMIND_ADMISSION_POLICY_VERSION"] = PV1

    # T4c 跨主体：操作主体 ≠ 快照主体（非 admin）→ 拒绝
    tid_x = seed_ticket("accept", "closed")
    try:
        store._con.execute(
            "UPDATE tickets SET mandate_json=?, policy_version=? WHERE id=?",
            (json.dumps(MANDATE_OK, ensure_ascii=False), PV1, tid_x))
        store._con.commit()
        r = store.transition_ticket(tid_x, "feedback", actor="u-B", operator_subject="u-B")
        record("T4|跨主体回退被拒（快照主体 u-A ≠ 操作主体 u-B 且非 admin）",
               r.get("ok") is False and r.get("kind") in ("mandate_stale", "mandate_legacy"),
               f"got={r}")
    except (TypeError, sqlite3.OperationalError) as ex:
        record("T4|跨主体回退被拒（快照主体 u-A ≠ 操作主体 u-B 且非 admin）", False,
               f"机制缺失（{type(ex).__name__}: {ex}）")

    # T4d 补授权：legacy 拒 → 人工补授权（时间线事件，不改快照）→ 放行
    tid_ra = seed_ticket("accept", "closed")
    try:
        r0 = store.transition_ticket(tid_ra, "feedback", actor="admin")
        record("T4|补授权前 legacy 回退被拒", r0.get("ok") is False
               and r0.get("kind") == "mandate_legacy", f"got={r0}")
        store.record_mandate_reauth(tid_ra, actor="admin-1", note="旧单人工补授权",
                                    policy_version=PV1)
        m_json = q1(db, "SELECT mandate_json FROM tickets WHERE id=?", (tid_ra,))
        record("T4|补授权不改写 mandate_json（禁止伪造历史依据，只追加时间线）",
               not (m_json["mandate_json"] or "").strip(), f"mandate={dict(m_json)}")
        r1 = store.transition_ticket(tid_ra, "feedback", actor="admin")
        record("T4|补授权后同一次回退放行（reason=reauthorized）", r1.get("ok") is True,
               f"got={r1}")
    except AttributeError as ex:
        record("T4|补授权前 legacy 回退被拒", False, f"机制缺失（{ex}）")
        record("T4|补授权不改写 mandate_json（禁止伪造历史依据，只追加时间线）", False,
               "机制缺失（record_mandate_reauth 不存在）")
        record("T4|补授权后同一次回退放行（reason=reauthorized）", False,
               "机制缺失（record_mandate_reauth 不存在）")
    except sqlite3.OperationalError as ex:
        record("T4|补授权前 legacy 回退被拒", False, f"机制缺失（OperationalError: {ex}）")
        record("T4|补授权不改写 mandate_json（禁止伪造历史依据，只追加时间线）", False,
               f"机制缺失（OperationalError: {ex}）")
        record("T4|补授权后同一次回退放行（reason=reauthorized）", False,
               f"机制缺失（OperationalError: {ex}）")

    store.close()


# ── T1 影子 E2E：off vs observe 两档 /chat 对照（业务一致 + hazard_observations 唯一增量）──
def t1_shadow_e2e(tmp_root):
    from core import hazard_gate
    from fastapi.testclient import TestClient
    import api.main as api_main

    db_a = str(Path(tmp_root) / f"t1_off_{STAMP}.db")
    db_b = str(Path(tmp_root) / f"t1_obs_{STAMP}.db")
    store_a = None
    store_b = None
    orig_decide = hazard_gate.decide
    os.environ["SAFETYMIND_ADMISSION_MODE"] = "off"
    try:
        with TestClient(api_main.app) as client:            # lifespan：/chat 依赖（对齐 SM-10 T3）
            hazard_gate.decide = lambda *a, **k: dict(FAKE_AUTO)
            store_a = api_main._audit                        # off 档：AUDIT_DB_PATH 指向 db_a

            r_a = client.post("/chat", json={
                "message": f"位号P-101储罐底部渗漏，需要处理（{STAMP}）",
                "user_id": "u-shadow", "conv_id": f"c-off-{STAMP}"})
            b_a = r_a.json()
            rid_a = str(b_a.get("request_id") or "")
            record("T1|off 档 /chat 现行为（HIGH+ 自动建单照旧）",
                   r_a.status_code == 200 and b_a.get("hazard_action") == "auto_ticket"
                   and int(b_a.get("hazard_ticket_id") or 0) > 0,
                   f"code={r_a.status_code} action={b_a.get('hazard_action')} "
                   f"tid={b_a.get('hazard_ticket_id')}")

            store_b = api_main.audit_store.AuditStore(db_b)   # observe 档：换隔离库
            api_main._audit = store_b
            os.environ["SAFETYMIND_ADMISSION_MODE"] = "observe"
            # 与 off 档同文（排除意图/紧急度识别差异噪声；换隔离库故互不去重）
            r_b = client.post("/chat", json={
                "message": f"位号P-101储罐底部渗漏，需要处理（{STAMP}）",
                "user_id": "u-shadow", "conv_id": f"c-obs-{STAMP}"})
            b_b = r_b.json()
            rid_b = str(b_b.get("request_id") or "")

            def biz(store, rid):
                tk = store.find_ticket_by_request(rid, "hazard")
                t = store.get_ticket(tk) if tk else None
                ob = qall(store._path, "SELECT kind, status, ticket_id FROM notification_outbox"
                                        " WHERE request_id=?", (rid,))
                tl = qall(store._path, "SELECT action FROM ticket_timeline WHERE ticket_id=?",
                          (tk or 0,))
                sg = q1(store._path, "SELECT COUNT(*) AS n FROM hazard_suggestions"
                                     " WHERE request_id=?", (rid,))
                up = q1(store._path, "SELECT COUNT(*) AS n FROM upgrade_events"
                                     " WHERE request_id=?", (rid,))
                return {
                    "ticket": None if t is None else (
                        t["type"], t["priority"], t["stage"], t["status"], t["severity"],
                        t["responsible_dept"], t["urgency"], t["intent"],
                        json.dumps(t["entities"], sort_keys=True, ensure_ascii=False)),
                    "outbox": sorted((r["kind"], r["status"]) for r in ob),
                    "timeline": sorted({r["action"] for r in tl}),
                    "suggestions": int(sg["n"]) if sg else -1,
                    "upgrades": int(up["n"]) if up else -1,
                }

            biz_a = biz(store_a, rid_a)
            biz_b = biz(store_b, rid_b)
            record("T1|observe 档业务票据与 off 基线一致（工单字段逐项相同）",
                   biz_a["ticket"] is not None and biz_a["ticket"] == biz_b["ticket"],
                   f"off={biz_a['ticket']} obs={biz_b['ticket']}")
            record("T1|observe 档通知/时间线/建议/升级事件与 off 基线一致",
                   biz_a["outbox"] == biz_b["outbox"] and biz_a["timeline"] == biz_b["timeline"]
                   and biz_a["suggestions"] == biz_b["suggestions"]
                   and biz_a["upgrades"] == biz_b["upgrades"],
                   f"off={ {k: biz_a[k] for k in ('outbox', 'timeline', 'suggestions', 'upgrades')} } "
                   f"obs={ {k: biz_b[k] for k in ('outbox', 'timeline', 'suggestions', 'upgrades')} }")

            # ── 影子数据：hazard_observations 是唯一增量 ──
            try:
                obs_b = qall(db_b, "SELECT * FROM hazard_observations WHERE request_id=?", (rid_b,))
            except sqlite3.OperationalError as ex:
                obs_b = None
                record("T1|hazard_observations 表存在（影子裁决落库）", False,
                       f"机制缺失（OperationalError: {ex}）")
            else:
                record("T1|hazard_observations 表存在（影子裁决落库）", True)
            if obs_b:
                o = obs_b[0]
                record("T1|影子行含 would_accept+定级+依据条款+命中大类+派发/限期",
                       o.get("would_accept") in (0, 1)
                       and o.get("severity") == FAKE_AUTO["severity"]
                       and o.get("category") == FAKE_AUTO["category"]
                       and (o.get("basis") or "") == FAKE_AUTO["basis"]
                       and o.get("dept") == FAKE_AUTO["dept"]
                       and float(o.get("deadline_hours") or 0) == float(FAKE_AUTO["deadline_hours"]),
                       f"row={ {k: o.get(k) for k in ('would_accept', 'severity', 'category', 'basis', 'dept', 'deadline_hours')} }")
                record("T1|影子行绑定主体与策略版本，并记录现行动作对照（current_action=auto_ticket）",
                       o.get("user_id") == "u-shadow" and (o.get("policy_version") or "") != ""
                       and o.get("current_action") == "auto_ticket"
                       and int(o.get("current_ticket_id") or 0) > 0,
                       f"row={ {k: o.get(k) for k in ('user_id', 'policy_version', 'current_action', 'current_ticket_id')} }")
            else:
                record("T1|影子行含 would_accept+定级+依据条款+命中大类+派发/限期", False,
                       "机制缺失（hazard_observations 无行/无表）")
                record("T1|影子行绑定主体与策略版本，并记录现行动作对照（current_action=auto_ticket）",
                       False, "机制缺失（hazard_observations 无行/无表）")
            try:
                obs_a = qall(store_a._path, "SELECT COUNT(*) AS n FROM hazard_observations")
                record("T1|off 档零影子写入（现行为不产生观测行）",
                       obs_a and obs_a[0]["n"] == 0, f"n={obs_a[0]['n'] if obs_a else '?'}")
            except sqlite3.OperationalError as ex:
                record("T1|off 档零影子写入（现行为不产生观测行）", False,
                       f"机制缺失（OperationalError: {ex}）")

            # ── T2 接线：api 建单分支必须经过准入层 ──
            api_src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
            record("T2|api/main.py 建单分支接线准入层（admission 引用存在）",
                   "admission" in api_src, "api/main.py 无 admission 引用")
    finally:
        hazard_gate.decide = orig_decide
        os.environ["SAFETYMIND_ADMISSION_MODE"] = "off"
        for s in (store_a, store_b):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass


# ── T5 escalation_basis 断言存在（既有语义重申，零弱化守卫）──────────────────
def t5_escalation_basis_guard():
    from agents.agent_orchestrator import (IntentCategory, UrgencyLevel, escalation_basis)
    U, I = UrgencyLevel, IntentCategory
    record("T5|escalation_basis 确定性优先级（urgency > intent > model_hint）不变",
           escalation_basis(U.CRITICAL, I.OTHER, True) == "urgency"
           and escalation_basis(U.LOW, I.ESCALATION, True) == "intent"
           and escalation_basis(U.LOW, I.OTHER, True) == "model_hint")
    src = (REPO / "agents" / "agent_orchestrator.py").read_text(encoding="utf-8")
    record("T5|升级出口仍写 basis（urgency/safety_net/主链路+并行链路）",
           'escalation_basis="urgency"' in src and 'escalation_basis="safety_net"' in src
           and src.count("_basis = escalation_basis(") >= 2)
    record("T5|model_hint 关键词检测保留（escalated 分类不弱化，摘出只在建单副作用层）",
           "_needs_escalation" in src and "转人工" in src)
    api_src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    record("T5|ChatResponse 暴露 escalation_basis 并透传",
           "escalation_basis: str" in api_src
           and 'escalation_basis=getattr(result, "escalation_basis", "")' in api_src)


def main():
    tmp_root = tempfile.mkdtemp(prefix="sm20_admission_")
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_CLARIFY": "0",
        "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
        "SAFETYMIND_OUTBOX_ENABLED": "0",
        "PROMETHEUS_PORT": "0",
        "AUDIT_ENABLED": "1",
        "API_HOST": "127.0.0.1",
        "SAFETYMIND_DEPLOYED": "",
        "EMERGENCY_MAX_CONCURRENCY": "2",
        "EMERGENCY_QUEUE_TIMEOUT_S": "0.05",
        "AUDIT_DB_PATH": str(Path(tmp_root) / "t1_off_base.db"),   # T1 off 档库（lifespan 建）
        "PREFERENCE_DB_PATH": str(Path(tmp_root) / "pref.db"),
        "SAFETYMIND_MEMORY_SNAPSHOT": str(Path(tmp_root) / "mem.json"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(Path(tmp_root) / "convs"),
        "CHROMA_PERSIST_DIRECTORY": str(Path(tmp_root) / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(Path(tmp_root) / "kb_chroma"),
        "ADMIN_TOKEN": f"sm20-admin-{STAMP}",
        "SAFETYMIND_ADMISSION_MODE": "off",
        # 先于 import 钉死（api.main 导入时 load_dotenv 不覆盖已显式设置的变量）
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "ESCALATION_WEBHOOK_URL": "",
    })
    try:
        t0_t2_admission_unit()
        t2_critical_zero_weakening()
        t3_t4_snapshot_and_revalidation()
        t5_escalation_basis_guard()
        t1_shadow_e2e(tmp_root)
    finally:
        failed = RESULTS.count(False)
        total = len(RESULTS)
        print(f"\n{total - failed}/{total} PASSED")
        print(f"[SM-20 RED 环境] 临时目录（保留备查，进程退出即弃）: {tmp_root}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
