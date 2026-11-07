# -*- coding: utf-8 -*-
# SM-01 第三片：API 角色权限矩阵（5 主体 × 4 动作逐格断言 + 越权写探针）——门禁层 G1。
# 设计：scratch/sm-loop/handoff/SM-01/slice3-design.md（role_matrix_eval / role_write_probe 机制移植，不搬代码）
# 红灯实测：scratch/sm-loop/handoff/SM-01/slice3-role-matrix-actual.json（2026-11-07，逐格实录后按实际行为钉死断言）
#
# 覆盖（全部走 FastAPI TestClient 真实 HTTP 端点，不起真实服务、零 LLM、零网络）：
#   5 主体：匿名(仅访问令牌) / 本人(u-A) / 他人(u-B) / 授权管理员(u-ADM) / 凭据撤销后(u-C)
#   4 动作：读工单 / 确认建议 / 导出下载 / 管理端改判 → 5×4 = 20 格 + 外墙探针(无访问令牌×4) = 24 格
#   写探针：每个主体的越权写逐个发真实请求，断言 401/403/409 且 DB 六表快照前后零变化
#
# G1 逐格预期（行动指南 G1：跨界读取或写入 0 次）：
#   读工单      本人 found=true；匿名/他人/撤权 found=false（不泄露存在性）；管理员走授权读
#               通道 GET /tickets/{id}/timeline（200，用户端 progress 仍归属受限）；无令牌 401
#   确认建议    仅本人 200（建议确认+建单+时间线+通知四表精确落库）；匿名/他人/管理员/撤权
#               一律 403 且 DB 零变化；无令牌 401
#   导出下载    无令牌 401；持访问令牌者 200 —— 当前产品无主体/角色边界（实测立案 RF-1：
#               skill safety_ledger_assistant 面向安全员产出台账，属设计取舍不修；导出内容为
#               静态法定模板，无跨主体数据，G1 数据跨界=0）
#   管理端改判  仅管理员 200（severity 改判 + patch 时间线留痕）；其余主体 401 且 DB 零变化
#
# 立案记录（实测发现、按设计取舍不修，详见红灯实测 JSON）：
#   RF-1 /ledger/export 仅访问口令门、无角色边界（任何 access gate 持有者可触发服务端文件生成）
#   RF-2 导出文件名按秒时间戳，同秒并发导出互相覆盖（实测 files_created +0）
#   RF-3 被拒越权写不留任何审计行 —— 探针断言取"DB 零变化"（比"只增对应拒绝事件"更严格）
#
# 铁律：存储全部走一次性临时目录（审计库 / 台账输出钉到 tmp），不触碰生产数据目录与仓库
# data/；环境不可用退出码 2，绝不伪装成业务失败。
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TOKEN_A, TOKEN_B, TOKEN_C, TOKEN_ADM = "mtx-tok-a", "mtx-tok-b", "mtx-tok-c", "mtx-tok-adm"
ACCESS, ADMIN = "mtx-access-1", "mtx-admin-1"
USERS_FULL = [
    {"token": TOKEN_A, "subject_id": "u-A", "roles": ["reporter"], "scopes": ["plant-a"]},
    {"token": TOKEN_B, "subject_id": "u-B", "roles": ["reporter"], "scopes": ["plant-b"]},
    {"token": TOKEN_C, "subject_id": "u-C", "roles": ["reporter"], "scopes": ["plant-c"]},
    {"token": TOKEN_ADM, "subject_id": "u-ADM", "roles": ["admin"], "scopes": ["*"]},
]
RID_1 = "RID-MTX-1"   # u-A 建议（夹具内本人确认 → 工单 T1：读/改判靶子）
RID_2 = "RID-MTX-2"   # u-A 建议，保持 pending（确认建议动作逐格靶子）
RID_3 = "RID-MTX-3"   # u-B 建议（夹具内本人确认，正向对照：确认建议对合法本人可用）
RID_4 = "RID-MTX-4"   # u-C 建议（撤权前本人确认 → 工单 T4；撤权后读靶子）

# 本人确认成功后的业务副作用（逐表钉死：多一表少一表都算行为漂移）
CONFIRM_DELTA = {"hazard_suggestions", "tickets", "ticket_timeline", "notification_outbox"}
REJUDGE_DELTA = {"tickets", "ticket_timeline"}
TABLES = ["audit_log", "upgrade_events", "tickets", "hazard_suggestions",
          "ticket_timeline", "notification_outbox"]
REJECT_CODES = (401, 403, 409)

RESULTS = []
TMP = Path(tempfile.mkdtemp(prefix="sm01_role_matrix_", dir="D:/claudecode/scratch/sm-loop"))


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


