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

可靠通知（notification outbox，2026-10；SM-11 2026-11 增补）：
  - 工单变化与"待发送通知"同一次持锁/同一次 commit 落库（进程退出后仍可恢复投递）；
  - 状态严格区分 waiting(待配置) / pending(待发) / sending(投递中) / sent(渠道已受理)
    / ack(接收方或人工已确认) / failed(重试中) / failed_exhausted(失败耗尽，仅供人工重放)；
  - 未配置渠道入 waiting（不耗 attempts、不计失败、永不被领取），补配置后经
    adopt_waiting_notifications 恢复投递（旧空渠道行启动时一次性迁移为 waiting）；
  - 行按 kind 分类：notify(建单/确认卡派单) / overdue(逾期巡检) / escalate(应急升级)，
    按 (kind, channel) 维护健康项（连败 FAILURES_BEFORE_ERROR=3 次才 breached）；
  - 领取发代次 claim_generation：送达/失败回执只能由当前代次持有者写入，
    被 reap 复位的旧悬挂消费者复活后无法改写新消费者的投递结果；
  - 幂等键唯一，重复投递不新增；悬挂 sending 由 reap_stale_sending 复位重投；
  - 投递语义为 at-least-once：渠道已受理（外部事实）与本地标记 sent（本地事实）之间
    存在不可消除的崩溃窗口，重投 payload 逐字节相同且顶层带 idempotency_key，
    下游按该键去重实现业务恰好一次。
"""
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def canonical_json(obj: Any) -> str:
    """幂等内容哈希的规范化序列化：键排序 + 紧凑分隔符（同内容必同串）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def idem_hash(obj: Any) -> str:
    """请求内容哈希（SM-10 §2.2）：同键同哈希=请求重试（重放收据）、同键异哈希=冲突。"""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


# 按 (kind, channel) 健康项连败阈值（SM-11 §2.6，对标 dsh src/app/effects.ts:7）：
# 连败 <3 不向管理层报错（per-row details 仍如实记录，只是不升级）；≥3 记 breached_ts。
FAILURES_BEFORE_ERROR = 3

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
    user_id TEXT DEFAULT '',
    mandate_json TEXT DEFAULT '',
    policy_version TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hazard_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts REAL NOT NULL,
    request_id TEXT NOT NULL UNIQUE,          -- 幂等：同上报编号一条（INSERT OR IGNORE）
    user_id TEXT DEFAULT '',                  -- 服务端绑定主体（S-05）
    conv_id TEXT DEFAULT '',
    policy_version TEXT DEFAULT '',           -- 影子裁决用的策略版本
    would_accept INTEGER NOT NULL DEFAULT 0,  -- 新准入规则下是否自动建单（1/0）
    decision TEXT DEFAULT '',                 -- accept | reject | observe | passthrough
    reason TEXT DEFAULT '',                   -- 依据条款（admission.adjudicate().reason）
    action_proposed TEXT DEFAULT '',          -- auto_ticket | suggest | none
    severity TEXT DEFAULT '',                 -- major | general（定级）
    category TEXT DEFAULT '',                 -- 命中台账大类
    basis TEXT DEFAULT '',                    -- 依据条款引用（hazard_gate.basis）
    dept TEXT DEFAULT '',                     -- 派发部门
    deadline_hours REAL,                      -- 限期
    current_action TEXT DEFAULT '',           -- 现行路径实际动作（影子对照列）
    current_ticket_id INTEGER                 -- 现行单号（可空）
);
CREATE INDEX IF NOT EXISTS idx_hzobs_request ON hazard_observations(request_id);
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
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts REAL NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    channel TEXT DEFAULT '',
    request_id TEXT DEFAULT '',
    ticket_id INTEGER,
    upgrade_id INTEGER,
    payload TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    next_attempt_ts REAL DEFAULT 0,
    last_error TEXT DEFAULT '',
    sent_ts REAL,
    acked_ts REAL,
    kind TEXT DEFAULT 'notify',
    claim_generation INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_upg_ts ON upgrade_events(ts);
CREATE INDEX IF NOT EXISTS idx_ticket_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_ttimeline_ticket ON ticket_timeline(ticket_id);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON notification_outbox(status, next_attempt_ts);
CREATE TABLE IF NOT EXISTS hazard_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts REAL NOT NULL,
    action TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    idem_key TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    receipt TEXT DEFAULT '',
    error_code TEXT DEFAULT '',
    detail TEXT DEFAULT ''
);
-- 部分唯一索引（SM-10 幂等预留裁决）：只有 applied 行参与唯一约束——replayed/conflicted
-- 痕不占键，同键重放/冲突审计可无限追加。并发裁决：INSERT 撞此索引（语句级回滚）→
-- 重查 applied 行 → 同哈希=重放收据 / 异哈希=冲突痕先落库再 409。
CREATE UNIQUE INDEX IF NOT EXISTS ux_hazard_ops_active
    ON hazard_ops(action, subject, idem_key) WHERE status='applied';
CREATE INDEX IF NOT EXISTS idx_hazard_ops_key ON hazard_ops(action, subject, idem_key);
-- 按 (kind, channel) 健康项（SM-11 §2.6）：连败计数 + 达阈 breached_ts；waiting 行不参与。
CREATE TABLE IF NOT EXISTS notification_health (
    kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    consecutive_failures INTEGER DEFAULT 0,
    breached_ts REAL,
    PRIMARY KEY(kind, channel)
);
-- SM-40 只读巡检 watchdog：事实状态与事件（巡检组件的**自有簿记**，只允许巡检路径写入；
-- 业务表零接触——巡检对业务表只读，见 core/watchdog.py 模块文档与 tests/test_watchdog.py）。
-- watchdog_state：(fact_code, key) 当前态——迁移判定收敛进 watchdog_observe 的单个
-- BEGIN IMMEDIATE（与领取/逾期同款写锁先行），重启与多调度者并行由此恰一次去重。
CREATE TABLE IF NOT EXISTS watchdog_state (
    fact_code TEXT NOT NULL,
    key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '',           -- 'alert' | ''（正常）
    since_ts REAL NOT NULL DEFAULT 0,         -- 当前态起始时间
    detail TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL DEFAULT '',        -- 置为此态的事件 ID（alert 事件可追溯）
    PRIMARY KEY(fact_code, key)
);
CREATE TABLE IF NOT EXISTS watchdog_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_code TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,                       -- 'alert' | 'recovered'
    source_ts REAL NOT NULL,                  -- 事实来源时间（采集时刻）
    event_ts REAL NOT NULL,                   -- 事件落库时刻
    detail TEXT NOT NULL DEFAULT ''
);
-- SM-42 整改验收摘要导出 manifest：每张已验收工单的版本化产物台账（(ticket_id, version) 唯一）。
-- 产物文件/数据快照只落盘一次，行内 sha256 是"归档后未被篡改"的核对依据；
-- 归档与 ticket_timeline 的 acceptance_export 事件同 commit 落库（授权→改判→反馈→验收→
-- 导出全链留痕）。旧库随建表脚本自动创建（非破坏迁移，无回填）。
CREATE TABLE IF NOT EXISTS acceptance_summary_exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    template_id TEXT NOT NULL DEFAULT '',
    data_hash TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    file_hash TEXT NOT NULL DEFAULT '',
    snapshot_path TEXT NOT NULL DEFAULT '',
    snapshot_hash TEXT NOT NULL DEFAULT '',
    approved_by TEXT DEFAULT '',
    created_ts REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_acceptance_export_ver
    ON acceptance_summary_exports(ticket_id, version);
CREATE INDEX IF NOT EXISTS idx_acceptance_export_ticket
    ON acceptance_summary_exports(ticket_id);
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


def allowed_transitions(stage: str) -> tuple:
    """当前阶段的合法下一步（SM-21 R1：唯一派生点=_STAGE_TRANSITIONS，按 TICKET_STAGES 序）。

    客户端/管理端渲染"下一步动作"一律读本派生（服务端为权限与状态的唯一判断点），
    未知阶段回空元组（不发明动作）。"""
    allowed = _STAGE_TRANSITIONS.get(str(stage or ""))
    if not allowed:
        return ()
    return tuple(s for s in TICKET_STAGES if s in allowed)


def _preview(text: str, limit: int = 80) -> str:
    """消息摘要（不存原文）：压缩空白并截断。"""
    return " ".join(str(text or "").split())[:limit]


