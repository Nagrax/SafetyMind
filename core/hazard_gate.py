"""隐患工单触发闸门（普通对话 → 隐患闭环的入口，纯规则、零 LLM、毫秒级）。

分层触发（产品决策 2026-10-13，对齐双重预防机制"排查-上报-整改-验收-销号"闭环）：
  - CRITICAL / 转人工 → 走既有应急升级通道（emergency 工单），本闸门返回 none 不重复建单；
  - 隐患类意图（hazard_report / equipment_alarm / incident_report）× urgency ≥ HIGH
    → action=auto 自动建单；
  - 同批意图 × MEDIUM，或 hazard_inspection（多为咨询，永不自动）× ≥ MEDIUM
    → action=suggest 建议建单（用户端确认卡，点击 /hazard/confirm 才建单）；
  - 其余（纯咨询/寒暄/反馈）→ none。防止"工单贬值"：无差别建单会让管理人员不再看工单。

定级（一般/重大）：对照《隐患排查依据大全》台账聚合配置——
  - 消息+实体 命中某大类关键词（按高危优先顺序取首个命中）；
  - 命中类属"重大倾向"（易燃易爆危险作业/易燃易爆设施及场所/热工设备设施及场所，
    台账 125/132 条重大集中于此）且伴随高危信号（紧急度 ≥ HIGH 或命中危化品实体）
    → 建议重大；否则一般；
  - basis 携带台账依据条款引用，简报可追溯。
  管理员可在研讨阶段经 PATCH /tickets/{id} 人工改判。

派发：hazard_dispatch 配置（data/ 运行时副本优先，种子兜底）按大类/关键词
定责任部门与限期（重大/一般分开设限期，默认 48h/168h）。

设计约束：纯函数 + 只读配置；任何异常由调用方 try/except 降级，绝不阻塞对话主链路。
"""
import json
import logging
import os
import pathlib
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ROOT = pathlib.Path(__file__).parent.parent.resolve()

# 自动建单意图（发现型：设备报警/隐患上报/事故上报——消息本身就在报告现场问题）
AUTO_INTENTS = {"hazard_report", "equipment_alarm", "incident_report"}
# 建议建单意图 = 自动集（降档到 MEDIUM 时）+ 隐患排查（多为"帮我列清单"式咨询，只建议）
SUGGEST_INTENTS = AUTO_INTENTS | {"hazard_inspection"}

# 紧急度阈值（与 core.intent_recognizer.UrgencyLevel 数值对齐：LOW=1..CRITICAL=4）
_URGENCY_MEDIUM = 2
_URGENCY_HIGH = 3
_URGENCY_CRITICAL = 4

# 兜底配置（data/ 与种子均缺失时仍可工作：全一般、派安环部）
_FALLBACK_DISPATCH = {
    "default": {"dept": "安环部", "deadline_hours_major": 48, "deadline_hours_general": 168},
    "rules": [],
}
# 兜底台账：无类别关键词时按"一般隐患"登记，不阻断建单
_FALLBACK_TAXONOMY: Dict[str, Any] = {"categories": []}

_lock = threading.Lock()
_taxonomy: Optional[Dict[str, Any]] = None
_dispatch: Optional[Dict[str, Any]] = None


def _read_json(paths: List[pathlib.Path]) -> Optional[Dict[str, Any]]:
    for p in paths:
        try:
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as ex:  # 配置损坏按缺失处理，走种子/兜底
            logger.warning(f"配置读取失败({p.name}): {ex}")
    return None


def _configs() -> tuple:
    """惰性加载 + 缓存（配置为只读数据，进程内不变；SAFETYMIND_HAZARD_DISPATCH
    可指定运行时派发副本路径）。线程安全：闸门在对话链路中被并发调用。"""
    global _taxonomy, _dispatch
    with _lock:
        if _taxonomy is None:
            _taxonomy = _read_json([
                _ROOT / "data" / "resource_library" / "hazard_taxonomy.json",
                _ROOT / "mcp" / "hazard_taxonomy_seed.json",
            ]) or _FALLBACK_TAXONOMY
        if _dispatch is None:
            override = os.getenv("SAFETYMIND_HAZARD_DISPATCH", "").strip()
            paths = ([pathlib.Path(override)] if override else []) + [
                _ROOT / "data" / "resource_library" / "hazard_dispatch.json",
                _ROOT / "mcp" / "hazard_dispatch_seed.json",
            ]
            _dispatch = _read_json(paths) or _FALLBACK_DISPATCH
        return _taxonomy, _dispatch


