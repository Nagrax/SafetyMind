"""审计日志 + 升级事件 + 轻量工单（SQLite 单文件，零外部依赖）。

设计约束：
  - 审计数据立身之本：断电不丢（WAL+commit）、可追责（request_id 全链路）、
    可统计（闭环时长/升级次数/超期数）——向量库和缓存都不具备这个形状；
  - 不存对话原文，只存摘要与指标（原文已在 ConversationStore，按需联动）；
  - 单进程假设（与 SafetyMind 部署形态一致），写操作加线程锁；
  - 表结构变更走非破坏迁移：__init__ 里 PRAGMA table_info 探测缺列后 ALTER TABLE
    ADD COLUMN + 回填（借鉴 OCTO Loop 的五阶段回路，2026-10）。

工单五阶段回路（ticket loop）：
    brief（需求简报）→ discuss（过程研讨）→ execute（执行产出）
      → feedback（多轮反馈）→ accept / reject（验收批注）
  - 顺序流转按 _STAGE_TRANSITIONS 白名单校验，accept/reject 可回退到 feedback；
  - 每次流转在 ticket_timeline 留操作者/批注/时间戳，非法跳转直接拒绝；
  - 旧数据兼容：status 两态保留为派生兼容字段（brief..feedback→open，
    accept/reject→closed），存量 open 工单视同 execute、closed 视同 accept，
    迁移只加列回填，不改写既有数据。

隐患工单闭环（hazard loop，2026-10 对齐双重预防机制）：
  - 对话侧 core/hazard_gate.py 分层触发（HIGH+ 自动 / MEDIUM 确认卡）；
  - MEDIUM 建议暂存 hazard_suggestions 表（用户 POST /hazard/confirm 才建单）；
  - tickets 增 severity（建议定级）/responsible_dept（派发部门）/user_id（上报人）；
  - 去重：同用户 24h 内同实体未关闭隐患单合并 repeat_report，不重复建单；
  - 逾期：check_overdue_tickets 幂等记 overdue_notify 并触发值班 webhook；
  - 改判：update_ticket_fields 白名单改字段 + patch 时间线留痕（研讨阶段人工定级）。
"""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    request_id TEXT NOT NULL,
    user_id TEXT, conv_id TEXT,
    message_preview TEXT, intent TEXT, urgency TEXT,
    escalated INTEGER DEFAULT 0, latency_ms REAL, degraded INTEGER DEFAULT 0,
    knowledge_used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS upgrade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    request_id TEXT NOT NULL,
    user_id TEXT, conv_id TEXT,
    message_preview TEXT, intent TEXT,
    notified INTEGER DEFAULT 0, notify_channel TEXT DEFAULT '',
    ack INTEGER DEFAULT 0, ack_ts REAL
);
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
    closed_ts REAL,
    stage TEXT DEFAULT '',
    intent TEXT DEFAULT '',
    urgency TEXT DEFAULT '',
    entities TEXT DEFAULT '',
    message_digest TEXT DEFAULT '',
    routing_reason TEXT DEFAULT '',
    severity TEXT DEFAULT '',
    responsible_dept TEXT DEFAULT '',
    user_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hazard_suggestions (
    request_id TEXT PRIMARY KEY,
    created_ts REAL NOT NULL,
    user_id TEXT DEFAULT '',
    conv_id TEXT DEFAULT '',
    payload TEXT NOT NULL,
    ticket_id INTEGER,
    status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS ticket_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    from_stage TEXT DEFAULT '',
    to_stage TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    note TEXT DEFAULT '',
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_upg_ts ON upgrade_events(ts);
CREATE INDEX IF NOT EXISTS idx_ticket_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_ttimeline_ticket ON ticket_timeline(ticket_id);
"""

# ── 五阶段回路定义（新库建表即带列；旧库由 _migrate 非破坏补列）────────────────
TICKET_STAGES = ("brief", "discuss", "execute", "feedback", "accept", "reject")
STAGE_LABELS = {
    "brief": "需求简报", "discuss": "过程研讨", "execute": "执行产出",
    "feedback": "多轮反馈", "accept": "验收通过", "reject": "验收驳回",
}
# 合法流转白名单：顺序前进 + 验收两态可回退到反馈阶段（驳回重做/验收后追加反馈轮）。
_STAGE_TRANSITIONS: Dict[str, set] = {
    "brief":   {"discuss"},
    "discuss": {"execute"},
    "execute": {"feedback"},
    "feedback": {"accept", "reject"},
    "accept":  {"feedback"},
    "reject":  {"feedback"},
}


def _preview(text: str, limit: int = 80) -> str:
    """消息摘要（不存原文）：压缩空白并截断。"""
    return " ".join(str(text or "").split())[:limit]


class AuditStore:
    """SQLite 审计/升级/工单存储。root 为数据库文件所在目录。"""

    def __init__(self, db_path: Optional[str] = None):
        self._path = db_path or os.getenv(
            "AUDIT_DB_PATH", os.path.join("data", "audit.db"))
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(self._path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.executescript(_DEFAULT_DDL)
            self._migrate_locked()
            self._con.commit()

    def _migrate_locked(self) -> None:
        """非破坏迁移：缺列则 ALTER ADD COLUMN；存量两态工单按旧语义回填阶段并留痕。
        只加列/回填新增列，不改写既有数据，失败仅回滚本次（审计主表不受影响）。"""
        try:
            cols = {r[1] for r in self._con.execute("PRAGMA table_info(tickets)")}
            for col, ddl in (
                ("stage", "TEXT DEFAULT ''"),
                ("intent", "TEXT DEFAULT ''"),
                ("urgency", "TEXT DEFAULT ''"),
                ("entities", "TEXT DEFAULT ''"),
                ("message_digest", "TEXT DEFAULT ''"),
                ("routing_reason", "TEXT DEFAULT ''"),
                ("severity", "TEXT DEFAULT ''"),
                ("responsible_dept", "TEXT DEFAULT ''"),
                ("user_id", "TEXT DEFAULT ''"),
            ):
                if col not in cols:
                    self._con.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ddl}")
            # 旧数据兼容（设计要求 3）：open 视为执行产出阶段，closed 视为验收完成。
            legacy = self._con.execute(
                "SELECT id, status FROM tickets WHERE stage='' OR stage IS NULL").fetchall()
            now = time.time()
            for r in legacy:
                stage = "accept" if r["status"] == "closed" else "execute"
                self._con.execute("UPDATE tickets SET stage=? WHERE id=?", (stage, r["id"]))
                self._con.execute(
                    "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                    " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                    (r["id"], now, "legacy_stage", "", stage, "system",
                     "旧数据兼容：open→execute / closed→accept" if r["status"] != "closed"
                     else "旧数据兼容：closed 视同验收完成", ""))
        except sqlite3.Error as ex:  # pragma: no cover - 迁移失败不拖垮审计存储
            self._con.rollback()
            logger.warning(f"工单五阶段迁移失败（沿用两态读取）: {ex}")

    def _norm_stage(self, stage: str, status: str) -> str:
        """阶段归一：stage 列缺失（理论上 _migrate 后不再出现）时按旧两态推导。"""
        return stage if stage else ("accept" if status == "closed" else "execute")

    def _ticket_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """行 → 对外字典：补 stage 派生、解析实体 JSON、保留 overdue/closure_hours。"""
        d = dict(row)
        d["stage"] = self._norm_stage(d.get("stage", ""), d.get("status", ""))
        d["stage_label"] = STAGE_LABELS.get(d["stage"], d["stage"])
        try:
            d["entities"] = json.loads(d["entities"]) if d.get("entities") else {}
        except (ValueError, TypeError):
            d["entities"] = {}
        now = time.time()
        d["overdue"] = bool(d.get("due_ts") and d["status"] != "closed" and d["due_ts"] < now)
        if d.get("closed_ts"):
            d["closure_hours"] = round((d["closed_ts"] - d["created_ts"]) / 3600, 1)
        return d

    # ── 审计日志 ──────────────────────────────────────────────────────────
    def audit_request(self, request_id: str, user_id: str, conv_id: str,
                      message: str, intent: str, urgency: str,
                      escalated: bool, latency_ms: float,
                      degraded: bool = False, knowledge_used: bool = False) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO audit_log (ts, request_id, user_id, conv_id, message_preview,"
                " intent, urgency, escalated, latency_ms, degraded, knowledge_used)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), request_id, user_id, conv_id, _preview(message),
                 intent, urgency, int(escalated), latency_ms,
                 int(degraded), int(knowledge_used)))
            self._con.commit()

    def list_audit(self, limit: int = 100, escalated_only: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            sql = "SELECT * FROM audit_log"
            if escalated_only:
                sql += " WHERE escalated=1"
            sql += " ORDER BY id DESC LIMIT ?"
            rows = self._con.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── 升级事件（值班闭环）───────────────────────────────────────────────
    def add_upgrade(self, request_id: str, user_id: str, conv_id: str,
                    message: str, intent: str) -> int:
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO upgrade_events (ts, request_id, user_id, conv_id,"
                " message_preview, intent) VALUES (?,?,?,?,?,?)",
                (time.time(), request_id, user_id, conv_id, _preview(message), intent))
            self._con.commit()
            return cur.lastrowid

    def mark_notified(self, upgrade_id: int, channel: str) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE upgrade_events SET notified=1, notify_channel=? WHERE id=?",
                (channel, upgrade_id))
            self._con.commit()

    def list_upgrades(self, limit: int = 50, unacked_only: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            sql = "SELECT * FROM upgrade_events"
            if unacked_only:
                sql += " WHERE ack=0"
            sql += " ORDER BY id DESC LIMIT ?"
            rows = self._con.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def ack_upgrade(self, upgrade_id: int) -> bool:
        with self._lock:
            cur = self._con.execute(
                "UPDATE upgrade_events SET ack=1, ack_ts=? WHERE id=? AND ack=0",
                (time.time(), upgrade_id))
            self._con.commit()
            return cur.rowcount > 0

    # ── 工单（五阶段回路）───────────────────────────────────────────────────
    def create_ticket(self, title: str, type_: str = "hazard",
                      priority: str = "normal", assignee: str = "",
                      source_request_id: str = "", due_ts: Optional[float] = None,
                      brief: Optional[Dict[str, Any]] = None,
                      actor: str = "system", user_id: str = "",
                      severity: str = "", responsible_dept: str = "") -> int:
        """建单并落入需求简报（brief）阶段。brief 缺省时（管理端手动建单）
        以标题生成最小简报；request_id 经 source_request_id 落库。
        简报字段：intent / urgency / entities(实体) / message_digest(消息摘要) /
        routing_reason / request_id —— 全部与本行一起落库。
        隐患工单补充字段：severity（major/general 建议定级）、responsible_dept
        （派发部门）、user_id（上报人，去重与报表用）。"""
        if brief is None:
            brief = {"intent": "", "urgency": "", "entities": {},
                     "message_digest": _preview(title, 80),
                     "routing_reason": "管理员手动建单", "request_id": source_request_id}
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO tickets (created_ts, title, type, priority, assignee,"
                " source_request_id, due_ts, stage, intent, urgency, entities,"
                " message_digest, routing_reason, severity, responsible_dept, user_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), _preview(title, 120), type_, priority, assignee,
                 source_request_id, due_ts, "brief",
                 str(brief.get("intent", "")), str(brief.get("urgency", "")),
                 json.dumps(brief.get("entities") or {}, ensure_ascii=False),
                 _preview(str(brief.get("message_digest", "")), 120),
                 _preview(str(brief.get("routing_reason", "")), 120),
                 str(severity or ""), str(responsible_dept or ""), str(user_id or "")))
            tid = cur.lastrowid
            brief_store = dict(brief)
            brief_store["request_id"] = brief_store.get("request_id") or source_request_id
            self._con.execute(
                "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                (tid, time.time(), "create", "", "brief", actor, _preview(title, 80),
                 json.dumps(brief_store, ensure_ascii=False)))
            self._con.commit()
            return tid

    def _ticket_row_locked(self, ticket_id: int) -> Optional[sqlite3.Row]:
        return self._con.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()

    def get_ticket(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._ticket_row_locked(ticket_id)
        return self._ticket_dict(row) if row else None

    def transition_ticket(self, ticket_id: int, to_stage: str,
                          actor: str = "", note: str = "",
                          detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """五阶段流转：白名单校验（顺序前进 + accept/reject 回退 feedback），
        非法跳转/未知阶段/重复同阶段一律拒绝；成功则更新 stage 与派生 status，
        并把 操作者/批注/时间戳 写入 ticket_timeline。持同一把线程锁保证原子。"""
        with self._lock:
            row = self._ticket_row_locked(ticket_id)
            if row is None:
                return {"ok": False, "kind": "not_found",
                        "error": f"工单不存在: {ticket_id}"}
            from_stage = self._norm_stage(row["stage"], row["status"])
            if to_stage not in _STAGE_TRANSITIONS:
                return {"ok": False, "kind": "bad_stage", "from_stage": from_stage,
                        "error": f"未知阶段: {to_stage}"}
            if to_stage == from_stage:
                return {"ok": False, "kind": "bad_transition", "from_stage": from_stage,
                        "error": f"已处于 {STAGE_LABELS.get(from_stage, from_stage)} 阶段"}
            if to_stage not in _STAGE_TRANSITIONS[from_stage]:
                return {"ok": False, "kind": "bad_transition", "from_stage": from_stage,
                        "error": f"非法流转: {STAGE_LABELS.get(from_stage, from_stage)}"
                                 f" → {STAGE_LABELS.get(to_stage, to_stage)}"}
            now = time.time()
            closing = to_stage in ("accept", "reject")
            if closing:
                self._con.execute(
                    "UPDATE tickets SET stage=?, status='closed', closed_ts=? WHERE id=?",
                    (to_stage, now, ticket_id))
            else:  # 回退/推进到中间阶段：恢复在办态，闭环时长随之失效
                self._con.execute(
                    "UPDATE tickets SET stage=?, status='open', closed_ts=NULL WHERE id=?",
                    (to_stage, ticket_id))
            self._con.execute(
                "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, now, "stage_change", from_stage, to_stage,
                 actor, note, json.dumps(detail or {}, ensure_ascii=False)))
            self._con.commit()
            new_row = self._ticket_row_locked(ticket_id)
            return {"ok": True, "from_stage": from_stage, "to_stage": to_stage,
                    "ticket": self._ticket_dict(new_row)}

    def record_ticket_event(self, ticket_id: int, action: str, actor: str = "",
                            note: str = "", from_stage: str = "", to_stage: str = "",
                            detail: Optional[Dict[str, Any]] = None) -> bool:
        """追加时间线事件（如 webhook 通知实收），不改 stage。工单不存在返回 False。"""
        with self._lock:
            if self._ticket_row_locked(ticket_id) is None:
                return False
            self._con.execute(
                "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, time.time(), action, from_stage, to_stage,
                 actor, note, json.dumps(detail or {}, ensure_ascii=False)))
            self._con.commit()
        return True

    def get_ticket_timeline(self, ticket_id: int) -> Optional[List[Dict[str, Any]]]:
        """完整时间线（按发生顺序）。工单不存在返回 None（端点据此回 404）。"""
        with self._lock:
            if self._ticket_row_locked(ticket_id) is None:
                return None
            rows = self._con.execute(
                "SELECT * FROM ticket_timeline WHERE ticket_id=? ORDER BY id ASC",
                (ticket_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            except (ValueError, TypeError):
                d["detail"] = {}
            out.append(d)
        return out

    def get_ticket_progress(self, request_id: str) -> Optional[Dict[str, Any]]:
        """工单进度查询（用户端升级卡片数据源，克制暴露）：按 source_request_id 取
        最近一张关联工单，回五阶段进度骨架 + 最新一条带批注事件。
        刻意不下发完整时间线与简报细节（entities/message_digest/routing_reason
        仍只在管理端 GET /tickets/{id}/timeline）；无关联工单返回 None
        （端点据此回 found=false，前端降级为"工单生成中"空态）。"""
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM tickets WHERE source_request_id=? ORDER BY id DESC LIMIT 1",
                (request_id,)).fetchone()
            if row is None:
                return None
            events = self._con.execute(
                "SELECT * FROM ticket_timeline WHERE ticket_id=? ORDER BY id ASC",
                (row["id"],)).fetchall()
        ticket = self._ticket_dict(row)
        stage = ticket["stage"]
        # 各阶段首次到达时间（create/stage_change/legacy_*，webhook 等旁路事件不计入）
        visited = {}
        latest_note = None
        for e in events:
            to_stage = e["to_stage"] or ""
            if e["action"] in ("create", "stage_change", "legacy_close", "legacy_stage") \
                    and to_stage in STAGE_LABELS:
                visited.setdefault(to_stage, e["ts"])
            if e["note"]:
                latest_note = {"action": e["action"], "actor": e["actor"], "note": e["note"],
                               "to_stage": to_stage, "ts": e["ts"]}
        # 进度骨架：brief→discuss→execute→feedback 顺序推进，accept/reject 归并为验收结论。
        # 走到后面的阶段即视前面全部完成（旧数据迁移单只有 legacy 一条时间线也自洽）。
        order = ("brief", "discuss", "execute", "feedback")
        cur_idx = order.index(stage) if stage in order else len(order)
        steps = []
        for i, s in enumerate(order):
            steps.append({
                "stage": s, "label": STAGE_LABELS[s],
                "state": "current" if s == stage else ("done" if i < cur_idx else "pending"),
                "ts": visited.get(s),
            })
        verdict_stage = stage if stage in ("accept", "reject") else ""
        return {
            "found": True,
            "ticket_id": ticket["id"],
            "title": ticket["title"],
            "status": ticket["status"],
            "stage": stage,
            "stage_label": ticket["stage_label"],
            "created_ts": ticket["created_ts"],
            "closed_ts": ticket.get("closed_ts"),
            "steps": steps,
            "verdict": {
                "stage": verdict_stage,
                "label": STAGE_LABELS.get(verdict_stage, "验收"),
                "state": "done" if verdict_stage else "pending",
                "ts": visited.get(verdict_stage) if verdict_stage else None,
            },
            "latest_event": latest_note,
        }

    def list_tickets(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._con.execute(
                    "SELECT * FROM tickets WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = self._con.execute(
                    "SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._ticket_dict(r) for r in rows]

    def close_ticket(self, ticket_id: int) -> bool:
        """旧两态闭环接口（POST /tickets/{id}/close），行为不回退：status 置 closed，
        stage 视同验收完成（accept），并在时间线留痕。对任何阶段可用（旧语义）。"""
        with self._lock:
            row = self._ticket_row_locked(ticket_id)
            if row is None or row["status"] == "closed":
                return False
            now = time.time()
            self._con.execute(
                "UPDATE tickets SET status='closed', closed_ts=?, stage='accept' WHERE id=?",
                (now, ticket_id))
            self._con.execute(
                "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, now, "legacy_close", self._norm_stage(row["stage"], row["status"]),
                 "accept", "admin", "旧接口闭环，视同验收完成", ""))
            self._con.commit()
            return True

    # ── 隐患工单（hazard gate 暂存/去重/逾期/改判）──────────────────────────
    def save_hazard_suggestion(self, request_id: str, user_id: str, conv_id: str,
                               payload: Dict[str, Any]) -> None:
        """暂存确认卡建议（MEDIUM 档：用户点击 /hazard/confirm 才建单）。
        payload = hazard_gate.decide() 结果 + 简报材料；SQLite 落库，重启不丢、可审计。
        同一 request_id 重复上报按覆盖处理（后到的裁决为准）。"""
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO hazard_suggestions"
                " (request_id, created_ts, user_id, conv_id, payload, ticket_id, status)"
                " VALUES (?,?,?,?,?,NULL,'pending')",
                (request_id, time.time(), user_id, conv_id,
                 json.dumps(payload, ensure_ascii=False)))
            self._con.commit()

    def get_hazard_suggestion(self, request_id: str,
                              max_age_hours: float = 24.0) -> Optional[Dict[str, Any]]:
        """取待确认建议（pending、未建单、未过期）；无则 None（端点据此回 404）。"""
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM hazard_suggestions WHERE request_id=?", (request_id,)).fetchone()
        if row is None or row["status"] != "pending" or row["ticket_id"] is not None:
            return None
        if time.time() - row["created_ts"] > max_age_hours * 3600:
            return None
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            return None
        return {"request_id": row["request_id"], "user_id": row["user_id"],
                "conv_id": row["conv_id"], "created_ts": row["created_ts"],
                "payload": payload}

    def mark_hazard_suggestion(self, request_id: str, status: str,
                               ticket_id: Optional[int] = None) -> bool:
        """确认/合并/过期标记（status: confirmed | merged | expired）。"""
        with self._lock:
            cur = self._con.execute(
                "UPDATE hazard_suggestions SET status=?, ticket_id=COALESCE(?, ticket_id)"
                " WHERE request_id=?", (status, ticket_id, request_id))
            self._con.commit()
            return cur.rowcount > 0

    def find_open_hazard_duplicate(self, user_id: str, entities: Dict[str, Any],
                                   window_hours: float = 24.0) -> Optional[Dict[str, Any]]:
        """去重：同用户 24h 内未关闭的隐患工单且实体重叠（设备/区域/化学品任一
        交集）→ 返回已有工单（调用方合并 repeat_report 事件，不新建）。
        防工单贬值：同一处漏点反复上报只累计到一张单上。"""
        since = time.time() - window_hours * 3600
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM tickets WHERE type='hazard' AND status!='closed'"
                " AND user_id=? AND created_ts>? ORDER BY id DESC LIMIT 50",
                (user_id, since)).fetchall()
        ents = entities if isinstance(entities, dict) else {}
        keys = ("equipment_id", "area", "chemical")
        for r in rows:
            try:
                old = json.loads(r["entities"]) if r["entities"] else {}
            except (ValueError, TypeError):
                old = {}
            for k in keys:
                a = {str(x) for x in (ents.get(k) or []) if x}
                b = {str(x) for x in (old.get(k) or []) if x}
                if a and b and a & b:
                    return self._ticket_dict(r)
        return None

    def list_overdue_tickets(self) -> List[Dict[str, Any]]:
        """逾期未闭环工单（due_ts 已过且 open）——GET /tickets/overdue 数据源。"""
        now = time.time()
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM tickets WHERE status!='closed'"
                " AND due_ts IS NOT NULL AND due_ts<? ORDER BY due_ts ASC", (now,)).fetchall()
        return [self._ticket_dict(r) for r in rows]

    def check_overdue_tickets(self) -> List[Dict[str, Any]]:
        """逾期巡检（后台循环调用）：首次逾期的工单记 overdue_notify 时间线并返回
        （调用方据此发 webhook）；已有该事件的不再重复返回——幂等，通知只发一次。"""
        newly = []
        for t in self.list_overdue_tickets():
            with self._lock:
                dup = self._con.execute(
                    "SELECT 1 FROM ticket_timeline WHERE ticket_id=? AND action='overdue_notify'",
                    (t["id"],)).fetchone()
            if dup is None:
                ok = self.record_ticket_event(
                    t["id"], "overdue_notify", actor="system",
                    note=f"整改期限已过（逾期 {round((time.time() - t['due_ts']) / 3600, 1)}h），升级提醒",
                    to_stage=t["stage"])
                if ok:
                    newly.append(t)
        return newly

    # 允许经 PATCH /tickets/{id} 改判的字段（研讨阶段人工定级/改派/改期限）
    _PATCHABLE_TICKET_FIELDS = {
        "severity": lambda v: str(v),
        "responsible_dept": lambda v: str(v),
        "assignee": lambda v: str(v),
        "priority": lambda v: str(v),
        "due_ts": lambda v: float(v),
    }

    def update_ticket_fields(self, ticket_id: int, fields: Dict[str, Any],
                             actor: str = "admin", note: str = "") -> Dict[str, Any]:
        """白名单字段修改 + patch 时间线留痕（改动前后值都落 detail）。
        返回 {"ok", kind?, ticket?}；not_found / no_field 与流转端点错误口径一致。"""
        with self._lock:
            row = self._ticket_row_locked(ticket_id)
            if row is None:
                return {"ok": False, "kind": "not_found", "error": f"工单不存在: {ticket_id}"}
            changes: Dict[str, Any] = {}
            for key, cast in self._PATCHABLE_TICKET_FIELDS.items():
                if key in fields and fields[key] is not None:
                    try:
                        changes[key] = cast(fields[key])
                    except (TypeError, ValueError):
                        return {"ok": False, "kind": "bad_field",
                                "error": f"字段 {key} 的值不合法: {fields[key]!r}"}
            if not changes:
                return {"ok": False, "kind": "no_field",
                        "error": "无可更新字段（允许: " + ", ".join(sorted(self._PATCHABLE_TICKET_FIELDS)) + "）"}
            detail = {k: {"from": row[k], "to": v} for k, v in changes.items()}
            sets = ", ".join(f"{k}=?" for k in changes)
            self._con.execute(f"UPDATE tickets SET {sets} WHERE id=?",
                              (*changes.values(), ticket_id))
            self._con.execute(
                "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, time.time(), "patch", "", "", actor, note or "工单信息修改",
                 json.dumps(detail, ensure_ascii=False)))
            self._con.commit()
            new_row = self._ticket_row_locked(ticket_id)
            return {"ok": True, "ticket": self._ticket_dict(new_row)}

    # ── 报表（安环部周报的数据源）─────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        now = time.time()
        day = now - 86400
        week = now - 7 * 86400
        with self._lock:
            q = lambda sql, args=(): self._con.execute(sql, args).fetchone()[0]  # noqa: E731
            out = {
                "requests_24h": q("SELECT COUNT(*) FROM audit_log WHERE ts>?", (day,)),
                "requests_7d": q("SELECT COUNT(*) FROM audit_log WHERE ts>?", (week,)),
                "escalations_24h": q("SELECT COUNT(*) FROM upgrade_events WHERE ts>?", (day,)),
                "escalations_unacked": q("SELECT COUNT(*) FROM upgrade_events WHERE ack=0"),
                "tickets_open": q("SELECT COUNT(*) FROM tickets WHERE status!='closed'"),
                "tickets_overdue": q(
                    "SELECT COUNT(*) FROM tickets WHERE status!='closed' AND due_ts IS NOT NULL AND due_ts<?", (now,)),
                "tickets_closed_7d": q(
                    "SELECT COUNT(*) FROM tickets WHERE status='closed' AND closed_ts>?", (week,)),
                "avg_closure_hours_7d": None,
            }
            row = self._con.execute(
                "SELECT AVG(closed_ts-created_ts) FROM tickets"
                " WHERE status='closed' AND closed_ts>?", (week,)).fetchone()
            if row and row[0] is not None:
                out["avg_closure_hours_7d"] = round(row[0] / 3600, 1)
        return out

    def close(self) -> None:
        with self._lock:
            self._con.close()


def notify_escalation_blocking(url: str, payload: Dict[str, Any]) -> bool:
    """向值班 webhook 推送升级通知（企业微信/钉钉/飞书通用文本格式）。
    网络失败返回 False，绝不抛出——通知失败不能阻断对话主链路。"""
    import json
    import urllib.request
    try:
        text = payload.get("text", "")
        body = json.dumps({"msgtype": "text", "text": {"content": text}},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False