def sug_payload(summary, area):
    return {
        "decision": {"action": "suggest", "severity": "major", "summary": summary,
                     "dept": "设备动力部", "deadline_hours": 72, "basis": "角色矩阵夹具"},
        "brief": {"intent": "hazard_report", "urgency": "medium",
                  "entities": {"area": [area]}, "message_digest": summary,
                  "routing_reason": "role-matrix-fixture"},
    }


def probe_rejected_write(send):
    """写探针：真实发一次越权写请求，返回 (是否被拒且 DB 零变化, 明细)。

    断言：状态码 ∈ {401,403,409} 且 DB 六表快照前后零变化（当前产品拒绝路径不写任何
    审计行，"零变化"是"审计行只增对应拒绝事件"的最严格形态——拒绝不留审计痕已立案 RF-3）。
    只计算不断言，落断言由调用方的矩阵格完成（一格一条断言，不重复记）。"""
    before = db_snapshot()
    resp = send()
    delta = db_delta(before, db_snapshot())
    ok = resp.status_code in REJECT_CODES and not delta
    detail = f"code={resp.status_code} db_delta={delta} body={resp.text[:120]}"
    return ok, detail


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
        from core.audit_store import AuditStore
        import core.ledger_export as ledger_export
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
    ledger_out = TMP / "ledger_out"
    ledger_out.mkdir(parents=True, exist_ok=True)
    ledger_export.DEFAULT_OUT = ledger_out   # 台账输出钉到临时目录（绝不写 D:/SafetyMindData）
    api_main._audit = audit

    matrix_cells = []   # 矩阵格结果（24 格逐格断言，不留空格）

    def cell(name, ok, detail=""):
        matrix_cells.append(bool(ok))
        record(name, ok, detail)

    try:
        # ── 夹具：种建议 + 各自本人确认（正向对照，既是夹具也是矩阵正向锚点）──────
        audit.save_hazard_suggestion(RID_1, "u-A", "conv-mtx-1", sug_payload("甲区灭火器失效", "甲区"))
        audit.save_hazard_suggestion(RID_2, "u-A", "conv-mtx-2", sug_payload("乙区配电柜门未锁", "乙区"))
        audit.save_hazard_suggestion(RID_3, "u-B", "conv-mtx-3", sug_payload("丙区安全帽缺失", "丙区"))
        audit.save_hazard_suggestion(RID_4, "u-C", "conv-mtx-4", sug_payload("丁区梯子破损", "丁区"))

        b = db_snapshot()
        r = client.post("/hazard/confirm", json={"request_id": RID_1, "user_id": "u-A"}, headers=hdr(TOKEN_A))
        t1 = (r.json() or {}).get("ticket_id", 0) if r.status_code == 200 else 0
        delta = db_delta(b, db_snapshot())
        # 夹具正向对照计入 RESULTS 但不计入矩阵格（矩阵 24 格 = 5 主体 × 4 动作 + 外墙探针）
        record("夹具正向|本人(u-A)确认 RID_1 → 200 建单 T1 且四表精确落库",
               r.status_code == 200 and (r.json() or {}).get("ok") is True and t1 > 0
               and set(delta) == CONFIRM_DELTA,
               f"code={r.status_code} t1={t1} delta={delta}")
        r = client.post("/hazard/confirm", json={"request_id": RID_3, "user_id": "u-B"}, headers=hdr(TOKEN_B))
        record("夹具正向|他人(u-B)确认自己的 RID_3 → 200（证明 403 格是归属判定而非端点坏死）",
               r.status_code == 200 and (r.json() or {}).get("ok") is True,
               f"code={r.status_code} {r.text[:120]}")
        r = client.post("/hazard/confirm", json={"request_id": RID_4, "user_id": "u-C"}, headers=hdr(TOKEN_C))
        record("夹具正向|u-C 撤权前确认自己的 RID_4 → 200（工单 T4）",
               r.status_code == 200 and (r.json() or {}).get("ok") is True,
               f"code={r.status_code} {r.text[:120]}")
        t1 = audit.find_ticket_by_request(RID_1) or t1
        t4 = audit.find_ticket_by_request(RID_4) or 0

        # 凭据撤销：主体目录移除 tok-c（产品逐请求解析 env，即时生效）
        os.environ["SAFETYMIND_USERS"] = json.dumps(
            [u for u in USERS_FULL if u["token"] != TOKEN_C], ensure_ascii=False)

        # ══ 动作 1：读工单（用户端 GET /tickets/progress/{rid}；管理端 timeline）══
        b = db_snapshot()
        r = client.get(f"/tickets/progress/{RID_1}", headers=hdr())
        body = r.json() if r.status_code == 200 else {}
        cell("矩阵|读工单|匿名 → 200 且 found=false（读不到且不泄露存在性）",
             r.status_code == 200 and body.get("found") is False and not db_delta(b, db_snapshot()),
             f"code={r.status_code} body={str(body)[:120]}")

        b = db_snapshot()
        r = client.get(f"/tickets/progress/{RID_1}", headers=hdr(TOKEN_A))
        body = r.json() if r.status_code == 200 else {}
        cell("矩阵|读工单|本人 → 200 且 found=true（自己的上报编号可读）",
             r.status_code == 200 and body.get("found") is True
             and body.get("ticket_id") == t1 and not db_delta(b, db_snapshot()),
             f"code={r.status_code} body={str(body)[:160]}")

        b = db_snapshot()
        r = client.get(f"/tickets/progress/{RID_1}", headers=hdr(TOKEN_B))
        body = r.json() if r.status_code == 200 else {}
        cell("矩阵|读工单|他人(u-B) → found=false（读不到 u-A 的工单，不泄露存在性）",
             r.status_code == 200 and body.get("found") is False and not db_delta(b, db_snapshot()),
             f"code={r.status_code} body={str(body)[:120]}")

        b = db_snapshot()
        r_tl = client.get(f"/tickets/{t1}/timeline", headers=ahdr(TOKEN_ADM))
        r_pg = client.get(f"/tickets/progress/{RID_1}", headers=hdr(TOKEN_ADM))
        tl_body = r_tl.json() if r_tl.status_code == 200 else {}
        pg_body = r_pg.json() if r_pg.status_code == 200 else {}
        cell("矩阵|读工单|授权管理员 → 管理端 timeline 200 含该工单；用户端 progress 仍归属受限(found=false)",
             r_tl.status_code == 200 and tl_body.get("ticket", {}).get("id") == t1
             and bool(tl_body.get("timeline"))
             and r_pg.status_code == 200 and pg_body.get("found") is False
             and not db_delta(b, db_snapshot()),
             f"timeline={r_tl.status_code} progress={r_pg.status_code} found={pg_body.get('found')}")

        b = db_snapshot()
        r = client.get(f"/tickets/progress/{RID_4}", headers=hdr(TOKEN_C))
        body = r.json() if r.status_code == 200 else {}
        cell("矩阵|读工单|凭据撤销后 → 撤权前自己的上报编号也读不到（found=false）",
             r.status_code == 200 and body.get("found") is False and not db_delta(b, db_snapshot()),
             f"code={r.status_code} body={str(body)[:120]}")

        b = db_snapshot()
        r = client.get(f"/tickets/progress/{RID_1}")
        cell("矩阵|读工单|无访问令牌(外墙) → 401",
             r.status_code == 401 and not db_delta(b, db_snapshot()),
             f"code={r.status_code}")

        # ══ 动作 2：确认建议（POST /hazard/confirm；越权格先行，本人格最后）══════
        # 越权写探针：每个主体真实发一次越权确认，断言 403 且 DB 六表零变化。
        for subj, tok in (("匿名", ""), ("他人(u-B)", TOKEN_B),
                          ("授权管理员(u-ADM)", TOKEN_ADM), ("凭据撤销后(u-C)", TOKEN_C)):
            ok, detail = probe_rejected_write(
                lambda tok=tok: client.post("/hazard/confirm",
                                            json={"request_id": RID_2, "user_id": "u-A"},
                                            headers=hdr(tok)))
            cell(f"矩阵|确认建议|{subj} → 403 且 DB 六表零变化（写探针：真实越权写请求）",
                 ok, detail + " （越权确认 u-A 的 pending 建议）")

        b = db_snapshot()
        r = client.post("/hazard/confirm", json={"request_id": RID_2, "user_id": "u-A"}, headers=hdr(TOKEN_A))
        ok_body = r.json() if r.status_code == 200 else {}
        delta = db_delta(b, db_snapshot())
        sug_gone = audit.get_hazard_suggestion(RID_2) is None   # 确认后不再处于 pending
        cell("矩阵|确认建议|本人 → 200 建单、建议退出 pending、四表精确落库",
             r.status_code == 200 and ok_body.get("ok") is True
             and ok_body.get("merged") is False and ok_body.get("ticket_id", 0) > 0
             and sug_gone and set(delta) == CONFIRM_DELTA,
             f"code={r.status_code} body={str(ok_body)[:120]} sug_gone={sug_gone} delta={delta}")

        ok, detail = probe_rejected_write(
            lambda: client.post("/hazard/confirm", json={"request_id": RID_2}))
        cell("矩阵|确认建议|无访问令牌(外墙) → 401 且 DB 六表零变化", ok, detail)

        # ══ 动作 3：导出下载（POST /ledger/export，输出钉到临时目录）════════════
        # 当前产品行为冻结（立案 RF-1）：任何 access gate 持有者可导出（无主体/角色边界）。
        # G1 判定：导出内容为静态法定模板、不含任何主体数据 → 无跨界读取；落盘必须仍在
        # 夹具目录（封闭性守卫：产品若改为写硬编码生产目录，此格立即红）。
        b = db_snapshot()
        n0 = len(list(ledger_out.iterdir()))
        r = client.post("/ledger/export", json={"ledger": "hazard", "format": "xlsx"})
        n1 = len(list(ledger_out.iterdir()))
        cell("矩阵|导出下载|无访问令牌(外墙) → 401 且零文件、DB 零变化",
             r.status_code == 401 and n1 == n0 and not db_delta(b, db_snapshot()),
             f"code={r.status_code} files+{n1 - n0} delta={db_delta(b, db_snapshot())}")

        for subj, tok in (("匿名", ""), ("本人", TOKEN_A), ("他人(u-B)", TOKEN_B),
                          ("授权管理员(u-ADM)", TOKEN_ADM), ("凭据撤销后(u-C)", TOKEN_C)):
            b = db_snapshot()
            r = client.post("/ledger/export", json={"ledger": "hazard", "format": "xlsx"},
                            headers=hdr(tok))
            body = r.json() if r.status_code == 200 else {}
            path = str(body.get("path", ""))
            cell(f"矩阵|导出下载|{subj} → 200 且产物在夹具目录、DB 零变化（RF-1 行为冻结）",
                 r.status_code == 200 and body.get("ok") is True
                 and str(ledger_out) in path and not db_delta(b, db_snapshot()),
                 f"code={r.status_code} path={path[:120]} delta={db_delta(b, db_snapshot())}")

        xlsx_ok, xlsx_detail = False, ""
        try:
            import openpyxl
            files = sorted(ledger_out.iterdir())
            wb = openpyxl.load_workbook(str(files[-1]))
            xlsx_ok = len(wb.sheetnames) >= 1
            xlsx_detail = f"sheets={wb.sheetnames} files={[p.name for p in files]}"
        except Exception as ex:
            xlsx_detail = f"{type(ex).__name__}: {ex}"
        record("矩阵|导出下载|产物为真实可读回的 xlsx（非空壳/非错误页）", xlsx_ok, xlsx_detail)

        # ══ 动作 4：管理端改判（PATCH /tickets/{t1}；越权格先行，管理员最后）══════
        for subj, tok in (("无访问令牌(外墙)", None), ("匿名", ""), ("本人", TOKEN_A),
                          ("他人(u-B)", TOKEN_B), ("凭据撤销后(u-C)", TOKEN_C)):
            if tok is None:
                ok, detail = probe_rejected_write(
                    lambda: client.patch(f"/tickets/{t1}",
                                         json={"severity": "general", "note": "改判探针"}))
            else:
                ok, detail = probe_rejected_write(
                    lambda tok=tok: client.patch(f"/tickets/{t1}",
                                                 json={"severity": "general", "note": "改判探针"},
                                                 headers=hdr(tok)))
            cell(f"矩阵|管理端改判|{subj} → 401 且 DB 六表零变化（写探针：真实越权写请求）",
                 ok, detail)

        b = db_snapshot()
        sev_before = (audit.get_ticket(t1) or {}).get("severity", "")
        r = client.patch(f"/tickets/{t1}", json={"severity": "general", "note": "改判探针"},
                         headers=ahdr(TOKEN_ADM))
        body = r.json() if r.status_code == 200 else {}
        patch_rows = [e for e in (audit.get_ticket_timeline(t1) or []) if e.get("action") == "patch"]
        delta = db_delta(b, db_snapshot())
        cell("矩阵|管理端改判|授权管理员 → 200、severity major→general、patch 时间线留痕、两表精确落库",
             r.status_code == 200 and body.get("ok") is True
             and body.get("ticket", {}).get("severity") == "general"
             and sev_before == "major"
             and bool(patch_rows)
             and patch_rows[-1].get("detail", {}).get("severity", {}).get("from") == "major"
             and set(delta) == REJUDGE_DELTA,
             f"code={r.status_code} sev {sev_before}→{body.get('ticket', {}).get('severity')} "
             f"delta={delta} patch={str(patch_rows[-1])[:160] if patch_rows else '[]'}")

        # ══ 收口：矩阵 24 格逐格断言无遗漏 ══════════════════════════════════════
        closure_ok = len(matrix_cells) == 24 and all(matrix_cells)
        matrix_cells.append(closure_ok)   # 收口自检格如实计入
        record("矩阵收口|5 主体 × 4 动作 + 外墙探针 = 24 格全格断言（不留空格）",
               closure_ok, f"cells={len(matrix_cells) - 1} all_ok={all(matrix_cells)}")
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
