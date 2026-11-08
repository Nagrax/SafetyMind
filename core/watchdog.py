"""只读巡检 watchdog（SM-40）：读取既有扫描/调度入口的等待、逾期、卡单、审批与
版本漂移，输出稳定事实码 + 事件 ID + 来源时间 + 责任范围 + 处理建议。

红线（与 tests/test_watchdog.py 共同钉死）：
  - 本模块对**业务表绝对只读**：只调用 AuditStore 的既有只读方法
    （notification_stats / notification_health / list_overdue_tickets /
    list_stuck_tickets）与巡检自有两表（watchdog_state / watchdog_events，经
    AuditStore.watchdog_observe 单事务写入——业务表零接触，八表快照回归守卫）；
  - 绝不发网络请求、绝不调 LLM、绝不触碰工单流转 / SOP 发布 / 通知重放等生产写入口；
  - 自然语言问诊后接时只解释 GET /watchdog 已鉴权字段，不新增数据面。

去重语义（任务卡验收 1）：迁移判定唯一依据是库内 watchdog_state（收敛进
AuditStore.watchdog_observe 的单个 BEGIN IMMEDIATE，锁内判态）——进程内不持任何
去重状态，因此重启与两个调度者并行都恰一次新告警；状态未变静默；恢复恰一次
recovered 事件。

失效面（任务卡验收 2）：单族读取失败 → 该族事实按 unknown 上报（带来源时间与错误串，
可追溯），**不**进状态迁移（绝不因读不到而误报恢复或误报告警）；整库不可用或迁移
失败 → status="unavailable"、零写入（fail-closed）；存储恢复后下一轮续扫，既有告警
状态原样保持。

负载（任务卡验收 3 / G5）：self_stats 记录每次扫描耗时；扫描为有限条 SELECT，
无网络无模型，实测产物见 scratch/sm-loop/handoff/SM-40/run-wf/load-measure.json。
"""
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 事实码 → (责任范围, 处理建议)。稳定映射：测试冻结，改动须同步 SM-40 契约。
FACT_OWNERS: Dict[str, tuple] = {
    "WD-NOTIFY-WAITING": (
        "值班渠道管理员",
        "配置值班渠道（ESCALATION_WEBHOOK_URL）后 outbox 循环自动 adopt 恢复；未配置不耗重试不计失败"),
    "WD-NOTIFY-FAILED": (
        "值班渠道管理员",
        "投递失败重试中：按 last_error 与退避计划等待自动重试；渠道恢复后送达即清零健康桶"),
    "WD-NOTIFY-EXHAUSTED": (
        "值班渠道管理员",
        "投递已耗尽（需人工介入）：核对渠道后经管理端重放（POST /notifications/{id}/replay）"),
    "WD-NOTIFY-CHANNEL": (
        "值班渠道管理员",
        "渠道连败达阈：检查渠道可用性；任一次送达即自动解除告警态（恢复事件由巡检记录）"),
    "WD-TICKET-OVERDUE": (
        "工单责任部门/督办",
        "整改期限已过：督办闭环，或经管理端改判期限（时间线留痕，逾期告警每单仅一次）"),
    "WD-TICKET-STUCK": (
        "工单处理人",
        "阶段长时间无进展：推进五阶段流转或在管理端批注说明卡点"),
    "WD-SKILL-CANDIDATES": (
        "SOP 审批人（admin/publisher）",
        "存在待审批候选：审批或驳回（POST /skills/candidates/{id}/approve|reject），积压即治理欠账"),
    "WD-SOP-DRIFT": (
        "SOP 管理员",
        "发布指针与生效视图不一致：重启或重新应用发布指针后核对（drift 期间回复按生效视图渲染）"),
    "WD-SOP-UNAVAILABLE": (
        "平台运维",
        "SOP 发布治理层不可用：检查数据目录权限与磁盘；加载与对话不受影响，发布端点不可用"),
}


def _owner(code: str) -> tuple:
    return FACT_OWNERS.get(code, ("平台运维", "核对巡检契约（core/watchdog.py FACT_OWNERS）"))