def reload_config() -> None:
    """重新加载配置（测试与热更新用；与 /skills/reload 同哲学）。"""
    global _taxonomy, _dispatch
    with _lock:
        _taxonomy = None
        _dispatch = None


def _match_category(text: str, taxonomy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按配置顺序（高危类在前）取首个命中关键词的大类。"""
    for cat in taxonomy.get("categories", []):
        for kw in cat.get("keywords", []):
            if kw and kw in text:
                return cat
    return None


def _resolve_dispatch(category: Optional[str], text: str,
                      dispatch: Dict[str, Any]) -> Dict[str, Any]:
    """派发规则：按 order 依次匹配（match=大类名 / match_keywords=消息关键词），
    未命中用 default。返回 dept/deadline_hours_major/deadline_hours_general。"""
    default = dict(dispatch.get("default") or _FALLBACK_DISPATCH["default"])
    rules = sorted(dispatch.get("rules", []), key=lambda r: r.get("order", 99))
    for rule in rules:
        if rule.get("match") and rule["match"] == category:
            return {**default, **{k: rule[k] for k in
                                  ("dept", "deadline_hours_major", "deadline_hours_general")
                                  if k in rule}}
        kws = rule.get("match_keywords") or []
        if any(kw in text for kw in kws if kw):
            return {**default, **{k: rule[k] for k in
                                  ("dept", "deadline_hours_major", "deadline_hours_general")
                                  if k in rule}}
    return default


def _norm_urgency(urgency: Any) -> int:
    """接受 UrgencyLevel 枚举 / int / str（"critical" 等），统一成 1-4。"""
    raw = getattr(urgency, "value", urgency)
    if isinstance(raw, str):
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(raw.strip().lower(), 1)
    try:
        return max(1, min(4, int(raw)))
    except (TypeError, ValueError):
        return 1


def decide(intent: Any, urgency: Any, entities: Dict[str, List[str]],
           message: str, escalated: bool = False,
           enabled: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    """隐患闸门裁决。返回 None 表示不建单；否则：
      action: "auto" | "suggest"
      severity: "major" | "general"      建议定级（研讨阶段可人工改判）
      category / basis: 台账大类与依据引用（留痕用）
      dept / deadline_hours: 派发对象与限期
      summary: 工单标题/确认卡摘要
    纯规则零 LLM；intent 接受 IntentCategory 枚举或其字符串值。"""
    if enabled is None:
        enabled = os.getenv("SAFETYMIND_HAZARD_AUTOTICKET", "1") == "1"
    if not enabled:
        return None
    if escalated:  # 应急升级已建 emergency 工单并走值班通道，不重复建单
        return None
    intent_val = str(getattr(intent, "value", intent) or "other")
    urg = _norm_urgency(urgency)

    if intent_val in AUTO_INTENTS:
        action = "auto" if urg >= _URGENCY_HIGH else (
            "suggest" if urg == _URGENCY_MEDIUM else None)
    elif intent_val in SUGGEST_INTENTS:  # 只剩 hazard_inspection：咨询属性，永不自动
        action = "suggest" if urg >= _URGENCY_MEDIUM else None
    else:
        return None
    if action is None:
        return None

    taxonomy, dispatch = _configs()
    entities = entities or {}
    chem = " ".join(entities.get("chemical") or [])
    equip = " ".join(entities.get("equipment_id") or [])
    text = " ".join([str(message or ""), chem, equip])

    cat = _match_category(text, taxonomy)
    cat_name = cat["name"] if cat else ""
    chemicals = entities.get("chemical") or []
    major = bool(cat and cat.get("major_lean")
                 and (urg >= _URGENCY_HIGH or chemicals))
    severity = "major" if major else "general"

    disp = _resolve_dispatch(cat_name if cat else None, text, dispatch)
    deadline = disp["deadline_hours_major"] if major else disp["deadline_hours_general"]

    digest = " ".join(str(message or "").split())[:60] or "（无摘要）"
    if cat:
        summary = f"{cat_name}隐患：{digest}"
        basis = cat.get("basis_sample") or ""
    else:
        summary = f"现场隐患：{digest}"
        basis = "未匹配台账大类，按一般隐患登记"

    return {
        "action": action,
        "severity": severity,
        "category": cat_name,
        "basis": basis,
        "dept": disp.get("dept", "安环部"),
        "deadline_hours": deadline,
        "summary": summary[:100],
    }
