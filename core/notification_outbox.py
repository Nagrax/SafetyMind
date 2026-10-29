"""通知 outbox 投递逻辑（纯逻辑，无网络依赖，可单测）。

与 AuditStore 的分工：
  - AuditStore 负责可靠落库（工单变化与待发送记录同事务）、状态机、悬挂复位；
  - 本模块负责"复位悬挂 → 原子领取 → 逐行投递 → 标记送达/失败（指数退避）"的一次批量投递；
  - sender 由调用方注入（api 层注入 notify_escalation_blocking 并放到线程执行，
    测试注入假函数），本模块自身绝不发起网络请求、也绝不抛出。

状态语义见 core/audit_store.py 的 notification outbox 段：
pending 待发 / sending 投递中 / sent 渠道已受理 / ack 接收方或人工已确认 / failed 重试中或失败待处理。
"""
import json
import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


def backoff_seconds(attempts: int, base_s: float = 5.0, cap_s: float = 300.0) -> float:
    """指数退避：min(base_s * 2**(attempts-1), cap_s)；attempts>=1（按第几次尝试计）。"""
    n = max(1, int(attempts or 1))
    return float(min(base_s * (2 ** (n - 1)), cap_s))


def deliver_pending(audit, sender: Callable[[str, Dict[str, Any]], bool],
                    batch: int = 10, max_attempts: int = 5,
                    base_s: float = 5.0, cap_s: float = 300.0, stale_s: float = 120.0,
                    on_sent: Callable[[Dict[str, Any], Dict[str, Any]], None] = None
                    ) -> Dict[str, Any]:
    """一次批量投递：

    1) 先 audit.reap_stale_sending(stale_s) 复位悬挂 sending（进程被终止的残行）；
    2) audit.claim_pending_notifications(batch, max_attempts) 原子领取；
    3) 逐行调用 sender(url, payload)（同步可调用；允许抛异常，异常按失败处理）；
    4) 成功 → mark_notification_sent(id)（并调 on_sent 回调补痕）；失败 →
       mark_notification_failed(id, error, backoff_seconds(attempts))；
    5) 返回 {"claimed","sent","failed","reaped","details"}。

    payload JSON 损坏时按失败处理并记清 last_error，绝不静默丢弃。单行失败不影响其余行。
    """
    out: Dict[str, Any] = {"claimed": 0, "sent": 0, "failed": 0, "reaped": 0, "details": []}
    try:
        out["reaped"] = int(audit.reap_stale_sending(stale_s) or 0)
    except Exception as ex:                                   # 复位失败不影响投递
        logger.warning(f"outbox 悬挂发送复位失败: {ex}")
    try:
        rows = audit.claim_pending_notifications(batch, max_attempts) or []
    except Exception as ex:
        logger.warning(f"outbox 领取待发送记录失败: {ex}")
        return out

    out["claimed"] = len(rows)
    for row in rows:
        nid = row.get("id")
        err = ""
        try:
            payload = json.loads(row.get("payload") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("payload 不是 JSON 对象")
        except (ValueError, TypeError) as ex:
            payload, err = None, f"payload JSON 解析失败: {ex}"

        ok = False
        if payload is not None:
            try:
                ok = bool(sender(row.get("channel", ""), payload))
                if not ok:
                    err = "渠道未受理（sender 返回 False）"
            except Exception as ex:                           # 断网/超时/协议错误
                ok, err = False, f"{type(ex).__name__}: {ex}"

        if ok:
            try:
                audit.mark_notification_sent(nid)
            except Exception as ex:
                logger.warning(f"outbox 标记送达失败(id={nid}): {ex}")
            out["sent"] += 1
            out["details"].append({"id": nid, "key": row.get("idempotency_key"),
                                   "ok": True, "error": ""})
            if on_sent is not None:
                try:
                    on_sent(row, payload)
                except Exception as ex:
                    logger.warning(f"outbox 送达回调失败(id={nid}): {ex}")
        else:
            try:
                audit.mark_notification_failed(
                    nid, err or "投递失败",
                    backoff_seconds(row.get("attempts", 1), base_s, cap_s))
            except Exception as ex:
                logger.warning(f"outbox 标记失败状态失败(id={nid}): {ex}")
            out["failed"] += 1
            out["details"].append({"id": nid, "key": row.get("idempotency_key"),
                                   "ok": False, "error": err})
    return out
