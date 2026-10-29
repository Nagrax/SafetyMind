"""紧急消息本地快速预检分流（零 LLM / 零 IO / 零记忆，毫秒级）。

设计约束（不得违反）：
  - 紧急度规则一律复用 core.intent_recognizer 的 CRITICAL 词表与组合规则
    （直接 import 同一对象，禁止另写一套弱化副本）；
  - 抑制只作用于"快速分流"，绝不移除或修改 orchestrator 既有的 CRITICAL 模板路径与
    _urgency 规则——被抑制的消息仍走正常链路，由 orchestrator 兜底模板化（漏检不弱化）；
  - 模块内不做任何网络/LLM/记忆访问，precheck 亚毫秒级返回。

状态区文案四态如实表达：已记录 / 已建单 / 通知（待发/已送达/已确认），
在通知渠道未配置或投递失败时绝不出现"已通知/已收到"字样。
"""
import re
from typing import Any, Dict, List, Optional

from core.intent_recognizer import (
    IntentCategory,
    IntentRecognizer,
    UrgencyLevel,
    _URGENCY_COMBOS,
    _URGENCY_KEYWORDS,
)

# 复用意图识别器的规则方法（__new__ 绕过 __init__：纯规则方法不需要 API key / 模型）
_PATTERN_SHIM = IntentRecognizer.__new__(IntentRecognizer)

# 演练/培训/假设语境词：出现且无强现场证据时抑制快速分流（避免把演习当真实事故升级）
_DRILL_WORDS = ("演练", "演习", "模拟", "桌面推演", "预演", "培训", "课件", "考试",
                "案例", "假如", "假设", "如果发生", "制度要求", "规定")
# 强现场证据词：确认"当下正在发生"。"正在"若只是演练短语的一部分则不计（见 _has_strong_evidence）
_STRONG_WORDS = ("现场", "正在", "已经", "真的", "确实", "发生爆炸", "有人员", "有人被困")
_DRILL_PHRASE = re.compile(
    r"(?:正在|在做|进行|开展|组织).{0,10}?(?:演练|演习|预演|模拟|推演|培训|课件)")
# 否定词：关键词前 6 字内出现即视为否定/排除（"没有泄漏""不是火灾"）
_NEGATIONS = ("没有", "未", "无", "没", "不是", "并非", "排除", "消除")
# 讨论词本身（非指代当下事件）：命中即抑制（"泄漏这两个字怎么写"）
_META_WORDS = ("怎么写", "什么意思", "定义", "的含义", "英文", "翻译")
# 参数名（**物性参数**，不是现场事件）：2026-10-29 实测——"甲醇的闪点和爆炸极限分别是多少？"
# 因含"爆炸"被当成紧急事态，直接返回应急模板并建了应急工单（#83），用户拿不到闪点数值。
# 词表与判定集中在 core/param_phrases.py（紧急度判定与入口快路径共用一份，避免漂移）。
from core.param_phrases import keyword_only_in_param_phrase as _param_only  # noqa: E402

_NOTIFY_TEXT = {
    "unconfigured": "【通知】未配置通知渠道，待人工处理",
    "queued": "【通知】待发送",
    "sent": "【通知】已送达（值班 webhook）",
    "acked": "【通知】接收方已确认",
    "failed": "【通知】通知未送达，待人工处理",
    "unknown": "【通知】通知状态待补，未确认送达",
}

# 通用应急要点（与 EmployeeAgent.CRITICAL_TEMPLATES 的通用要点同源，逐条对应）
_EMERGENCY_BODY = (
    "1. 自身防护：佩戴防护用品，严禁贸然进入危险区域；\n"
    "2. 疏散隔离：组织无关人员向上风向撤离，设置警戒线；\n"
    "3. 控制源头：在确保自身安全的前提下切断泄漏源/电源/火源；\n"
    "4. 呼叫求援：立即拨打本单位应急电话及 119/120，说明地点、物质、伤亡情况；\n"
    "5. 现场引导：等候专业处置力量，提供您掌握的信息。")
_INCIDENT_BODY = (
    "1. 抢救伤员、保护现场，防止事故扩大；\n"
    "2. 立即拨打本单位应急电话及 120（如有人员伤亡），并按规定时限上报；\n"
    "3. 保留现场证据（照片/位置/时间/涉事设备与物质）；\n"
    "4. 配合调查，不瞒报、不谎报、不迟报。")


def _keyword_is_negated(msg: str, keyword: str) -> bool:
    """关键词前 6 字内出现否定词 → 视为否定（如"没有泄漏""不是火灾"）。"""
    idx = msg.find(keyword)
    if idx < 0:
        return False
    return any(n in msg[max(0, idx - 6):idx] for n in _NEGATIONS)


def _keyword_only_in_param_phrase(msg: str, keyword: str) -> bool:
    """兼容旧调用点：实现已集中到 core/param_phrases。"""
    return _param_only(msg, keyword)


def _has_strong_evidence(msg: str) -> bool:
    """是否存在"当下正在发生"的强现场证据。演练短语里的"正在"不算证据。"""
    if _DRILL_PHRASE.search(msg):
        return any(w in msg for w in _STRONG_WORDS if w != "正在")
    return any(w in msg for w in _STRONG_WORDS)