class Watchdog:
    """只读巡检器：scan() 采集→迁移→报告；report() 只读最近一次扫描视图。

    构造参数（全部可选注入，测试/端点复用同一实例）：
      audit            AuditStore 实例（唯一存储面；None 视为存储不可用）
      skill_evolution  SkillEvolution（审批事实；None = 不产出该事实，不报不可用）
      skill_ledger / skill_manager  发布指针与生效视图（二者齐备才产 WD-SOP-DRIFT，
                       任一缺失产 WD-SOP-UNAVAILABLE——配置缺失不静默绿）
      stuck_s          卡单阈值秒（env SAFETYMIND_WATCHDOG_STUCK_S，默认 86400）
      interval_s       仅用于 /watchdog 展示（env SAFETYMIND_WATCHDOG_INTERVAL_S，默认 60）
    """

    def __init__(self, audit, *, skill_evolution=None, skill_ledger=None,
                 skill_manager=None, stuck_s: Optional[float] = None,
                 interval_s: Optional[float] = None):
        self._audit = audit
        self._evo = skill_evolution
        self._ledger = skill_ledger
        self._mgr = skill_manager
        self._stuck_s = float(
            stuck_s if stuck_s is not None
            else os.getenv("SAFETYMIND_WATCHDOG_STUCK_S", "86400"))
        self._interval_s = float(
            interval_s if interval_s is not None
            else os.getenv("SAFETYMIND_WATCHDOG_INTERVAL_S", "60"))
        self._last_report: Optional[Dict[str, Any]] = None
        self._scan_count = 0
        self._last_scan_ts = 0.0
        self._last_scan_ms = 0.0
        self._last_errors: List[Dict[str, Any]] = []

    # ── 事实采集（每族独立 try：一族失败不拖垮其余，fail-per-fact）──────────────
    def _collect(self) -> tuple:
        observations: List[Dict[str, Any]] = []
        unknown: List[Dict[str, Any]] = []
        failed_families = 0
        total_families = 0

        def family(codes: List[str], fn):
            """跑一族采集：成功产出观察项；失败按 codes 逐个落 unknown（不进迁移）。"""
            nonlocal failed_families, total_families
            total_families += 1
            ts = time.time()
            try:
                observations.extend(fn(ts))
            except Exception as ex:
                failed_families += 1
                err = f"{type(ex).__name__}: {ex}"
                for c in codes:
                    scope, sug = _owner(c)
                    unknown.append({"fact_code": c, "key": "", "error": err,
                                    "source_ts": ts, "owner_scope": scope,
                                    "suggestion": sug})

        def fam_stats(ts):
            st = self._audit.notification_stats() or {}
            out = []
            # 注意：stats 键为 failed_exhausted（SM-11 §2.8.2 键集合只增不减），
            # 事实 key 用短名 exhausted（决策 R2 冻结）。
            for code, k, sk in (("WD-NOTIFY-WAITING", "waiting", "waiting"),
                                ("WD-NOTIFY-FAILED", "failed", "failed"),
                                ("WD-NOTIFY-EXHAUSTED", "exhausted", "failed_exhausted")):
                n = int(st.get(sk, 0) or 0)
                out.append({"fact_code": code, "key": k, "bad": n > 0, "count": n,
                            "detail": f"{sk}={n}", "source_ts": ts})
            return out

        def fam_health(ts):
            rows = [h for h in (self._audit.notification_health() or [])
                    if h.get("breached_ts")]
            return [{"fact_code": "WD-NOTIFY-CHANNEL",
                     "key": f"{h.get('kind', 'notify')}:{h.get('channel', '')}",
                     "bad": True, "count": int(h.get("consecutive_failures") or 0),
                     "detail": f"渠道连败 {h.get('consecutive_failures')} 次达阈"
                               f"（breached_ts={h.get('breached_ts')}）",
                     "source_ts": ts} for h in rows]

        def fam_overdue(ts):
            now = time.time()
            out = []
            for t in (self._audit.list_overdue_tickets() or []):
                hrs = max(0.0, (now - float(t.get("due_ts") or now)) / 3600.0)
                out.append({"fact_code": "WD-TICKET-OVERDUE",
                            "key": f"ticket:{t.get('id')}", "bad": True, "count": 1,
                            "detail": f"工单#{t.get('id')} {t.get('title', '')} 逾期 "
                                      f"{hrs:.1f}h（due_ts={t.get('due_ts')}）",
                            "source_ts": ts})
            return out

        def fam_stuck(ts):
            out = []
            for t in (self._audit.list_stuck_tickets(self._stuck_s) or []):
                out.append({"fact_code": "WD-TICKET-STUCK",
                            "key": f"ticket:{t.get('id')}", "bad": True, "count": 1,
                            "detail": f"工单#{t.get('id')} 停留在 "
                                      f"{t.get('stage') or '(未知阶段)'} 超过 "
                                      f"{self._stuck_s:.0f}s",
                            "source_ts": ts})
            return out

        def fam_candidates(ts):
            if self._evo is None:          # 组件未接入：不产出该事实（不误报不可用）
                return []
            rows = self._evo.list_candidates(status="pending") or []
            oldest = min((str(r.get("id") or "") for r in rows), default="")
            return [{"fact_code": "WD-SKILL-CANDIDATES", "key": "pending",
                     "bad": len(rows) > 0, "count": len(rows),
                     "detail": f"待审批候选 {len(rows)} 条（最早 id={oldest or '无'}）",
                     "source_ts": ts}]

        def fam_drift(ts):
            if self._ledger is None or self._mgr is None:
                return [{"fact_code": "WD-SOP-UNAVAILABLE", "key": "ledger",
                         "bad": True, "count": 1,
                         "detail": "SOP 发布治理层不可用（ledger/manager 未初始化，"
                                   "配置缺失或初始化失败）",
                         "source_ts": ts}]
            rid_p = str((self._ledger.pointer() or {}).get("revision_id") or "")
            rid_e = str(getattr(self._mgr, "_published_revision", "") or "")
            bad = rid_p != rid_e
            return [{"fact_code": "WD-SOP-DRIFT", "key": "pointer", "bad": bad,
                     "count": 1 if bad else 0,
                     "detail": f"发布指针={rid_p or '(空)'} 生效视图={rid_e or '(空)'}"
                               + ("（指针已动而生效视图未同步）" if bad else "（一致）"),
                     "source_ts": ts}]

        family(["WD-NOTIFY-WAITING", "WD-NOTIFY-FAILED", "WD-NOTIFY-EXHAUSTED"], fam_stats)
        family(["WD-NOTIFY-CHANNEL"], fam_health)
        family(["WD-TICKET-OVERDUE"], fam_overdue)
        family(["WD-TICKET-STUCK"], fam_stuck)
        family(["WD-SKILL-CANDIDATES"], fam_candidates)
        family(["WD-SOP-DRIFT", "WD-SOP-UNAVAILABLE"], fam_drift)
        return observations, unknown, failed_families, total_families

    # ── 一次扫描 ─────────────────────────────────────────────────────────────
    def scan(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        now = time.time()
        status = "ok"
        events: List[Dict[str, Any]] = []
        facts: List[Dict[str, Any]] = []

        if self._audit is None:
            status = "unavailable"
            observations, unknown, failed, total = [], [], 1, 1
            self._remember_error(now, "audit 存储未注入（巡检不可用）")
        else:
            observations, unknown, failed, total = self._collect()
            if failed >= total:                       # 所有族都读不到 = 存储整体不可用
                status = "unavailable"
            if observations and status == "ok":
                try:
                    events = self._audit.watchdog_observe(observations)
                except Exception as ex:               # 迁移失败：fail-closed，零状态翻转
                    status = "unavailable"
                    err = f"{type(ex).__name__}: {ex}"
                    self._remember_error(now, f"watchdog_observe 失败: {err}")
                    scope, sug = _owner("WD-STORE-UNAVAILABLE")
                    unknown.append({"fact_code": "WD-STORE-UNAVAILABLE", "key": "db",
                                    "error": err, "source_ts": now,
                                    "owner_scope": scope, "suggestion": sug})

        state_map: Dict[tuple, Dict[str, Any]] = {}
        if status == "ok":
            try:
                state_map = {(r["fact_code"], r["key"]): r
                             for r in (self._audit.watchdog_state_all() or [])}
            except Exception as ex:
                status = "unavailable"
                self._remember_error(now, f"watchdog_state 读取失败: "
                                         f"{type(ex).__name__}: {ex}")

        for o in observations:
            code, key = o["fact_code"], o["key"]
            row = state_map.get((code, key)) or {}
            scope, sug = _owner(code)
            facts.append({"fact_code": code, "key": key,
                          "state": "alert" if o.get("bad") else "ok",
                          "count": int(o.get("count") or 0),
                          "detail": str(o.get("detail") or ""),
                          "owner_scope": scope, "suggestion": sug,
                          "event_id": str(row.get("event_id") or ""),
                          "since_ts": float(row.get("since_ts") or 0.0)})
        for u in unknown:                              # unknown 也带责任范围/建议（可追溯）
            u.setdefault("owner_scope", _owner(u.get("fact_code", ""))[0])
            u.setdefault("suggestion", _owner(u.get("fact_code", ""))[1])

        self._scan_count += 1
        self._last_scan_ts = now
        self._last_scan_ms = (time.perf_counter() - t0) * 1000.0
        report = {"status": status, "generated_ts": now, "facts": facts,
                  "events": events, "unknown": unknown,
                  "self_stats": self.self_stats()}
        self._last_report = report
        return report

    # ── 只读视图（GET /watchdog 数据源；不采集，零写入面）────────────────────
    def report(self) -> Dict[str, Any]:
        base = self._last_report or {"status": "unknown", "generated_ts": 0.0,
                                     "facts": [], "events": [], "unknown": [],
                                     "note": "尚未完成首次扫描"}
        out = dict(base)
        out["interval_s"] = self._interval_s
        try:
            out["events_recent"] = self._audit.watchdog_events_recent(50)
        except Exception as ex:
            out["events_recent"] = []
            self._remember_error(time.time(), f"watchdog_events_recent 读取失败: "
                                             f"{type(ex).__name__}: {ex}")
        out["self_stats"] = self.self_stats()
        return out

    def self_stats(self) -> Dict[str, Any]:
        """巡检自身负载（G5 测量缝）：扫描次数/最近耗时/最近错误环。"""
        return {"scan_count": self._scan_count,
                "last_scan_ts": self._last_scan_ts,
                "last_scan_duration_ms": round(self._last_scan_ms, 3),
                "last_errors": list(self._last_errors[-10:])}

    def _remember_error(self, ts: float, err: str) -> None:
        self._last_errors.append({"source_ts": ts, "error": str(err)[:300]})
        logger.warning(f"只读巡检异常（可追溯，不误报）: {err}")
