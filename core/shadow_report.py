"""影子评审指标聚合（SM-22，2026-11-08）：双轨分歧 / 确认率 / 确认时长 / 去重覆盖 /
转人工覆盖 / 待复核清单——全部只读，分母显式，样本（request_id）可核对。

## 为什么

SM-20 落了 hazard_observations 影子裁决数据，SM-21 落了工单阶段权限矩阵，但
"双轨对照，复核 would_accept、实际工单和实际业务需求"（任务卡 SM-22 scope）没有
任何可核对的输出面：影子行躺在表里，误单候选/漏单候选/确认率/确认时长无处可查。
本模块是纯聚合（零 LLM、零写库、零网络），供 GET /admission/shadow-report 消费。

## 口径（契约源：scratch/sm-loop/handoff/SM-22/run-wf/decision.md §2.3）

  - 双轨分歧（hazard_observations，分母=影子行总数）：
      shadow_only   would_accept=1 且现行未建单   → 新规则要建、现行没建（误单候选）；
      current_only  would_accept=0 且现行建了单   → 现行建了、新规则不建（漏单候选，
                                                      含 reason=model_hint_random 差异）；
      agree_build   两边都建；agree_none          → 两边都没建。
  - 确认率 = (confirmed+merged) / 全部建议行；确认时长 = 已确认建议的
    ticket.created_ts − suggestion.created_ts（p50/p95/最大，秒）。
  - 去重覆盖 = hazard 工单 repeat_report 时间线事件数（合并路径留痕，SM-10）。
  - 转人工覆盖 = 有 source_request_id 的应急单 ∩ escalated 请求 / escalated 请求数。
  - 待复核清单 = status=pending 的建议卡（observe 收口后的人工队列可见面）。

逐节独立 try/except 降级：单节失败只在该节标 error，绝不拖垮整份报告、绝不抛出
（调用方是管理端点，报告失败只告警）。字段语义与异常都在返回值里，不做假数据。
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 影子对照列里"现行实际建了单"的动作值（auto 建单 / 合并进既有单）
_BUILT_ACTIONS = ("auto_ticket", "merged")
# 报告里携带的样本 id 上限（防列表失控；计数不受影响）
_SAMPLE_CAP = 20
_PENDING_CAP = 50
_TICKET_SCAN_CAP = 200


def _section(fn):
    """逐节降级包装：失败返回 {"error": "..."}，不抛出。"""
    try:
        return fn()
    except Exception as ex:  # 报告聚合绝不阻塞管理端点
        logger.warning(f"shadow_report 分节聚合失败（{fn.__name__}）: {ex}")
        return {"error": f"{type(ex).__name__}: {ex}"}


def _pctl(sorted_vals: List[float], q: float) -> Optional[float]:
    """最近秩百分位（秒，1 位小数）；空列表返回 None。"""
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return round(sorted_vals[idx], 1)


def shadow_metrics(store, *, now: Optional[float] = None) -> Dict[str, Any]:
    """聚合影子评审指标（只读）。store=core.audit_store.AuditStore 实例。
    全部分母进 denominators；比例不带阈值判断（阈值判读属评测产物与业务复核）。
    真实数据在 observe 未启用期间为空集——如实返回零计数，不造样本。"""
    now = time.time() if now is None else float(now)
    out: Dict[str, Any] = {"generated_ts": now}

    def _dual_track() -> Dict[str, Any]:
        rows = store.list_hazard_observations(limit=2000) or []
        buckets = {"shadow_only": [], "current_only": [], "agree_build": [], "agree_none": []}
        for r in rows:
            wa = bool(r.get("would_accept"))
            built = str(r.get("current_action") or "") in _BUILT_ACTIONS
            key = ("agree_build" if (wa and built) else
                   "shadow_only" if wa else
                   "current_only" if built else "agree_none")
            buckets[key].append(str(r.get("request_id") or ""))
        return {"denominator": len(rows), **{
            k: {"count": len(v), "sample_request_ids": v[:_SAMPLE_CAP]}
            for k, v in buckets.items()}}

    def _confirm() -> Dict[str, Any]:
        rows = store.list_hazard_suggestions(limit=2000) or []
        by_status: Dict[str, int] = {}
        durations: List[float] = []
        ticket_ids: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            st = str(r.get("status") or "pending")
            by_status[st] = by_status.get(st, 0) + 1
            tid = r.get("ticket_id")
            if st in ("confirmed", "merged") and tid:
                if tid not in ticket_ids:
                    t = store.get_ticket(int(tid))
                    ticket_ids[int(tid)] = t or {}
                t = ticket_ids[int(tid)]
                if t.get("created_ts"):
                    durations.append(max(0.0, float(t["created_ts"]) - float(r["created_ts"])))
        durations.sort()
        settled = by_status.get("confirmed", 0) + by_status.get("merged", 0)
        return {
            "denominator": len(rows),
            "by_status": by_status,
            "rate": round(settled / len(rows), 4) if rows else None,
            "duration_s": {"n": len(durations), "p50": _pctl(durations, 0.50),
                           "p95": _pctl(durations, 0.95),
                           "max": round(durations[-1], 1) if durations else None},
        }

    def _dedup() -> Dict[str, Any]:
        tickets = [t for t in (store.list_tickets(limit=_TICKET_SCAN_CAP) or [])
                   if t.get("type") == "hazard"]
        repeat_events = 0
        for t in tickets:
            tl = store.get_ticket_timeline(int(t["id"]))
            repeat_events += sum(1 for e in (tl or []) if e.get("action") == "repeat_report")
        return {"hazard_tickets": len(tickets), "repeat_report_events": repeat_events,
                "note": "repeat_report 由合并路径留痕（SM-10）；重复上报不新建工单"}

    def _escalation() -> Dict[str, Any]:
        escalated = store.list_audit(limit=2000, escalated_only=True) or []
        esc_ids = {str(r.get("request_id") or "") for r in escalated} - {""}
        covered = set()
        for t in (store.list_tickets(limit=_TICKET_SCAN_CAP) or []):
            src = str(t.get("source_request_id") or "")
            if t.get("type") == "emergency" and src:
                covered.add(src)
        hit = len(esc_ids & covered)
        return {"escalated_requests": len(esc_ids), "covered": hit,
                "rate": round(hit / len(esc_ids), 4) if esc_ids else None,
                "note": "auto 档 model_hint 升级按设计不建应急单（reason=model_hint_random）"}

    def _pending() -> List[Dict[str, Any]]:
        rows = store.list_hazard_suggestions(status="pending", limit=_PENDING_CAP) or []
        out_rows = []
        for r in rows:
            payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
            decision = payload.get("decision") if isinstance(payload, dict) else {}
            out_rows.append({
                "request_id": str(r.get("request_id") or ""),
                "created_ts": r.get("created_ts"),
                "user_id": str(r.get("user_id") or ""),
                "summary": str((decision or {}).get("summary") or "")[:100],
            })
        return out_rows

    out["denominators"] = _section(lambda: {
        "observations": len(store.list_hazard_observations(limit=2000) or []),
        "suggestions": len(store.list_hazard_suggestions(limit=2000) or []),
        "tickets_hazard": sum(1 for t in (store.list_tickets(limit=_TICKET_SCAN_CAP) or [])
                              if t.get("type") == "hazard"),
        "escalated_requests": len(store.list_audit(limit=2000, escalated_only=True) or []),
    })
    out["dual_track"] = _section(_dual_track)
    out["confirm"] = _section(_confirm)
    out["dedup"] = _section(_dedup)
    out["escalation_coverage"] = _section(_escalation)
    out["pending_suggestions"] = _section(_pending)
    return out
