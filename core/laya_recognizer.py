# Laya 判别式意图前置 + pattern 加权融合（离线消融网格的赢家配置）
# 证据: laya_eval/RESULTS.md — dev 63.7% vs Laya 单路 51.9% / pattern 单路 53.3% / 英文键 42.2%
# 设计约束:
#   - 不触碰升级硬规则链路（后果词表 → CRITICAL 直达升级接待，在 _urgency/_route 中，先于本模块结果生效）
#   - laya/torch 为可选依赖，懒加载；SAFETYMIND_LAYA=off（默认）时零侵入
#   - 级联: 融合置信度 >= tau_direct 直接返回（跳过 LLM 意图调用）；否则回退原三路融合
import os
import math
from typing import Callable, Dict, Optional, Tuple

# 中文键 → IntentCategory 值（选项键用中文：默认 192 token 预算下每个选项仅约 9 token 可见，
# 英文键+长描述在截断后锚定力不足；详见 laya_eval/RESULTS.md 截断实验）
ZH_KEY: Dict[str, str] = {
    "设备报警处置": "equipment_alarm", "检维修保养": "equipment_maintenance", "隐患上报": "hazard_report",
    "隐患排查": "hazard_inspection", "事故上报": "incident_report", "应急处置": "emergency_response",
    "危化品管理": "chemical_safety", "培训取证": "training_cert", "迎检考核": "inspection_audit",
    "职业健康": "occupational_health", "法规查询": "regulation_query", "防护用品": "ppe_inquiry",
    "制度文档": "safety_document", "一般咨询": "general_consult", "办票审批": "work_permit",
    "问候": "greeting", "正面反馈": "feedback", "转人工": "escalation", "无关": "other",
}
ZH_SHORT: Dict[str, str] = {
    "设备报警处置": "报警响 超温超压 联锁 DCS", "检维修保养": "检修 维修 保养 大修 计划",
    "隐患上报": "发现隐患 上报 无事故", "隐患排查": "排查 查什么 频次 清单",
    "事故上报": "已出事故 报告 时限 材料", "应急处置": "正在泄漏着火中毒 现场急救",
    "危化品管理": "危化品 储存 相容 MSDS", "培训取证": "培训学时 证 复审 考试",
    "迎检考核": "检查组 标准化考核 迎检准备", "职业健康": "体检 职业病 危害因素",
    "法规查询": "法规 国标 条文 第几条", "防护用品": "面具 手套 选型 检测周期",
    "制度文档": "规程 制度 预案 修订 受控", "一般咨询": "安全理念 概念 通俗解释",
    "办票审批": "动火 受限空间 作业票 审批", "问候": "你好 早上好 打招呼",
    "正面反馈": "谢谢 有用 点赞", "转人工": "人工 投诉 领导 专家", "无关": "天气 笑话 生活",
}
INSTRUCTIONS = "这条用户消息属于哪类安全生产意图？从下列选项中选出最符合用户意图的一项；若都不符合选 other。"
# 泛化问句会给 general_consult 0.5 的伪置信度（见 _best_pattern_match），融合时排除
GENERIC_NOISE = {"general_consult"}


def calibrate_probs(probs: Dict[str, float], temperature: float) -> Dict[str, float]:
    """温度校准：Laya 中文场景置信度系统性偏高（消融实测 avg_conf 0.69-0.84 的类命中率仅 0-50%），
    T>1 拉平分布后再与 pattern 融合。纯函数，可单测。"""
    if not probs:
        return {}
    if temperature <= 0:
        temperature = 1e-6
    eps = 1e-9
    logits = {k: math.log(max(float(v), eps)) / temperature for k, v in probs.items()}
    mx = max(logits.values())
    exps = {k: math.exp(v - mx) for k, v in logits.items()}
    z = sum(exps.values())
    return {k: v / z for k, v in exps.items()}