def _suppressed(msg: str, combo_hit: Optional[tuple], kw_hits: List[str]) -> bool:
    """误升级防护：讨论词本身、否定、演练/假设语境（无强现场证据时）一律抑制快速分流。"""
    if any(w in msg for w in _META_WORDS):
        return True
    has_drill = any(w in msg for w in _DRILL_WORDS)
    if combo_hit is not None:
        # 组合规则（泄漏+扩散等）是强现场迹象：只有纯演练/假设语境且无现场证据才抑制
        return has_drill and not _has_strong_evidence(msg)
    live = [kw for kw in kw_hits
            if not _keyword_is_negated(msg, kw)
            and not _keyword_only_in_param_phrase(msg, kw)]
    if not live:
        return True
    return has_drill and not _has_strong_evidence(msg)


def _kind(message: str) -> str:
    """用既有 _pattern_recognize 的意图结果区分：incident_report→incident，其余→emergency。"""
    try:
        intent = _PATTERN_SHIM._pattern_recognize(message).get("intent")
    except Exception:
        intent = IntentCategory.OTHER
    return "incident" if intent == IntentCategory.INCIDENT_REPORT else "emergency"




# 快路径补充伤情词（2026-10-28 并发轴实测）："车间氨气泄漏已经有人晕倒"是一个
# 教科书级严重紧急，却因词表只有"中毒/有人受伤/大量泄漏"而漏检——消息里写的是
# "晕倒"。快路径是零 LLM 的安全网，应当**宁可提前响应**，因此在这里补充显性
# 伤情词；共享的意图词表保持不动（避免影响意图基准准确率）。
_CRITICAL_EXTRA = (
    "晕倒", "昏迷", "不省人事", "失去意识", "倒地", "窒息", "吐血",
    "呼吸困难", "呼吸急促", "无法动弹", "大面积",
)

# 泄漏/喷料 + 人员伤情 = 强现场证据组合（与既有"泄漏+扩散"同性质）
_CASUALTY_COMBO = (
    ("泄漏", "刺漏", "渗漏", "喷料", "溢出"),
    ("晕倒", "昏迷", "中毒", "窒息", "受伤", "倒地", "不省人事", "失去意识"),
)


def precheck(message: str) -> Optional[Dict[str, Any]]:
    """纯本地紧急预检：命中既有 CRITICAL 词表或组合规则时返回分流信号，否则 None。

    返回 {"kind": "emergency"|"incident", "matched": [...], "reason": str}。
    命中不代表绕过正常链路：分流只在 /chat 入口提前返回最小响应，
    未命中的（或被抑制的）紧急消息仍由 orchestrator 的 CRITICAL 模板兜底。
    """
    msg = (message or "").strip()
    if not msg:
        return None

    combo_hit = None
    for words, signs in list(_URGENCY_COMBOS) + [_CASUALTY_COMBO]:
        w = [x for x in words if x in msg]
        s = [x for x in signs if x in msg]
        if w and s:
            combo_hit = (w[0], s[0])
            break
    kw_hits = [kw for kw in _URGENCY_KEYWORDS[UrgencyLevel.CRITICAL] if kw in msg]
    kw_hits.extend(kw for kw in _CRITICAL_EXTRA if kw in msg)
    if combo_hit is None and not kw_hits:
        return None
    if _suppressed(msg, combo_hit, kw_hits):
        return None

    matched: List[str] = []
    if combo_hit:
        matched.append(f"组合规则:{combo_hit[0]}+{combo_hit[1]}")
    matched.extend(kw_hits)
    reason = "紧急组合规则命中（" + "、".join(matched) + "）" if combo_hit \
        else "CRITICAL 紧急关键词命中（" + "、".join(matched) + "）"
    return {"kind": _kind(msg), "matched": matched, "reason": reason}


def entities(message: str) -> Dict[str, List[str]]:
    """复用意图识别器的规则实体抽取（本地正则，无 LLM），供快速分流落简报/返回体用。"""
    try:
        return _PATTERN_SHIM._extract_entities(message)
    except Exception:
        return {"equipment_id": [], "work_type": [], "area": [], "chemical": [], "date": []}


def status_lines(status: Dict[str, Any]) -> Dict[str, str]:
    """处置状态区文案（四态如实表达，禁止在未送达时出现"已通知/已收到"）。"""
    rid = str(status.get("request_id") or "")
    recorded = f"【已记录】上报编号 {rid}" if status.get("recorded") \
        else f"【已记录】记录待补（编号 {rid}）"
    ticket_id = status.get("ticket_id") or 0
    ticket = f"【已建单】应急工单 #{ticket_id}" if ticket_id else "【建单】暂未成功，已记录待补建"
    notify = _NOTIFY_TEXT.get(str(status.get("notify") or "unknown"), _NOTIFY_TEXT["unknown"])
    return {"recorded": recorded, "ticket": ticket, "notify": notify}


def minimal_response(kind: str, status: Dict[str, Any]) -> str:
    """经领域审核的最小响应：通用应急要点 + 如实的处置状态区（状态只反映 status 事实）。"""
    recorded = bool(status.get("recorded"))
    if kind == "incident":
        head = ("【事故上报·立即行动】您的上报已触发升级流程，请按顺序执行："
                if recorded else "【事故上报·立即行动】请按顺序执行：")
        body = _INCIDENT_BODY
    else:
        head = ("【紧急事态·立即行动】您的上报已触发应急响应，请按顺序执行："
                if recorded else "【紧急事态·立即行动】请按顺序执行：")
        body = _EMERGENCY_BODY
    lines = status_lines(status)
    return (f"{head}\n{body}\n\n【处置状态】\n"
            f"{lines['recorded']}\n{lines['ticket']}\n{lines['notify']}\n"
            f"重要提示：以上为通用应急要点，现场处置以本单位应急预案和应急指挥指令为准。")