class AuditStore:
    """SQLite 审计/升级/工单存储。root 为数据库文件所在目录。"""

    def __init__(self, db_path: Optional[str] = None):
        self._path = db_path or os.getenv(
            "AUDIT_DB_PATH", os.path.join("data", "audit.db"))
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        # RLock（可重入）：SM-10 多步消费事务在持锁内复用既有写入方法
        # （create_ticket / mark_hazard_suggestion / record_ticket_event），
        # 这些方法会再次 acquire 同一把锁——Lock 会死锁，RLock 允许嵌套。
        self._lock = threading.RLock()
        # 显式多步事务标记（持锁内读写）：置位期间各写入方法**不提前 commit**，
        # 由事务收口统一 commit / 任一步失败整体 rollback（零半提交）。
        self._in_tx = False
        self._con = sqlite3.connect(self._path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.executescript(_DEFAULT_DDL)
            self._migrate_locked()
            self._commit()

    def _commit(self) -> None:
        """提交当前写事务。显式多步事务（_in_tx=True）期间不提交——收口统一 commit，
        保证"预留→去重→工单→标记→入队"要么全提交要么全回滚（SM-10 §2.1）。"""
        if not self._in_tx:
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
                # SM-20 不可变授权快照（§4.1）：非破坏加列，旧库重跑幂等零回填——
                # 历史行 mandate_json 保持空串 = legacy 标记（_ticket_dict 派生 mandate_status）。
                ("mandate_json", "TEXT DEFAULT ''"),
                ("policy_version", "TEXT DEFAULT ''"),
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
        self._con.commit()   # 票据迁移收口后再做通知迁移（两块互不拖累）
        # SM-11 通知 outbox 非破坏迁移：加列（kind/claim_generation）→ 存量 kind 按幂等键
        # 分类回填（只读扫描→定向 UPDATE）→ 旧空渠道行（pending+channel=''）一次性迁移为
        # waiting（只改状态标签，不改 channel/attempts/last_error 任何投递相关字段）。
        # 全部幂等：重跑零行；启动时随建表迁移自然执行，无需周期性扫描。
        try:
            cols = {r[1] for r in self._con.execute("PRAGMA table_info(notification_outbox)")}
            for col, ddl in (
                ("kind", "TEXT DEFAULT 'notify'"),
                ("claim_generation", "INTEGER DEFAULT 0"),
            ):
                if col not in cols:
                    self._con.execute(f"ALTER TABLE notification_outbox ADD COLUMN {col} {ddl}")
            self._con.execute(
                "UPDATE notification_outbox SET kind='escalate'"
                " WHERE kind='notify' AND (idempotency_key LIKE 'upgrade:%'"
                " OR idempotency_key LIKE 'emergency:%')")
            self._con.execute(
                "UPDATE notification_outbox SET kind='overdue'"
                " WHERE kind='notify' AND idempotency_key LIKE 'ticket:%:overdue:%'")
            self._con.execute(
                "UPDATE notification_outbox SET status='waiting'"
                " WHERE status='pending' AND channel=''")
        except sqlite3.Error as ex:  # pragma: no cover - 迁移失败不拖垮审计存储
            self._con.rollback()
            logger.warning(f"通知 outbox 迁移失败（沿用旧结构读取）: {ex}")

    def _norm_stage(self, stage: str, status: str) -> str:
        """阶段归一：stage 列缺失（理论上 _migrate 后不再出现）时按旧两态推导。"""
        return stage if stage else ("accept" if status == "closed" else "execute")

    def _ticket_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """行 → 对外字典：补 stage 派生、解析实体 JSON、保留 overdue/closure_hours。"""
        d = dict(row)
        d["stage"] = self._norm_stage(d.get("stage", ""), d.get("status", ""))
        d["stage_label"] = STAGE_LABELS.get(d["stage"], d["stage"])
        # SM-21 阶段动作派生（只读，additive）：当前阶段合法下一步由白名单唯一派生，
        # 管理端/客户端渲染动作一律消费本字段，不得自带白名单。
        d["allowed_stages"] = list(allowed_transitions(d["stage"]))
        # SM-20 授权快照状态派生（只读，不改写数据）："bound"=有快照 / "legacy"=空（旧单标记，
        # 管理端据此辨识补授权对象）。禁止伪造历史依据：legacy 永远靠补授权时间线救，不改本行。
        d["mandate_status"] = "bound" if (d.get("mandate_json") or "").strip() else "legacy"
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
            self._commit()

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
            self._commit()
            return cur.lastrowid

    def mark_notified(self, upgrade_id: int, channel: str) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE upgrade_events SET notified=1, notify_channel=? WHERE id=?",
                (channel, upgrade_id))
            self._commit()

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
            self._commit()
            return cur.rowcount > 0

    # ── 通知 outbox（持久待发送记录 + 送达/确认状态）────────────────────────
    # status 语义（严格区分，不许混用；存储 token 不改名，通知契约名映射见决策单 §2.2）：
    #   waiting          待配置（channel==''：永不被领取、attempts 恒 0、不计失败；
    #                    补配置后经 adopt_waiting_notifications 恢复为 pending）
    #   pending          待发（可领取：入队 channel≠''；adopt / replay / reap 复位后）
    #   sending          投递中（已领取，携带当次领取代次 claim_generation；悬挂 → reap 复位）
    #   sent             渠道已受理（如 HTTP 200）——不等于接收方/人工已确认
    #   ack              接收方或人工已确认（独立事实，不得由 sent 代替）
    #   failed           重试中（attempts<max，退避后可再领）
    #   failed_exhausted 失败耗尽（attempts≥max 终态，仅供人工 replay，不与"重试中"混报）
    # kind 事件分类：notify（建单/确认卡派单）/ overdue（逾期巡检）/ escalate（应急升级）。
    # 投递语义 at-least-once：渠道受理（外部事实）与本地标记 sent（本地事实）间崩溃窗口
    # 不可消除；重投 payload 逐字节相同且顶层带 idempotency_key，下游按键去重。
    _NOTIFICATION_PUBLIC_FIELDS = (
        "id", "created_ts", "idempotency_key", "request_id", "ticket_id", "upgrade_id",
        "status", "attempts", "next_attempt_ts", "last_error", "sent_ts", "acked_ts",
        "kind", "claim_generation",
    )

    @staticmethod
    def _dumps_payload(payload: Any, idempotency_key: str = "") -> str:
        """payload 允许 dict（落 JSON 字符串）或已是字符串（原样保留）。
        不可序列化时直接抛错——调用方在持锁/落库前调用，保证不留半成品行。
        idempotency_key 非空时注入 payload 顶层（SM-10 §2.5.3）：渠道侧按它对
        reap 重投的同一行去重——重投 payload 逐字节相同、行数恒 1。"""
        if isinstance(payload, str):
            return payload
        if idempotency_key and isinstance(payload, dict) and "idempotency_key" not in payload:
            payload = {**payload, "idempotency_key": idempotency_key}
        return json.dumps(payload, ensure_ascii=False)

    def _notification_public(self, row: sqlite3.Row) -> Dict[str, Any]:
        """行 → 对外字典：剔除 payload 原文（避免滥发），渠道只回是否已配置（不回完整 URL）。"""
        d = dict(row)
        out = {k: d.get(k) for k in self._NOTIFICATION_PUBLIC_FIELDS}
        out["channel_configured"] = bool(d.get("channel"))
        out["channel"] = "webhook" if d.get("channel") else ""
        return out

    def _enqueue_locked(self, idempotency_key: str, channel: str, payload_text: str,
                        request_id: str, ticket_id: Optional[int],
                        upgrade_id: Optional[int], kind: str = "notify") -> int:
        """持锁内入队（供 enqueue_notification / create_ticket / check_overdue_tickets
        复用，保证同事务）。幂等键去重：INSERT OR IGNORE，已存在则返回既有 id。
        channel 为空 → status='waiting'（待配置，不耗 attempts 不计失败，last_error 记原因），
        非空 → 'pending'（待发）。kind：notify（默认）/ overdue / escalate。"""
        if not idempotency_key:
            return 0
        empty_channel = not (channel or "").strip()
        status = "waiting" if empty_channel else "pending"
        err = "未配置通知渠道" if empty_channel else ""
        self._con.execute(
            "INSERT OR IGNORE INTO notification_outbox (created_ts, idempotency_key,"
            " channel, request_id, ticket_id, upgrade_id, payload, status, attempts,"
            " next_attempt_ts, last_error, kind) VALUES (?,?,?,?,?,?,?,?,0,0,?,?)",
            (time.time(), idempotency_key, channel or "", request_id or "", ticket_id,
             upgrade_id, payload_text, status, err, kind or "notify"))
        row = self._con.execute(
            "SELECT id FROM notification_outbox WHERE idempotency_key=?",
            (idempotency_key,)).fetchone()
        return int(row["id"]) if row else 0

    def enqueue_notification(self, idempotency_key: str, channel: str, payload: Any,
                             request_id: str = "", ticket_id: Optional[int] = None,
                             upgrade_id: Optional[int] = None,
                             kind: str = "notify") -> int:
        """入队一条待发送通知（幂等：同键重复投递不新增，返回既有 id）。
        channel 传实际 webhook URL；未配置时传 ''（仍入库，落 waiting 待配置行，
        last_error='未配置通知渠道'）。kind：notify（默认）/ overdue / escalate。"""
        text = self._dumps_payload(payload, idempotency_key)   # 落库前序列化（含幂等键注入），失败不留半成品
        with self._lock:
            oid = self._enqueue_locked(idempotency_key, channel, text, request_id,
                                       ticket_id, upgrade_id, kind)
            self._commit()
        return oid

    def claim_pending_notifications(self, limit: int = 10, max_attempts: int = 5,
                                    now: Optional[float] = None) -> List[Dict[str, Any]]:
        """原子领取待投递通知：同一批待发行只被一个消费者领走。

        跨进程安全（多 worker / uvicorn --workers N）：仅靠进程内 threading.Lock 不足以
        防止两个进程同时读到同一批 pending（各自进程锁互不相干、WAL 下 SELECT 不阻塞写），
        会造成同一幂等键被重复投递。因此：
          1) 整个"选出并置 sending"包在 `BEGIN IMMEDIATE`（SQLite 写锁）内——后到的消费者
             会阻塞到前一个提交，再 SELECT 时该行已非 pending；
          2) 每条 UPDATE 仍是**条件更新**（带 `status IN ('pending','failed')` 等原条件）
             并校验 rowcount，即便 BEGIN 因已在事务中而失败，也以 rowcount 兜底：
             只有真正把该行从待发改为 sending 的消费者才算领取成功。
        领取发代次（SM-11 §2.5）：UPDATE 同步 `claim_generation=claim_generation+1`，返回行
        携带**新代次**——送达/失败回执经 mark_notification_sent/failed 的 generation 校验，
        只能由当前代次持有者写入（reap 复位的旧悬挂消费者复活后回执被拒）。
        next_attempt_ts 同时记录"本次领取时刻"，供 reap_stale_sending 判断悬挂时长。"""
        now = time.time() if now is None else now
        with self._lock:
            try:
                self._con.execute("BEGIN IMMEDIATE")   # 抢跨进程写锁，串行化"选出→置位"
            except sqlite3.OperationalError:
                pass                                   # 已在事务中：沿用既有事务，靠 rowcount 兜底
            claimed: List[Dict[str, Any]] = []
            try:
                rows = self._con.execute(
                    "SELECT * FROM notification_outbox WHERE status IN ('pending','failed')"
                    " AND channel!='' AND attempts<? AND next_attempt_ts<=?"
                    " ORDER BY id ASC LIMIT ?", (max_attempts, now, limit)).fetchall()
                for r in rows:
                    cur = self._con.execute(
                        "UPDATE notification_outbox SET status='sending', attempts=attempts+1,"
                        " claim_generation=claim_generation+1, next_attempt_ts=?"
                        " WHERE id=? AND status IN ('pending','failed')"
                        " AND channel!='' AND attempts<? AND next_attempt_ts<=?",
                        (now, r["id"], max_attempts, now))
                    if cur.rowcount != 1:
                        continue                           # 已被其他进程领走：不重复投放
                    d = dict(r)
                    d["status"] = "sending"
                    d["attempts"] = int(d.get("attempts") or 0) + 1
                    d["claim_generation"] = int(d.get("claim_generation") or 0) + 1
                    d["next_attempt_ts"] = now
                    claimed.append(d)
                self._commit()
            except sqlite3.Error:
                self._con.rollback()
                raise
        return claimed

    def mark_notification_sent(self, notification_id: int,
                               generation: Optional[int] = None) -> bool:
        """渠道已受理（如 HTTP 200）：status='sent' + sent_ts；不代表接收方确认。
        generation 非空时校验领取代次（回执只能由当前代次持有者写入，rowcount=0 即丢弃）；
        缺省（None）保持旧行为（既有测试与旧调用零破坏）。送达后对应 (kind,channel)
        健康桶连败清零（幂等 upsert）。"""
        with self._lock:
            row = self._con.execute(
                "SELECT kind, channel FROM notification_outbox WHERE id=?",
                (notification_id,)).fetchone()
            if row is None:
                return False
            if generation is None:
                cur = self._con.execute(
                    "UPDATE notification_outbox SET status='sent', sent_ts=?, last_error=''"
                    " WHERE id=?", (time.time(), notification_id))
            else:
                cur = self._con.execute(
                    "UPDATE notification_outbox SET status='sent', sent_ts=?, last_error=''"
                    " WHERE id=? AND claim_generation=? AND status='sending'",
                    (time.time(), notification_id, generation))
            if cur.rowcount > 0:
                self._clear_health_sent_locked(row["kind"], row["channel"])
            self._commit()
            return cur.rowcount > 0

    def mark_notification_failed(self, notification_id: int, error: str,
                                backoff_s: float, max_attempts: int = 5,
                                generation: Optional[int] = None) -> bool:
        """投递失败：status='failed'（attempts<max_attempts，退避后可重试）或
        'failed_exhausted'（attempts≥max_attempts 终态，仅供人工 replay）+ last_error。
        generation 非空时校验领取代次（旧代次回执 rowcount=0 被拒，不改写当前投递）；
        缺省（None）保持旧行为。对应 (kind,channel) 健康桶连败 +1（waiting 行进不到这里）。"""
        with self._lock:
            row = self._con.execute(
                "SELECT kind, channel, attempts FROM notification_outbox WHERE id=?",
                (notification_id,)).fetchone()
            if row is None:
                return False
            now = time.time()
            exhausted = int(row["attempts"] or 0) >= int(max_attempts)
            new_status = "failed_exhausted" if exhausted else "failed"
            if generation is None:
                cur = self._con.execute(
                    "UPDATE notification_outbox SET status=?, last_error=?,"
                    " next_attempt_ts=? WHERE id=?",
                    (new_status, str(error or "")[:500],
                     now + max(0.0, float(backoff_s or 0.0)), notification_id))
            else:
                cur = self._con.execute(
                    "UPDATE notification_outbox SET status=?, last_error=?, next_attempt_ts=?"
                    " WHERE id=? AND claim_generation=? AND status='sending'",
                    (new_status, str(error or "")[:500],
                     now + max(0.0, float(backoff_s or 0.0)), notification_id, generation))
            if cur.rowcount > 0:
                self._bump_health_failed_locked(row["kind"], row["channel"])
            self._commit()
            return cur.rowcount > 0

    def ack_notification(self, notification_id: int, actor: str = "") -> bool:
        """接收方/人工确认：status='ack' + acked_ts（"已确认"是独立事实，不得由 sent 代替）。"""
        with self._lock:
            cur = self._con.execute(
                "UPDATE notification_outbox SET status='ack', acked_ts=? WHERE id=? AND status!='ack'",
                (time.time(), notification_id))
            self._commit()
            return cur.rowcount > 0

    def reap_stale_sending(self, stale_s: float = 120.0, now: Optional[float] = None) -> int:
        """复位悬挂 sending 行（进程被终止导致投递中悬挂）：status 回 pending，attempts 保留，
        代次不动（旧代次在下一次 claim 时失效——SM-11 §2.2 转移汇总）。
        悬挂时长取 max(created_ts, next_attempt_ts)——后者是领取时刻标记（表结构无独立
        领取时间列，旧库不能 ALTER），纯粹用 created_ts 会把"积压较久的待发行"误判为悬挂。"""
        now = time.time() if now is None else now
        cutoff = now - float(stale_s)
        with self._lock:
            cur = self._con.execute(
                "UPDATE notification_outbox SET status='pending'"
                " WHERE status='sending' AND MAX(created_ts, COALESCE(next_attempt_ts, 0))<?",
                (cutoff,))
            self._commit()
            return int(cur.rowcount)

    def adopt_waiting_notifications(self, channel: str) -> int:
        """补配置后恢复 waiting 行（SM-11 §2.3 裁决点 4）：channel 烙死问题的显式修复口。
        UPDATE waiting AND channel='' → pending（清 last_error），幂等、返回迁移行数；
        渠道为空时零调用零迁移。恢复随时可做，迁移只做一次（§2.4）。"""
        ch = (channel or "").strip()
        if not ch:
            return 0
        with self._lock:
            cur = self._con.execute(
                "UPDATE notification_outbox SET channel=?, status='pending', last_error=''"
                " WHERE status='waiting' AND channel=''", (ch,))
            self._commit()
            return int(cur.rowcount)

    # ── 按 (kind, channel) 健康项（SM-11 §2.6）──────────────────────────────
    def _bump_health_failed_locked(self, kind: str, channel: str) -> None:
        """投递失败：对应桶连败 +1；达 FAILURES_BEFORE_ERROR 记 breached_ts（幂等 upsert）。
        channel 为空（waiting 行理论上进不到 mark_failed）不入桶兜底。"""
        if not (channel or "").strip():
            return
        self._con.execute(
            "INSERT INTO notification_health (kind, channel, consecutive_failures)"
            " VALUES (?,?,1) ON CONFLICT(kind, channel) DO UPDATE SET"
            " consecutive_failures=consecutive_failures+1,"
            " breached_ts=CASE WHEN consecutive_failures+1>=?"
            " THEN COALESCE(breached_ts, ?) ELSE breached_ts END",
            (kind or "notify", channel, FAILURES_BEFORE_ERROR, time.time()))

    def _clear_health_sent_locked(self, kind: str, channel: str) -> None:
        """送达受理：对应桶连败清零并解除 breached（恢复即解除告警态，幂等 upsert）。"""
        if not (channel or "").strip():
            return
        self._con.execute(
            "INSERT INTO notification_health (kind, channel, consecutive_failures)"
            " VALUES (?,?,0) ON CONFLICT(kind, channel) DO UPDATE SET"
            " consecutive_failures=0, breached_ts=NULL",
            (kind or "notify", channel))

    def notification_health(self) -> List[Dict[str, Any]]:
        """健康项清单（管理端/测试用）：(kind, channel) 粒度的连败计数与达阈时间。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM notification_health ORDER BY kind, channel").fetchall()
        return [dict(r) for r in rows]

    def get_notification(self, notification_id: int) -> Optional[Dict[str, Any]]:
        """单条通知的对外视图（同 list_notifications 字段口径，不下发 payload/完整 URL）。"""
        with self._lock:
            row = self._con.execute("SELECT * FROM notification_outbox WHERE id=?",
                                    (notification_id,)).fetchone()
        return self._notification_public(row) if row else None

    def list_notifications(self, status: Optional[str] = None,
                           limit: int = 100) -> List[Dict[str, Any]]:
        """通知清单（管理端/报表）：解析 payload JSON 但不回 payload 原文，渠道只回是否已配置。"""
        with self._lock:
            if status:
                rows = self._con.execute(
                    "SELECT * FROM notification_outbox WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = self._con.execute(
                    "SELECT * FROM notification_outbox ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
        return [self._notification_public(r) for r in rows]

    def replay_notification(self, notification_id: int) -> bool:
        """人工重放：复位为 pending、attempts=0、退避清零、错误清空，代次 +1
        （一切在途领取作废——SM-11 §2.2 转移汇总）。"""
        with self._lock:
            cur = self._con.execute(
                "UPDATE notification_outbox SET status='pending', attempts=0,"
                " next_attempt_ts=0, last_error='', sent_ts=NULL,"
                " claim_generation=claim_generation+1 WHERE id=?",
                (notification_id,))
            self._commit()
            return cur.rowcount > 0

    def notification_stats(self) -> Dict[str, Any]:
        """各状态计数 + 总数（管理端与报表用；failed 与 failed_exhausted 分开——
        "需人工介入"清单可直查，不与"重试中"混报；waiting 单独可见——"待配置"
        不再伪装成"待发"。键集合只增不减（SM-11 §2.8.2）。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT status, COUNT(*) AS n FROM notification_outbox GROUP BY status").fetchall()
        out: Dict[str, Any] = {s: 0 for s in ("pending", "sending", "sent", "ack",
                                              "failed", "waiting", "failed_exhausted")}
        total = 0
        for r in rows:
            out[r["status"]] = int(r["n"])
            total += int(r["n"])
        out["total"] = total
        return out

    # ── 工单（五阶段回路）───────────────────────────────────────────────────
    def create_ticket(self, title: str, type_: str = "hazard",
                      priority: str = "normal", assignee: str = "",
                      source_request_id: str = "", due_ts: Optional[float] = None,
                      brief: Optional[Dict[str, Any]] = None,
                      actor: str = "system", user_id: str = "",
                      severity: str = "", responsible_dept: str = "",
                      notification: Optional[Dict[str, Any]] = None,
                      mandate: Optional[Dict[str, Any]] = None) -> int:
        """建单并落入需求简报（brief）阶段。brief 缺省时（管理端手动建单）
        以标题生成最小简报；request_id 经 source_request_id 落库。
        简报字段：intent / urgency / entities(实体) / message_digest(消息摘要) /
        routing_reason / request_id —— 全部与本行一起落库。
        隐患工单补充字段：severity（major/general 建议定级）、responsible_dept
        （派发部门）、user_id（上报人，去重与报表用）。
        notification（可选，默认 None = 旧行为）：形如
        {"idempotency_key","channel","payload","request_id","ticket_id?","upgrade_id?","kind?"}，
        与本行在**同一次持锁、同一次 commit** 内插入 notification_outbox——
        "工单变化与待发送记录同事务落库"，进程退出后通知仍可恢复投递。
        kind 缺省 'notify'；应急路径传 'escalate'（SM-11 §2.2 事件分类）。
        mandate（可选，SM-20 §4.1）：不可变授权快照（admission.mandate_snapshot 构造），
        mandate_json 与 policy_version 两列**在既有同一次持锁/commit 内**与工单行一起写入
        （故障注入下随事务整体回滚，零半提交）；不传 = 不写快照（旧单 legacy 标记），
        该列此后只能被读取，任何改判/合并/回退路径都改写不了它。"""
        if brief is None:
            brief = {"intent": "", "urgency": "", "entities": {},
                     "message_digest": _preview(title, 80),
                     "routing_reason": "管理员手动建单", "request_id": source_request_id}
        # payload / mandate 序列化在持锁之前完成：不可序列化直接抛错，不留半成品行。
        notification_text = None
        if notification:
            notification_text = self._dumps_payload(notification.get("payload", {}),
                                                    notification.get("idempotency_key", ""))
        mandate_text = ""
        mandate_pv = ""
        if isinstance(mandate, dict) and mandate:
            mandate_text = json.dumps(mandate, ensure_ascii=False, sort_keys=True)
            mandate_pv = str(mandate.get("policy_version", "") or "")
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO tickets (created_ts, title, type, priority, assignee,"
                " source_request_id, due_ts, stage, intent, urgency, entities,"
                " message_digest, routing_reason, severity, responsible_dept, user_id,"
                " mandate_json, policy_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), _preview(title, 120), type_, priority, assignee,
                 source_request_id, due_ts, "brief",
                 str(brief.get("intent", "")), str(brief.get("urgency", "")),
                 json.dumps(brief.get("entities") or {}, ensure_ascii=False),
                 _preview(str(brief.get("message_digest", "")), 120),
                 _preview(str(brief.get("routing_reason", "")), 120),
                 str(severity or ""), str(responsible_dept or ""), str(user_id or ""),
                 mandate_text, mandate_pv))
            tid = cur.lastrowid
            brief_store = dict(brief)
            brief_store["request_id"] = brief_store.get("request_id") or source_request_id
            self._con.execute(
                "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                (tid, time.time(), "create", "", "brief", actor, _preview(title, 80),
                 json.dumps(brief_store, ensure_ascii=False)))
            if notification and notification_text is not None:
                self._enqueue_locked(
                    notification.get("idempotency_key", ""),
                    notification.get("channel", "") or "",
                    notification_text,
                    notification.get("request_id", "") or source_request_id,
                    notification.get("ticket_id") or tid,
                    notification.get("upgrade_id"),
                    notification.get("kind") or "notify")
            self._commit()
            return tid

    def _ticket_row_locked(self, ticket_id: int) -> Optional[sqlite3.Row]:
        return self._con.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()

    def get_ticket(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._ticket_row_locked(ticket_id)
        return self._ticket_dict(row) if row else None

    def find_ticket_by_request(self, request_id: str,
                               type_: Optional[str] = None) -> Optional[int]:
        """按来源上报编号取最近一张关联工单的 id（幂等建单：同编号 + 同类型不重复建）。
        无关联工单返回 None。"""
        with self._lock:
            if type_:
                row = self._con.execute(
                    "SELECT id FROM tickets WHERE source_request_id=? AND type=?"
                    " ORDER BY id DESC LIMIT 1", (request_id, type_)).fetchone()
            else:
                row = self._con.execute(
                    "SELECT id FROM tickets WHERE source_request_id=?"
                    " ORDER BY id DESC LIMIT 1", (request_id,)).fetchone()
        return int(row["id"]) if row else None

    def owner_of_request(self, request_id: str) -> Optional[str]:
        """只读：按上报编号取归属主体（工单非空 user_id 优先，其次升级事件 user_id）。

        S-05 用户端进度查询的数据边界依据——上报编号本身即凭据，但多用户形态下还必须是
        该编号的归属主体；无任何记录（或归属为空）返回 None，端点据此按无权限处理，
        不泄露存在性。刻意只挑非空归属：应急快速分流建单不写 user_id，其归属由同次
        升级事件承载，不能因为工单缺字段就判定"无人拥有"。
        """
        with self._lock:
            row = self._con.execute(
                "SELECT user_id FROM tickets WHERE source_request_id=? AND user_id!=''"
                " ORDER BY id DESC LIMIT 1", (request_id,)).fetchone()
            if row is None:
                row = self._con.execute(
                    "SELECT user_id FROM upgrade_events WHERE request_id=?"
                    " ORDER BY id DESC LIMIT 1", (request_id,)).fetchone()
        if row is None:
            return None
        return str(row["user_id"] or "") or None

    def transition_ticket(self, ticket_id: int, to_stage: str,
                          actor: str = "", note: str = "",
                          detail: Optional[Dict[str, Any]] = None,
                          operator_subject: str = "",
                          operator_is_admin: bool = False,
                          subject_check: Optional[Callable[[str], bool]] = None) -> Dict[str, Any]:
        """五阶段流转：白名单校验（顺序前进 + accept/reject 回退 feedback），
        非法跳转/未知阶段/重复同阶段一律拒绝；成功则更新 stage 与派生 status，
        并把 操作者/批注/时间戳 写入 ticket_timeline。持同一把线程锁保证原子。

        SM-20 §5 续期校验：两个终态回退（accept→feedback 验收后追加反馈轮、
        reject→feedback 驳回重做）前校验授权快照仍成立（mandate_revalidate）——
        不成立拒绝流转并留痕 mandate_reauth_required（不静默复活）；存在未过期的
        mandate_reauth 补授权事件则放行（reason=reauthorized，不改写快照）。
        向前流转与中间阶段推进不校验（首建已绑快照，终态才谈"复活"）。
        operator_subject/operator_is_admin/subject_check 由调用方注入（api 端传
        服务端主体；store 级测试缺省 = 不做跨主体校验、撤权接 core/auth 目录）。"""
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
            if from_stage in ("accept", "reject") and to_stage == "feedback":
                gate = self._mandate_gate_locked(
                    row, now, operator_subject=operator_subject,
                    operator_is_admin=operator_is_admin, subject_check=subject_check)
                if not gate["ok"]:
                    self._con.execute(
                        "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                        " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                        (ticket_id, now, "mandate_reauth_required", from_stage, "",
                         actor or "system", gate["error"],
                         json.dumps({"reason": gate["reason"], "kind": gate["kind"],
                                     "policy_version": gate["policy_version"],
                                     "operator_subject": str(operator_subject or "")},
                                    ensure_ascii=False)))
                    self._commit()
                    return {"ok": False, "kind": gate["kind"], "reason": gate["reason"],
                            "from_stage": from_stage, "error": gate["error"]}
            closing = to_stage in ("accept", "reject")
            # 条件更新（SM-10 §2.3.4）："读 stage → 校验 → UPDATE"的 check-then-act 在
            # 并发下有两个赢者——WHERE stage=? 兜底，败者 rowcount=0 落结构化冲突 + 审计行。
            if closing:
                cur = self._con.execute(
                    "UPDATE tickets SET stage=?, status='closed', closed_ts=?"
                    " WHERE id=? AND stage=?",
                    (to_stage, now, ticket_id, from_stage))
            else:  # 回退/推进到中间阶段：恢复在办态，闭环时长随之失效
                cur = self._con.execute(
                    "UPDATE tickets SET stage=?, status='open', closed_ts=NULL"
                    " WHERE id=? AND stage=?",
                    (to_stage, ticket_id, from_stage))
            if cur.rowcount != 1:
                latest = self._ticket_row_locked(ticket_id)
                current_stage = self._norm_stage(latest["stage"], latest["status"]) \
                    if latest is not None else from_stage
                conflict_detail = {"expected": {"stage": from_stage},
                                   "current": {"stage": current_stage},
                                   "requested": to_stage}
                self._con.execute(
                    "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                    " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                    (ticket_id, now, "stage_conflict", from_stage, "",
                     actor or "system", "工单阶段已被并发修改，本次流转被拒",
                     json.dumps(conflict_detail, ensure_ascii=False)))
                self._commit()
                return {"ok": False, "kind": "conflict", "from_stage": from_stage,
                        "current_stage": current_stage,
                        "error": f"工单阶段已被并发修改（当前 "
                                 f"{STAGE_LABELS.get(current_stage, current_stage)}），"
                                 f"请刷新后重试"}
            self._con.execute(
                "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, now, "stage_change", from_stage, to_stage,
                 actor, note, json.dumps(detail or {}, ensure_ascii=False)))
            self._commit()
            new_row = self._ticket_row_locked(ticket_id)
            return {"ok": True, "from_stage": from_stage, "to_stage": to_stage,
                    "ticket": self._ticket_dict(new_row)}

    def _mandate_gate_locked(self, row: sqlite3.Row, now: float, *,
                             operator_subject: str = "",
                             operator_is_admin: bool = False,
                             subject_check: Optional[Callable[[str], bool]] = None) -> Dict[str, Any]:
        """终态回退前的快照续期闸门（SM-20 §5，持锁内调用，**只读**快照绝不写）。

        裁决 = core.admission.mandate_revalidate（§5.2 原因集）+ 补授权救回：
        裁决不通过但存在未过期 mandate_reauth 时间线事件 → 放行（reason=reauthorized，
        补授权事件同样受有效期约束，过期需重新补授权）。快照缺失（legacy）→ 拒绝，
        kind=mandate_legacy；其余不成立 → kind=mandate_stale。"""
        from core import admission as _admission
        pv_now = _admission.policy_version()
        raw = ""
        try:
            raw = (row["mandate_json"] or "").strip()
        except (IndexError, KeyError):
            raw = ""
        if not raw:
            verdict = {"ok": False, "reason": "legacy"}
        else:
            try:
                mandate = json.loads(raw)
            except (ValueError, TypeError):
                mandate = None
            if not isinstance(mandate, dict):
                verdict = {"ok": False, "reason": "bad_schema"}   # JSON 不可解析 = 依据不可信
            else:
                verdict = _admission.mandate_revalidate(
                    mandate, current_policy=pv_now,
                    operator_subject=operator_subject,
                    operator_is_admin=operator_is_admin,
                    subject_check=subject_check, now=now)
        if verdict.get("ok"):
            return {"ok": True, "reason": str(verdict.get("reason") or "ok"),
                    "kind": "", "policy_version": pv_now}
        # 补授权救回（§5.3）：只追加过的 mandate_reauth 事件有效且未过期才放行
        reauth = self._con.execute(
            "SELECT ts FROM ticket_timeline WHERE ticket_id=? AND action='mandate_reauth'"
            " ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        if reauth is not None:
            try:
                reauth_ts = float(reauth["ts"] or 0)
            except (TypeError, ValueError):
                reauth_ts = 0.0
            if reauth_ts > 0 and now - reauth_ts <= _admission.mandate_max_age_hours() * 3600:
                return {"ok": True, "reason": "reauthorized", "kind": "",
                        "policy_version": pv_now}
        reason = str(verdict.get("reason") or "legacy")
        kind = "mandate_legacy" if reason == "legacy" else "mandate_stale"
        hint = ("旧工单无授权快照，请管理员补授权后重试（POST /tickets/{id}/reauth）"
                if kind == "mandate_legacy" else
                f"授权快照已失效（{reason}），请管理员补授权后重试（POST /tickets/{{id}}/reauth）")
        return {"ok": False, "reason": reason, "kind": kind,
                "policy_version": pv_now, "error": hint}

    def record_mandate_reauth(self, ticket_id: int, actor: str, note: str = "",
                              policy_version: str = "") -> bool:
        """补授权（SM-20 §5.3）：追加 mandate_reauth 时间线事件（admin/policy 版本/
        时间/批注全落 detail），**不改写 mandate_json、不改历史时间线**——禁止伪造
        历史依据，补授权以新事件留痕且受有效期约束。工单不存在返回 False。"""
        from core import admission as _admission
        with self._lock:
            row = self._ticket_row_locked(ticket_id)
            if row is None:
                return False
            pv = str(policy_version or "").strip() or _admission.policy_version()
            now = time.time()
            self._con.execute(
                "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                (ticket_id, now, "mandate_reauth", self._norm_stage(row["stage"], row["status"]),
                 "", actor or "admin", note or "人工补授权",
                 json.dumps({"policy_version": pv, "reauth_ts": now, "admin": actor or "admin"},
                            ensure_ascii=False)))
            self._commit()
        return True

    # ── SM-20 影子裁决（hazard_observations，observe 档旁路写入）──────────────
    def record_hazard_observation(self, *, request_id: str, user_id: str = "",
                                  conv_id: str = "", policy_version: str = "",
                                  would_accept: bool = False, decision: str = "",
                                  reason: str = "", action_proposed: str = "",
                                  severity: str = "", category: str = "",
                                  basis: str = "", dept: str = "",
                                  deadline_hours: Optional[float] = None,
                                  current_action: str = "",
                                  current_ticket_id: Optional[int] = None) -> int:
        """旁路影子写入（observe 档唯一增量）：同上报编号一条（INSERT OR IGNORE 幂等）。
        独立于业务事务（旁路语义：业务写入照旧，本行只增裁决数据，写失败只告警由
        调用方兜底）。返回观测行 id（已存在返回 0）。"""
        with self._lock:
            cur = self._con.execute(
                "INSERT OR IGNORE INTO hazard_observations (created_ts, request_id, user_id,"
                " conv_id, policy_version, would_accept, decision, reason, action_proposed,"
                " severity, category, basis, dept, deadline_hours, current_action,"
                " current_ticket_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), request_id, str(user_id or ""), str(conv_id or ""),
                 str(policy_version or ""), int(bool(would_accept)), str(decision or ""),
                 str(reason or ""), str(action_proposed or ""), str(severity or ""),
                 str(category or ""), str(basis or ""), str(dept or ""),
                 deadline_hours, str(current_action or ""), current_ticket_id))
            self._commit()
            return int(cur.lastrowid or 0)

    def list_hazard_observations(self, request_id: Optional[str] = None,
                                 limit: int = 100) -> List[Dict[str, Any]]:
        """影子观测查询（管理端/评测用）：按编号过滤或取最近 limit 条（新→旧）。"""
        with self._lock:
            if request_id:
                rows = self._con.execute(
                    "SELECT * FROM hazard_observations WHERE request_id=? ORDER BY id DESC"
                    " LIMIT ?", (request_id, limit)).fetchall()
            else:
                rows = self._con.execute(
                    "SELECT * FROM hazard_observations ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

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
            self._commit()
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
            self._commit()
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
            self._commit()

    def classify_hazard_suggestion(self, request_id: str,
                                   max_age_hours: float = 24.0) -> str:
        """建议行状态判定（只读，不改任何状态）：missing / handled / expired / pending。

        与 get_hazard_suggestion 的过滤条件同源（pending = 存在且未处理且未过期）；
        供确认端点区分"不存在/已处理（404）"与"已过期（409 冲突）"两种拒绝口径——
        过期不是"不存在"：行还在库里，必须给用户一个可行动的冲突结论（重新上报），
        且落 expired 痕后同收据重新上报即可回放（INSERT OR REPLACE 复位 pending）。
        已落 expired 痕的行重复确认仍按 expired（幂等冲突，不回退成 404）。"""
        with self._lock:
            row = self._con.execute(
                "SELECT status, ticket_id, created_ts FROM hazard_suggestions"
                " WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return "missing"
        if row["status"] == "expired":
            return "expired"
        if row["status"] != "pending" or row["ticket_id"] is not None:
            return "handled"
        if time.time() - row["created_ts"] > max_age_hours * 3600:
            return "expired"
        return "pending"

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
            self._commit()
            return cur.rowcount > 0

    def list_hazard_suggestions(self, status: Optional[str] = None,
                                limit: int = 1000) -> List[Dict[str, Any]]:
        """建议卡列表（SM-22 只读聚合面：确认率/确认时长/待复核清单的数据源）。
        status 过滤可选；按创建时间倒序；payload 原样返回（解析交给调用方）。"""
        with self._lock:
            if status:
                rows = self._con.execute(
                    "SELECT * FROM hazard_suggestions WHERE status=?"
                    " ORDER BY created_ts DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = self._con.execute(
                    "SELECT * FROM hazard_suggestions ORDER BY created_ts DESC LIMIT ?",
                    (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
            except (ValueError, TypeError):
                d["payload"] = {}
            out.append(d)
        return out

    # ── SM-10：带显式幂等键的写入事务（预留→去重→工单→标记→入队 单 BEGIN IMMEDIATE）──
    def _reserve_op_locked(self, action: str, subject: str, idem_key: str,
                           target: str, request_hash: str) -> Dict[str, Any]:
        """持锁 + 显式事务内插入幂等预留行（status='applied'）。

        撞部分唯一索引（并发或顺序上的同键重放/冲突）→ 语句级回滚、事务仍活 →
        重查已 applied 行裁决：同哈希 → 追加 replayed 痕并返回原收据（重放唯一数据源）；
        异哈希 → conflicted 痕先落库（expected/current 双方哈希，可事后回放）再由调用方抛 409。
        返回 {"kind": "reserved", "op_id": ...} | {"kind": "replayed", "receipt": ...}
            | {"kind": "conflict", "detail": ...}。"""
        try:
            cur = self._con.execute(
                "INSERT INTO hazard_ops (created_ts, action, subject, idem_key, target,"
                " request_hash, status, receipt) VALUES (?,?,?,?,?,?,'applied','')",
                (time.time(), action, subject, idem_key, target, request_hash))
            return {"kind": "reserved", "op_id": int(cur.lastrowid or 0)}
        except sqlite3.IntegrityError:
            row = self._con.execute(
                "SELECT * FROM hazard_ops WHERE action=? AND subject=? AND idem_key=?"
                " AND status='applied' ORDER BY id DESC LIMIT 1",
                (action, subject, idem_key)).fetchone()
            if row is not None and row["request_hash"] == request_hash and row["receipt"]:
                self._con.execute(
                    "INSERT INTO hazard_ops (created_ts, action, subject, idem_key, target,"
                    " request_hash, status) VALUES (?,?,?,?,?,?,'replayed')",
                    (time.time(), action, subject, idem_key, target, request_hash))
                # replayed 痕的提交由调用方收口（痕不参与唯一裁决）
                return {"kind": "replayed", "receipt": json.loads(row["receipt"])}
            detail = {
                "expected": {"request_hash": row["request_hash"] if row else "",
                             "target": row["target"] if row else ""},
                "current": {"request_hash": request_hash, "target": target},
            }
            self._con.execute(
                "INSERT INTO hazard_ops (created_ts, action, subject, idem_key, target,"
                " request_hash, status, error_code, detail)"
                " VALUES (?,?,?,?,?,?,'conflicted',?,?)",
                (time.time(), action, subject, idem_key, target, request_hash,
                 "IDEMPOTENCY_CONFLICT", json.dumps(detail, ensure_ascii=False)))
            # 冲突行先落库再抛（映射文档 §1：冲突也是审计行）；提交由调用方收口
            return {"kind": "conflict", "detail": detail}

    def consume_hazard_suggestion(
            self, request_id: str, user_id: str, actor: str,
            idem_action: str, idem_subject: str, idem_key: str, request_hash: str,
            new_ticket_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]],
                                             Dict[str, Any]]] = None,
            notify_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
            require_owner: Optional[str] = None,
            max_age_hours: float = 24.0) -> Dict[str, Any]:
        """带显式幂等键的确认消费（SM-10 §2.1 五步一个 BEGIN IMMEDIATE 写事务）：

          1. 幂等预留（hazard_ops applied 行，撞唯一索引=重放/冲突裁决，见 _reserve_op_locked）；
          2. 建议行判定 + 去重 + 工单变更（未命中→建 tickets+timeline；命中→repeat_report 一行）；
          3. 建议状态迁移 pending→confirmed/merged（+ticket_id）；
          4. outbox 入队（INSERT OR IGNORE，同键不新增）；
          5. 收据回填 hazard_ops.receipt → 统一 commit。

        任一步失败（含故障注入）→ 全体回滚：建议仍 pending、零工单、零时间线、零 outbox 行、
        预留行消失，同键重试重新执行完整消费（零半提交）。

        **预留裁决先于建议读取**：同键并发/重试的第二消费者在预留步即拿到原收据返回，
        绝不读建议行（赢家提交后建议已 handled，先读会误 404——T1 竞态的根因）。
        建议不在 pending（missing/handled）→ 回滚（预留行消失）→ 调用方 404；
        已过期 → 回滚 → 调用方走既有 expired 独立事务 + 409（SM-01 契约保留）。

        new_ticket_fn(decision, brief) → create_ticket 的业务参数（title/priority/due_ts/
        severity/responsible_dept，由 api 层单一推导）；notify_fn(decision) →
        {"channel","text","reason"}（outbox payload 由本方法组装并注入幂等键）。
        返回 kind: applied（含 ticket_id/merged/receipt）/ replayed（含 receipt）/
        conflict（含 detail）/ forbidden / rejected（state=missing|handled）/ expired。
        require_owner 非 None 时校验建议归属（隔离形态的服务端主体），不一致回滚并回 forbidden。"""
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            self._in_tx = True
            try:
                res = self._reserve_op_locked(idem_action, idem_subject, idem_key,
                                              request_id, request_hash)
                if res["kind"] in ("replayed", "conflict"):
                    self._con.commit()   # 裁决痕收口（重放/冲突不产生任何业务行）
                    return res
                # 事务内读建议行（此刻建议状态在写锁保护下，无 check-then-act 窗口）
                row = self._con.execute(
                    "SELECT * FROM hazard_suggestions WHERE request_id=?",
                    (request_id,)).fetchone()
                if row is None:
                    self._con.rollback()
                    return {"kind": "rejected", "state": "missing"}
                # 归属校验（S-05，隔离形态）：主体不一致 → 回滚预留行 → 调用方 403。
                # 事务内判定避免"先读后写"窗口，且 403 与 404 的既有优先级不变。
                if require_owner is not None and str(row["user_id"] or "") != require_owner:
                    self._con.rollback()
                    return {"kind": "forbidden"}
                if row["status"] == "expired":
                    self._con.rollback()
                    return {"kind": "expired"}
                if row["status"] != "pending" or row["ticket_id"] is not None:
                    self._con.rollback()
                    return {"kind": "rejected", "state": "handled"}
                if time.time() - row["created_ts"] > max_age_hours * 3600:
                    self._con.rollback()
                    return {"kind": "expired"}
                try:
                    payload = json.loads(row["payload"]) if row["payload"] else {}
                except (ValueError, TypeError):
                    payload = None
                if not isinstance(payload, dict):
                    self._con.rollback()
                    return {"kind": "rejected", "state": "handled"}
                decision = payload.get("decision") or {}
                brief = payload.get("brief") or {}
                # 步骤 2：去重判定（self.find_open_hazard_duplicate 为测试注入点）与其结果写入
                dup = self.find_open_hazard_duplicate(
                    user_id, brief.get("entities") or {})
                if dup is not None:
                    tid, merged = int(dup["id"]), True
                    self.record_ticket_event(
                        tid, "repeat_report", actor=actor,
                        note="24 小时内同实体重复上报，合并进本工单",
                        detail={"request_id": request_id,
                                "severity": str(decision.get("severity", "") or "")})
                else:
                    p = dict(new_ticket_fn(decision, brief) if new_ticket_fn else {})
                    title = p.pop("title", "")
                    # SM-20 §4.1：确认卡是用户显式动作（explicit），快照主体绑定上报人，
                    # 与工单行同事务写入（本方法整体一个 BEGIN IMMEDIATE，零半提交）。
                    from core import admission as _admission
                    mandate = _admission.mandate_snapshot(
                        source_type="hazard_confirm", strength="explicit",
                        subject_id=str(user_id or ""), scopes=[],
                        rule_id="manual.confirm", basis="confirm",
                        proposal_id=request_id, evidence_ids=[f"audit:{request_id}"])
                    tid = self.create_ticket(
                        title, type_="hazard", source_request_id=request_id,
                        brief=brief, actor=actor, user_id=user_id, mandate=mandate, **p)
                    merged = False
                # 步骤 3：建议状态迁移（self.mark_hazard_suggestion 为测试注入点；
                # RLock 重入 + _in_tx 期间不提前 commit）
                self.mark_hazard_suggestion(
                    request_id, "merged" if merged else "confirmed", ticket_id=tid)
                # 步骤 4：outbox 入队（同事务，对齐应急路径 create_ticket+outbox 形态）
                if notify_fn is not None:
                    n = notify_fn(decision) or {}
                    key = f"ticket:{tid}:{n.get('reason') or 'create'}:webhook"
                    n_payload = {"request_id": request_id, "text": str(n.get("text", "")),
                                 "timeline": {"ticket_id": tid, "note": "派单 webhook 已实收",
                                              "to_stage": "brief", "request_id": request_id}}
                    self._enqueue_locked(key, n.get("channel", "") or "",
                                         self._dumps_payload(n_payload, key),
                                         request_id, tid, None, "notify")
                # 步骤 5：收据回填 + 统一提交
                receipt = {"ok": True, "ticket_id": tid, "merged": merged}
                self._con.execute("UPDATE hazard_ops SET receipt=? WHERE id=?",
                                  (json.dumps(receipt, ensure_ascii=False),
                                   res["op_id"]))
                self._con.commit()
                return {"kind": "applied", "ticket_id": tid, "merged": merged,
                        "receipt": receipt}
            except BaseException:
                try:
                    self._con.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                self._in_tx = False

    def reserve_emergency_ticket(self, idem_key: str, target: str, request_hash: str,
                                 user_id: str, conv_id: str, message: str,
                                 intent_value: str, title: str, brief: Dict[str, Any],
                                 channel: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """应急快速分流建单（SM-10 §2.4）：幂等预留 + 升级事件 + 建单（含 outbox 同事务入队）
        一个 BEGIN IMMEDIATE——同一上报编号绝不产生第二张单/第二条升级事件。

        败者撞唯一索引 → 重查 applied 行 → 重放已有 ticket_id（绝不新建）；
        同 rid 异内容落 conflicted 痕可回放，但**仍重放既有单**（快速分流绝不抛出、
        行为零弱化：同号检查的既有幂等语义保持）。返回 {"kind": "applied"|"replayed",
        "ticket_id", "receipt"?}。"""
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            self._in_tx = True
            try:
                res = self._reserve_op_locked("emergency_ticket", "", idem_key,
                                              target, request_hash)
                if res["kind"] == "replayed":
                    self._con.commit()
                    return {"kind": "replayed",
                            "ticket_id": int(res["receipt"].get("ticket_id") or 0)}
                if res["kind"] == "conflict":
                    row = self._con.execute(
                        "SELECT receipt FROM hazard_ops WHERE action=? AND subject=?"
                        " AND idem_key=? AND status='applied' ORDER BY id DESC LIMIT 1",
                        ("emergency_ticket", "", idem_key)).fetchone()
                    receipt = json.loads(row["receipt"]) if row and row["receipt"] else {}
                    self._con.commit()   # 冲突痕/重放收口：快速分流不抛出，仍重放既有单
                    return {"kind": "replayed", "ticket_id": int(receipt.get("ticket_id") or 0)}
                upgrade_id = self.add_upgrade(target, user_id, conv_id, message, intent_value)
                # SM-20 §4.1：快速分流是确定性零 LLM 依据（emergency.precheck），建单同事务
                # 写不可变授权快照（三档都写——本方法即授权链的权威入口，红灯 T3 钉死）。
                from core import admission as _admission
                mandate = _admission.mandate_snapshot(
                    source_type="emergency_fastpath", strength="explicit",
                    subject_id=str(user_id or ""), scopes=[],
                    rule_id="emergency.precheck", basis="urgency",
                    proposal_id=target,
                    evidence_ids=[f"audit:{target}", f"upgrade:{upgrade_id}"])
                ticket_id = self.create_ticket(
                    title, type_="emergency", priority="critical",
                    source_request_id=target, brief=brief, mandate=mandate,
                    notification={"idempotency_key": idem_key, "channel": channel,
                                  "payload": payload, "request_id": target,
                                  "upgrade_id": upgrade_id, "kind": "escalate"})
                receipt = {"ticket_id": int(ticket_id), "upgrade_id": int(upgrade_id)}
                self._con.execute("UPDATE hazard_ops SET receipt=? WHERE id=?",
                                  (json.dumps(receipt, ensure_ascii=False), res["op_id"]))
                self._con.commit()
                return {"kind": "applied", "ticket_id": int(ticket_id), "receipt": receipt}
            except BaseException:
                try:
                    self._con.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                self._in_tx = False

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

    def check_overdue_tickets(self, channel: str = "") -> List[Dict[str, Any]]:
        """逾期巡检（后台循环调用）：判重 → 记 overdue_notify 事件 → outbox 入队（kind='overdue'）
        收敛为**每张新逾期单一个 BEGIN IMMEDIATE**（SM-11 §2.1，SM-10 §5.2.1 同款次序：
        写锁先行、判重在锁内）——事件与通知要么同 commit 要么同回滚，"事件已提交而通知
        永不入队"的崩溃窗口与"两进程各记一条事件"的 check-then-act 竞态同时消除。
        已有该事件的逾期单不重复返回——幂等，通知只发一次。
        channel 为值班 webhook（可空：空渠道入 waiting 待配置行，不再"永不通知"）；
        现有调用点无参调用零破坏（缺省 ''）。webhook 投递与补痕留在事务外（网络 IO
        进事务会长持写锁且渠道半成功不可回滚）。
        入队经 self._enqueue_locked（故障注入面，与 SM-10 同款实例属性 monkeypatch）；
        注入失败 → 整体回滚 → 重扫自然重试（恢复的唯一重试来源）。"""
        newly: List[Dict[str, Any]] = []
        for t in self.list_overdue_tickets():
            with self._lock:
                self._con.execute("BEGIN IMMEDIATE")
                self._in_tx = True
                appended = False
                try:
                    dup = self._con.execute(
                        "SELECT 1 FROM ticket_timeline WHERE ticket_id=?"
                        " AND action='overdue_notify'", (t["id"],)).fetchone()
                    if dup is None:
                        ok = self.record_ticket_event(
                            t["id"], "overdue_notify", actor="system",
                            note=f"整改期限已过（逾期 {round((time.time() - t['due_ts']) / 3600, 1)}h），升级提醒",
                            to_stage=t["stage"])
                        if ok:
                            key = f"ticket:{t['id']}:overdue:webhook"
                            text = (f"【SafetyMind 工单逾期】#{t['id']} {t['title']}\n"
                                    f"整改期限已过仍未闭环，请立即督办。\n"
                                    f"时间: {time.strftime('%m-%d %H:%M:%S')}")
                            payload = {"request_id": t.get("source_request_id", ""),
                                       "text": text,
                                       "timeline": {"ticket_id": t["id"],
                                                    "note": "派单 webhook 已实收",
                                                    "to_stage": "brief",
                                                    "request_id": t.get("source_request_id", "")}}
                            self._enqueue_locked(key, channel,
                                                 self._dumps_payload(payload, key),
                                                 t.get("source_request_id", ""), t["id"],
                                                 None, "overdue")
                            appended = True
                    self._con.commit()
                except BaseException:
                    try:
                        self._con.rollback()
                    except sqlite3.Error:
                        pass
                    raise
                finally:
                    self._in_tx = False
                if appended:
                    newly.append(t)
        return newly

    # ── SM-40 只读巡检：卡单只读查询 + 巡检状态机（自有两表，业务表零接触）──────
    def list_stuck_tickets(self, stale_s: float, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """卡单只读扫描（SM-40）：open 工单且最后一条时间线事件距今超过 stale_s；
        无时间线行的单用 created_ts 兜底（建单即卡）。纯 SELECT，零写入。
        stale_s 为负数时全部 open 单视为卡单（测试确定性缝）。"""
        now = time.time() if now is None else now
        cutoff = now - float(stale_s)
        with self._lock:
            rows = self._con.execute(
                "SELECT t.* FROM tickets t WHERE t.status!='closed' AND COALESCE("
                " (SELECT MAX(tl.ts) FROM ticket_timeline tl WHERE tl.ticket_id=t.id),"
                " t.created_ts) < ? ORDER BY t.id ASC", (cutoff,)).fetchall()
        return [self._ticket_dict(r) for r in rows]

    def watchdog_observe(self, events: List[Dict[str, Any]],
                         now: Optional[float] = None) -> List[Dict[str, Any]]:
        """巡检状态机（SM-40 去重唯一依据，调用方 core/watchdog.py）：

        events = [{"fact_code","key","bad":bool,"detail":str,"source_ts":float}]。
        整批收敛进**单个 BEGIN IMMEDIATE**（迁移判定在锁内读 watchdog_state）：
          - bad 且先前非 alert → 恰一条 alert 事件；
          - 状态未变 → 零新事件（静默）；
          - 非 bad 且先前 alert → 恰一条 recovered 事件；
          - 非 bad 且先前非 alert → 零新事件。
        重启（状态从库读）与两个调度者并行（写锁串行化，后到者在锁内读到已迁移状态）
        由此都恰一次去重。事件 ID = "WD-EV-<watchdog_events 自增 id>"（稳定可追溯）。
        返回本次新建事件列表（含 event_id）；存储不可用时抛出由调用方按 fail-closed 处理
        （不写任何行，绝不因读不到而误报恢复）。"""
        now = time.time() if now is None else now
        created: List[Dict[str, Any]] = []
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            self._in_tx = True
            try:
                for ev in events:
                    code = str(ev.get("fact_code") or "")
                    key = str(ev.get("key") or "")
                    if not code:
                        continue
                    bad = bool(ev.get("bad"))
                    cur_state = "alert" if bad else ""
                    row = self._con.execute(
                        "SELECT state FROM watchdog_state WHERE fact_code=? AND key=?",
                        (code, key)).fetchone()
                    prev = str(row["state"]) if row is not None else ""
                    if prev == cur_state:
                        continue                          # 状态未变：静默（并行第二次到达也在此沉默）
                    kind = "alert" if bad else "recovered"
                    detail = str(ev.get("detail") or "")
                    self._con.execute(
                        "INSERT INTO watchdog_events"
                        " (fact_code, key, kind, source_ts, event_ts, detail)"
                        " VALUES (?,?,?,?,?,?)",
                        (code, key, kind, float(ev.get("source_ts") or now), now, detail))
                    eid = int(self._con.execute("SELECT last_insert_rowid()").fetchone()[0])
                    self._con.execute(
                        "INSERT INTO watchdog_state (fact_code, key, state, since_ts, detail, event_id)"
                        " VALUES (?,?,?,?,?,?) ON CONFLICT(fact_code, key) DO UPDATE SET"
                        " state=excluded.state, since_ts=excluded.since_ts,"
                        " detail=excluded.detail, event_id=excluded.event_id",
                        (code, key, cur_state, now if bad else 0, detail,
                         f"WD-EV-{eid}" if bad else ""))
                    created.append({"event_id": f"WD-EV-{eid}", "fact_code": code,
                                    "key": key, "kind": kind,
                                    "source_ts": float(ev.get("source_ts") or now),
                                    "event_ts": now, "detail": detail})
                self._con.commit()
            except BaseException:
                try:
                    self._con.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                self._in_tx = False
        return created

    def watchdog_events_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """巡检事件近况（GET /watchdog 数据源）：新事件在前，含稳定事件 ID。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM watchdog_events ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["event_id"] = f"WD-EV-{d.pop('id')}"
            out.append(d)
        return out

    def watchdog_state_all(self) -> List[Dict[str, Any]]:
        """巡检当前状态全集（管理端/测试用）。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM watchdog_state ORDER BY fact_code, key").fetchall()
        return [dict(r) for r in rows]

    # 允许经 PATCH /tickets/{id} 改判的字段（研讨阶段人工定级/改派/改期限）
    _PATCHABLE_TICKET_FIELDS = {
        "severity": lambda v: str(v),
        "responsible_dept": lambda v: str(v),
        "assignee": lambda v: str(v),
        "priority": lambda v: str(v),
        "due_ts": lambda v: float(v),
    }

    # SM-20 §4.2 不可变快照：任何路径都不得经本方法触碰授权快照列
    # （白名单本就不含，显式拒绝防将来误加； mandate_status 是只读派生亦不可写）。
    _IMMUTABLE_TICKET_FIELDS = frozenset({"mandate_json", "policy_version", "mandate_status"})

    def update_ticket_fields(self, ticket_id: int, fields: Dict[str, Any],
                             actor: str = "admin", note: str = "") -> Dict[str, Any]:
        """白名单字段修改 + patch 时间线留痕（改动前后值都落 detail）。
        返回 {"ok", kind?, ticket?}；not_found / no_field 与流转端点错误口径一致。
        SM-20 §4.2：授权快照列（mandate_json/policy_version/mandate_status）不可变，
        显式请求直接拒绝（immutable），改判/改派任何路径都改写不了快照。"""
        with self._lock:
            for key in fields:
                if str(key) in self._IMMUTABLE_TICKET_FIELDS:
                    return {"ok": False, "kind": "immutable",
                            "error": f"字段 {key} 不可修改（授权快照不可变，补授权走 /tickets/{ticket_id}/reauth）"}
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
            self._commit()
            new_row = self._ticket_row_locked(ticket_id)
            return {"ok": True, "ticket": self._ticket_dict(new_row)}

    # ── SM-42 整改验收摘要导出 manifest（版本化产物台账）──────────────────
    def next_acceptance_version(self, ticket_id: int) -> int:
        """该工单下一版摘要的版本号 = 已归档版本数 + 1（唯一索引兜底并发）。"""
        with self._lock:
            row = self._con.execute(
                "SELECT COUNT(*) FROM acceptance_summary_exports WHERE ticket_id=?",
                (ticket_id,)).fetchone()
        return int(row[0] or 0) + 1

    def record_acceptance_export(self, *, ticket_id: int, version: int,
                                 template_id: str, data_hash: str,
                                 file_path: str, file_hash: str,
                                 snapshot_path: str, snapshot_hash: str,
                                 approved_by: str = "",
                                 actor: str = "admin") -> Dict[str, Any]:
        """归档一版验收摘要：manifest 行 + ticket_timeline acceptance_export 事件在
        **同一次持锁、同一次 commit** 内落库（导出也是授权链上的一环，时间线可追溯）。
        (ticket_id, version) 唯一索引兜底并发：撞索引 = 版本已被并发归档 → 结构化
        version_conflict，调用方负责清理已写产物（零残留）；工单不存在 → not_found。"""
        with self._lock:
            row = self._ticket_row_locked(ticket_id)
            if row is None:
                return {"ok": False, "code": "not_found",
                        "error": f"工单不存在: {ticket_id}"}
            try:
                cur = self._con.execute(
                    "INSERT INTO acceptance_summary_exports (ticket_id, version,"
                    " template_id, data_hash, file_path, file_hash, snapshot_path,"
                    " snapshot_hash, approved_by, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ticket_id, int(version), str(template_id), str(data_hash),
                     str(file_path), str(file_hash), str(snapshot_path),
                     str(snapshot_hash), str(approved_by or ""), time.time()))
                export_id = int(cur.lastrowid or 0)
                self._con.execute(
                    "INSERT INTO ticket_timeline (ticket_id, ts, action, from_stage,"
                    " to_stage, actor, note, detail) VALUES (?,?,?,?,?,?,?,?)",
                    (ticket_id, time.time(), "acceptance_export",
                     self._norm_stage(row["stage"], row["status"]), "",
                     actor or "admin",
                     f"整改验收摘要 v{int(version)} 已归档",
                     json.dumps({"export_id": export_id, "version": int(version),
                                 "template_id": str(template_id),
                                 "data_hash": str(data_hash),
                                 "file_hash": str(file_hash),
                                 "approved_by": str(approved_by or "")},
                                ensure_ascii=False)))
                self._commit()
            except sqlite3.IntegrityError:
                self._con.rollback()
                return {"ok": False, "code": "version_conflict",
                        "error": f"工单 {ticket_id} 的摘要版本 v{int(version)} "
                                 f"已被归档（并发冲突），请重新预览后导出"}
        return {"ok": True, "export_id": export_id}

    def get_acceptance_export(self, export_id: int) -> Optional[Dict[str, Any]]:
        """按产物 id 取 manifest 行（下载授权与完整性核对的数据源）。"""
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM acceptance_summary_exports WHERE id=?",
                (int(export_id),)).fetchone()
        return dict(row) if row else None

    def list_acceptance_exports(self, ticket_id: int) -> List[Dict[str, Any]]:
        """某工单全部已归档摘要版本（版本升序）。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM acceptance_summary_exports WHERE ticket_id=?"
                " ORDER BY version ASC", (ticket_id,)).fetchall()
        return [dict(r) for r in rows]

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