def fuse(laya_probs: Dict[str, float], pattern_cls: Optional[str], pattern_conf: float,
         weight: float = 0.5, exclude: Optional[set] = None) -> Tuple[Optional[str], float]:
    """加权融合：score = w * calib(laya_probs) + (1-w) * pattern_onehot * pattern_conf。
    返回 (intent, normalized_conf)。纯函数，可单测。"""
    exclude = exclude or set()
    if not laya_probs and not (pattern_cls and pattern_cls not in exclude):
        return None, 0.0
    scores: Dict[str, float] = {}
    total = 0.0
    for cls, p in (laya_probs or {}).items():
        scores[cls] = scores.get(cls, 0.0) + weight * float(p)
        total += weight * float(p)
    if pattern_cls and pattern_cls not in exclude:
        c = max(0.0, min(1.0, float(pattern_conf)))
        scores[pattern_cls] = scores.get(pattern_cls, 0.0) + (1.0 - weight) * c
        total += (1.0 - weight) * c
    if not scores or total <= 0:
        return None, 0.0
    best = max(scores, key=scores.get)
    return best, scores[best] / total


class LayaFuser:
    """懒加载 Laya(multilingual) 并与 pattern 结果加权融合。"""

    def __init__(self, temperature: float = 1.5, weight: float = 0.5,
                 tau_direct: float = 0.85, device: str = "cpu",
                 subfolder: str = "multilingual", repo: str = "convaiinnovations/laya"):
        self.temperature = temperature
        self.weight = weight
        self.tau_direct = tau_direct
        self.device = device
        self.subfolder = subfolder
        self.repo = repo
        self._agent = None

    @classmethod
    def from_env(cls) -> Optional["LayaFuser"]:
        """SAFETYMIND_LAYA!=1 时返回 None（零侵入默认）。"""
        if os.getenv("SAFETYMIND_LAYA", "off") != "1":
            return None
        return cls(
            temperature=float(os.getenv("SAFETYMIND_LAYA_TEMP", "1.5")),
            weight=float(os.getenv("SAFETYMIND_LAYA_W", "0.5")),
            tau_direct=float(os.getenv("SAFETYMIND_LAYA_TAU", "0.85")),
            device=os.getenv("SAFETYMIND_LAYA_DEVICE", "cpu"),
        )

    def _get_agent(self):
        if self._agent is None:
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            from laya import Agent  # 可选依赖，懒加载
            self._agent = Agent(self.repo, device=self.device, subfolder=self.subfolder)
            self._agent.predict("预热", self._question())  # 首条前向约 1.9s，启动时预热
        return self._agent

    def _question(self) -> Dict:
        return {"intent": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": dict(ZH_SHORT)}}

    def laya_probs(self, message: str) -> Dict[str, float]:
        """返回 {intent_value: prob}（中文键已映射回 intent value）。"""
        r = self._get_agent().predict(message, self._question())
        ans = r["answers"]["intent"]
        raw = ans.get("probabilities") or {}
        return {ZH_KEY.get(k, k): float(v) for k, v in raw.items()}

    def _try_recognize_sync(self, message: str, pattern_fn: Callable[[str], Dict]) -> Optional[Tuple[str, float]]:
        """同步实现：torch 前向是阻塞调用，经由 asyncio.to_thread 调用避免卡死事件循环。"""
        try:
            probs = self.laya_probs(message)
        except Exception:
            return None
        pat = pattern_fn(message)
        pat_cls = pat.get("intent")
        if hasattr(pat_cls, "value"):
            pat_cls = pat_cls.value
        cls, conf = fuse(probs, pat_cls, pat.get("confidence", 0.0),
                         weight=self.weight, exclude=GENERIC_NOISE)
        if cls is None:
            return None
        return cls, conf

    async def try_recognize(self, message: str, pattern_fn: Callable[[str], Dict]) -> Optional[Tuple[str, float]]:
        """异步入口：阻塞的模型前向放到线程池。返回 (intent_value, confidence)；模型不可用时 None。"""
        import asyncio
        return await asyncio.to_thread(self._try_recognize_sync, message, pattern_fn)
