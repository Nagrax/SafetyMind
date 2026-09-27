"""审计日志 + 升级事件 + 轻量工单（SQLite 单文件，零外部依赖）。

设计约束：
  - 审计数据立身之本：断电不丢（WAL+commit）、可追责（request_id 全链路）、
    可统计（闭环时长/升级次数/超期数）——向量库和缓存都不具备这个形状；
  - 不存对话原文，只存摘要与指标（原文已在 ConversationStore，按需联动）；
  - 单进程假设（与 SafetyMind 部署形态一致），写操作加线程锁；
  - 表结构变更走手动迁移（一期不做自动 migration）。
"""
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

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
    closed_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_upg_ts ON upgrade_events(ts);
CREATE INDEX IF NOT EXISTS idx_ticket_status ON tickets(status);
"""


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
            self._con.commit()

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

    # ── 工单 ──────────────────────────────────────────────────────────────
    def create_ticket(self, title: str, type_: str = "hazard",
                      priority: str = "normal", assignee: str = "",
                      source_request_id: str = "", due_ts: Optional[float] = None) -> int:
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO tickets (created_ts, title, type, priority, assignee,"
                " source_request_id, due_ts) VALUES (?,?,?,?,?,?,?)",
                (time.time(), _preview(title, 120), type_, priority, assignee,
                 source_request_id, due_ts))
            self._con.commit()
            return cur.lastrowid

    def list_tickets(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._con.execute(
                    "SELECT * FROM tickets WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = self._con.execute(
                    "SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        now = time.time()
        for r in rows:
            d = dict(r)
            d["overdue"] = bool(d["due_ts"] and d["status"] != "closed" and d["due_ts"] < now)
            if d["closed_ts"]:
                d["closure_hours"] = round((d["closed_ts"] - d["created_ts"]) / 3600, 1)
            out.append(d)
        return out

    def close_ticket(self, ticket_id: int) -> bool:
        with self._lock:
            cur = self._con.execute(
                "UPDATE tickets SET status='closed', closed_ts=? WHERE id=? AND status!='closed'",
                (time.time(), ticket_id))
            self._con.commit()
            return cur.rowcount > 0

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
