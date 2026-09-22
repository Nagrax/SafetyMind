"""
亮点：端到端意图识别（安全生产域）

三路融合策略：
  1. LLM 语义理解（权重 70%）—— 主力，理解复杂语义和上下文
  2. Embedding 向量相似度（权重 20%）—— 快速匹配常见表达
     （官方 Anthropic SDK 无 embeddings 资源时退化为本地字符 n-gram 向量，
       属词面近似匹配；配置第三方 base_url 时该路整体禁用）
  3. 关键词模式匹配（权重 10%）—— 零延迟兜底

三路结果通过加权投票合并，置信度低于阈值时降级为 OTHER。
LLM 和 Embedding 并行调用，不串行等待。
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    # ── 设备与工艺 ─────────────────────────────────────────────
    EQUIPMENT_ALARM    = "equipment_alarm"       # 设备/工艺报警处置
    EQUIPMENT_MAINT    = "equipment_maintenance" # 检维修作业
    # ── 合规与应急 ─────────────────────────────────────────────
    HAZARD_REPORT      = "hazard_report"         # 隐患上报
    WORK_PERMIT        = "work_permit"           # 作业票证（动火/受限空间/高处等）
    INCIDENT_REPORT    = "incident_report"       # 事故上报
    EMERGENCY_RESPONSE = "emergency_response"    # 应急处置（泄漏/火灾/中毒）
    CHEMICAL_SAFETY    = "chemical_safety"       # 危化品管理
    TRAINING_CERT      = "training_cert"         # 培训与特种作业取证
    INSPECTION_AUDIT   = "inspection_audit"      # 迎检/标准化考核
    OCC_HEALTH         = "occupational_health"   # 职业健康
    # ── 通用安全 ───────────────────────────────────────────────
    HAZARD_INSPECTION  = "hazard_inspection"     # 隐患排查
    REGULATION_QUERY   = "regulation_query"      # 法规标准查询
    PPE_INQUIRY        = "ppe_inquiry"           # 防护用品选用
    SAFETY_DOCUMENT    = "safety_document"       # 规程/制度文档
    GENERAL_CONSULT    = "general_consult"       # 一般安全咨询
    # ── 交互类 ─────────────────────────────────────────────────
    GREETING           = "greeting"              # 问候
    FEEDBACK           = "feedback"              # 正面反馈
    ESCALATION         = "escalation"            # 转人工/升级
    OTHER              = "other"


class UrgencyLevel(Enum):
    LOW      = 1
    MEDIUM   = 2
    HIGH     = 3
    CRITICAL = 4


@dataclass
class IntentResult:
    intent:     IntentCategory
    confidence: float
    urgency:    UrgencyLevel
    intent_group: str
    entities:   Dict[str, List[str]]   # 从消息中提取的实体
    reasoning:  str
    latency_ms: float
    source_scores: Dict[str, float] = field(default_factory=dict)


# ── Few-shot 模板（同时用于 LLM 示例和 Embedding 匹配）────────────────────────
_TEMPLATES: Dict[IntentCategory, List[str]] = {
    IntentCategory.EQUIPMENT_ALARM: [
        "反应器温度报警了怎么处理？",
        "压缩机振动值高报警",
        "压力超限联锁动作了怎么办？",
    ],
    IntentCategory.EQUIPMENT_MAINT: [
        "泵检修前要做哪些安全准备？",
        "反应釜检修作业有什么要求？",
        "装置停车检修需要办什么手续？",
    ],
    IntentCategory.HAZARD_REPORT: [
        "发现隐患应该怎么上报？",
        "管道有裂纹我去哪里报告？",
        "罐区围堰有裂缝，上报给谁？",
    ],
    IntentCategory.WORK_PERMIT: [
        "动火作业票怎么办、谁来审批？",
        "受限空间作业票办理流程是什么？",
        "高处作业许可证有效期多久？",
    ],
    IntentCategory.INCIDENT_REPORT: [
        "发生事故后上报流程是什么？",
        "事故报告有时限要求吗？",
        "工伤事故要报给哪些部门？",
    ],
    IntentCategory.EMERGENCY_RESPONSE: [
        "甲醇泄漏了怎么应急处置？",
        "车间着火了第一步做什么？",
        "有人吸入有毒气体怎么急救？",
    ],
    IntentCategory.CHEMICAL_SAFETY: [
        "危化品库房储存有什么要求？",
        "甲醇和浓硫酸能放在一起吗？",
        "MSDS 在哪里可以查？",
    ],
    IntentCategory.TRAINING_CERT: [
        "电工证到期了怎么复审？",
        "新员工三级安全教育包括什么？",
        "特种作业操作证在哪里办理？",
    ],
    IntentCategory.INSPECTION_AUDIT: [
        "迎检需要准备哪些安全资料？",
        "安全生产标准化考核怎么评分？",
        "上级检查发现的问题怎么整改闭环？",
    ],
    IntentCategory.OCC_HEALTH: [
        "职业健康体检周期是多久？",
        "噪声岗位职业病防护有什么要求？",
        "职业健康档案包括哪些内容？",
    ],
    IntentCategory.HAZARD_INSPECTION: [
        "日常安全检查主要查哪些内容？",
        "帮我列一份配电室隐患排查清单",
        "罐区巡检要注意什么风险点？",
    ],
    IntentCategory.REGULATION_QUERY: [
        "安全生产法对隐患排查治理有什么要求？",
        "危化品储存的最新标准是什么？",
        "GB30871 对特殊作业怎么规定？",
    ],
    IntentCategory.PPE_INQUIRY: [
        "进装置区需要穿什么防护用品？",
        "防毒面具怎么选型？",
        "安全带的使用要求是什么？",
    ],
    IntentCategory.SAFETY_DOCUMENT: [
        "动火操作规程在哪里查？",
        "公司的安全生产责任制文件叫什么？",
        "有没有反应釜的操作规程？",
    ],
    IntentCategory.GENERAL_CONSULT: [
        "安全生产主要包括哪些方面？",
        "什么是双重预防机制？",
        "安全风险分级管控怎么理解？",
    ],
    IntentCategory.GREETING: ["你好", "嗨，有人吗", "早上好"],
    IntentCategory.FEEDBACK: ["回答得很清楚，谢谢！", "非常专业", "问题解决了，感谢"],
    IntentCategory.ESCALATION: ["转人工，我要找安全专家", "这个问题处理不了，找你们主管", "给我升级处理"],
}

_SPECIFIC_INTENTS = {
    IntentCategory.EQUIPMENT_ALARM,
    IntentCategory.EQUIPMENT_MAINT,
    IntentCategory.HAZARD_REPORT,
    IntentCategory.WORK_PERMIT,
    IntentCategory.INCIDENT_REPORT,
    IntentCategory.EMERGENCY_RESPONSE,
    IntentCategory.CHEMICAL_SAFETY,
    IntentCategory.TRAINING_CERT,
    IntentCategory.INSPECTION_AUDIT,
    IntentCategory.OCC_HEALTH,
    IntentCategory.HAZARD_INSPECTION,
    IntentCategory.REGULATION_QUERY,
    IntentCategory.PPE_INQUIRY,
    IntentCategory.SAFETY_DOCUMENT,
}

_GENERIC_INTENTS = {
    IntentCategory.GENERAL_CONSULT,
    IntentCategory.GREETING,
    IntentCategory.FEEDBACK,
    IntentCategory.ESCALATION,
}

# 细粒度意图 → 粗粒度领域组（供路由原因展示和下游分流）
_INTENT_GROUPS: Dict[IntentCategory, str] = {
    IntentCategory.EQUIPMENT_ALARM:    "equipment",
    IntentCategory.EQUIPMENT_MAINT:    "equipment",
    IntentCategory.HAZARD_REPORT:      "compliance",
    IntentCategory.WORK_PERMIT:        "compliance",
    IntentCategory.INCIDENT_REPORT:    "compliance",
    IntentCategory.EMERGENCY_RESPONSE: "compliance",
    IntentCategory.CHEMICAL_SAFETY:    "compliance",
    IntentCategory.TRAINING_CERT:      "compliance",
    IntentCategory.INSPECTION_AUDIT:   "compliance",
    IntentCategory.OCC_HEALTH:         "compliance",
    IntentCategory.HAZARD_INSPECTION:  "general",
    IntentCategory.REGULATION_QUERY:   "general",
    IntentCategory.PPE_INQUIRY:        "general",
    IntentCategory.SAFETY_DOCUMENT:    "general",
    IntentCategory.GENERAL_CONSULT:    "general",
    IntentCategory.GREETING:           "general",
    IntentCategory.FEEDBACK:           "general",
    IntentCategory.ESCALATION:         "escalation",
}

# 紧急关键词（安全场景从严：后果类词直接 CRITICAL）
_URGENCY_KEYWORDS = {
    UrgencyLevel.CRITICAL: [
        "着火", "起火", "火灾", "爆炸", "中毒", "窒息", "坍塌", "倒塌",
        "触电", "大量泄漏", "救人", "被困", "有人受伤", "紧急", "立即", "立刻",
    ],
    UrgencyLevel.HIGH: [
        "泄漏", "报警", "超温", "超压", "事故", "伤亡", "烧伤", "灼伤",
        "骨折", "马上", "尽快", "今天", "urgent",
    ],
    UrgencyLevel.MEDIUM: ["这周", "隐患", "整改", "复查", "快点", "soon"],
}

# 常见危化品词表（实体抽取用，命中即记 chemical 实体）
_CHEMICAL_VOCAB = [
    "甲醇", "乙醇", "甲苯", "二甲苯", "丙酮", "苯", "甲醛", "液氨", "氨水",
    "氯气", "盐酸", "硫酸", "硝酸", "氢氧化钠", "氢气", "氮气", "氩气",
    "二氧化碳", "液化气", "天然气", "氯乙烯", "乙烯", "丙烯", "甲烷",
    "乙炔", "柴油", "汽油", "煤油",
]

_WORK_TYPES = ("动火", "受限空间", "高处", "吊装", "临时用电", "盲板", "断路", "动土")


def _cosine(a: List[float], b: List[float]) -> float:
    """纯 Python 余弦相似度，不依赖 numpy。"""
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class IntentRecognizer:
    """
    端到端意图识别器（安全生产域）。

    初始化时不加载任何本地模型，所有 AI 能力通过 Anthropic API 调用。
    模板 Embedding 在首次请求时懒加载并缓存，后续复用。
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        confidence_threshold: float = 0.5,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client    = AsyncAnthropic(**kwargs)
        self.model     = model
        self.threshold = confidence_threshold
        # 第三方兼容 API（如 DeepSeek）通常不支持 Embedding，禁用该策略。
        # 官方 Anthropic SDK 当前没有 embeddings 资源，因此下面会使用稳定的
        # 本地字符 n-gram 向量作为轻量兜底，保证三路融合链路真实可跑。
        self._embedding_enabled = not bool(base_url)

        self._tpl_embeddings: Dict[IntentCategory, List[List[float]]] = {}
        self._cache: Dict[str, IntentResult] = {}
        self.cache_hits   = 0
        self.cache_misses = 0

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> IntentResult:
        """
        识别用户意图。

        history 格式：[{"role": "user"/"assistant", "content": "..."}]
        """
        key = self._cache_key(message, history)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
        self.cache_misses += 1

        t0 = time.monotonic()

        # LLM 和 Embedding 并行（Embedding 不可用时跳过）
        llm_task = asyncio.create_task(self._llm_recognize(message, history))
        emb_task = asyncio.create_task(self._embedding_recognize(message)) if self._embedding_enabled else None
        pat      = self._pattern_recognize(message)

        if emb_task:
            llm, emb = await asyncio.gather(llm_task, emb_task)
        else:
            llm = await llm_task
            emb = {"intent": IntentCategory.OTHER, "confidence": 0.0}

        intent, confidence, source_scores = self._vote(llm, emb, pat)
        entities = self._extract_entities(message)
        urgency  = self._urgency(message, intent)

        result = IntentResult(
            intent=intent,
            confidence=confidence,
            urgency=urgency,
            intent_group=self._intent_group(intent),
            entities=entities,
            reasoning=llm.get("reasoning", ""),
            latency_ms=(time.monotonic() - t0) * 1000,
            source_scores=source_scores,
        )

        # LRU 缓存
        if len(self._cache) >= 1000:
            for k in list(self._cache)[:500]:
                del self._cache[k]
        self._cache[key] = result
        return result

    def learn(self, message: str, correct: IntentCategory) -> None:
        """在线学习：将纠正样本加入模板，清除对应 Embedding 缓存。"""
        tpls = _TEMPLATES.setdefault(correct, [])
        if message not in tpls:
            tpls.append(message)
            self._tpl_embeddings.pop(correct, None)  # 下次重新计算
            logger.info(f"学习新样本 → {correct.value}: {message[:40]}")

    # ── 三路识别策略 ──────────────────────────────────────────────────────────

    async def _llm_recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]],
    ) -> Dict[str, Any]:
        """策略 1：LLM 语义理解（Few-shot + 上下文）。"""
        message = self._clean_text(message)
        # 构建 Few-shot 示例
        examples = "\n".join(
            f'  消息: "{t}" → 意图: {cat.value}'
            for cat, tpls in _TEMPLATES.items()
            for t in tpls[:1]  # 每类取 1 条，控制 prompt 长度
        )
        # 最近 3 轮对话上下文
        ctx = ""
        if history:
            ctx = "\n最近对话:\n" + "\n".join(
                f"  {self._clean_text(m.get('role', 'user'))}: {self._clean_text(m.get('content', ''))}"
                for m in history[-3:]
            )

        prompt = f"""你是安全生产领域的意图分析专家。根据示例判断用户意图，返回 JSON。
如果用户问题能匹配细粒度安全业务意图，请优先返回细粒度意图，而不是宽泛大类。
例如作业票办理优先返回 work_permit，设备报警处置优先返回 equipment_alarm，
法规条款查询优先返回 regulation_query，隐患上报优先返回 hazard_report。
出现着火、爆炸、中毒、泄漏等后果描述时通常属于 emergency_response 或 incident_report。

示例:
{examples}

{ctx}
用户消息: "{message}"

返回格式（仅 JSON，不要其他文字）:
{{"intent": "<意图值>", "confidence": <0-1>, "reasoning": "<一句话说明>"}}

可选意图: {", ".join(c.value for c in IntentCategory)}"""
        prompt = self._clean_text(prompt)

        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=256,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            try:
                data["intent"] = IntentCategory(data["intent"])
            except ValueError:
                data["intent"] = IntentCategory.OTHER
            return data
        except Exception as ex:
            logger.warning(f"LLM 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM 失败", "failed": True}

    async def _embedding_recognize(self, message: str) -> Dict[str, Any]:
        """策略 2：Embedding 向量相似度匹配。"""
        try:
            await self._load_template_embeddings()
            msg_vec = await self._embed_text(message)

            best_cat, best_score = IntentCategory.OTHER, 0.0
            for cat, vecs in self._tpl_embeddings.items():
                score = max(_cosine(msg_vec, v) for v in vecs)
                if score > best_score:
                    best_score, best_cat = score, cat

            return {"intent": best_cat, "confidence": best_score}
        except Exception as ex:
            logger.warning(f"Embedding 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    def _pattern_recognize(self, message: str) -> Dict[str, Any]:
        """策略 3：关键词模式匹配（同步，零延迟兜底）。"""
        msg = message.lower()
        specific_patterns = {
            IntentCategory.WORK_PERMIT: ["作业票", "动火", "受限空间", "高处作业", "吊装", "临时用电", "盲板", "许可证", "票证"],
            IntentCategory.EMERGENCY_RESPONSE: ["泄漏", "着火", "火灾", "爆炸", "中毒", "窒息", "应急处置", "急救", "逃生"],
            IntentCategory.EQUIPMENT_ALARM: ["报警", "超温", "超压", "超液位", "联锁", "振动值", "仪表失灵"],
            IntentCategory.EQUIPMENT_MAINT: ["检修", "检维修", "维修作业", "停车检修", "大修"],
            IntentCategory.CHEMICAL_SAFETY: ["危化品", "危险化学品", "msds", "相容", "化学品的", "化学品储存"],
            IntentCategory.INCIDENT_REPORT: ["事故上报", "事故报告", "工伤", "伤亡", "上报流程"],
            IntentCategory.HAZARD_REPORT: ["隐患上报", "上报隐患", "报告隐患", "发现隐患", "隐患报告"],
            IntentCategory.HAZARD_INSPECTION: ["隐患排查", "排查清单", "安全检查", "巡检", "检查表"],
            IntentCategory.REGULATION_QUERY: ["安全生产法", "法规", "标准", "gb", "条款", "管理办法", "怎么规定"],
            IntentCategory.TRAINING_CERT: ["三级教育", "特种作业", "操作证", "取证", "复审", "培训"],
            IntentCategory.INSPECTION_AUDIT: ["迎检", "检查组", "标准化", "外审", "督导", "考核"],
            IntentCategory.OCC_HEALTH: ["职业健康", "职业病", "体检", "噪声岗位", "职业危害"],
            IntentCategory.PPE_INQUIRY: ["防护用品", "劳保", "防毒面具", "安全带", "护目镜", "防护服", "耳塞"],
            IntentCategory.SAFETY_DOCUMENT: ["操作规程", "规程", "责任制", "制度文件", "应急预案文件"],
        }
        generic_patterns = {
            IntentCategory.ESCALATION: ["转人工", "人工", "升级", "投诉", "找主管", "找专家"],
            IntentCategory.FEEDBACK:   ["谢谢", "感谢", "很棒", "好评", "专业"],
            IntentCategory.GREETING:   ["你好", "您好", "嗨", "hello", "hi", "早上好", "下午好"],
            IntentCategory.GENERAL_CONSULT: ["怎么", "什么", "如何", "咨询", "帮助", "?", "？"],
        }

        best_cat, best_score = self._best_pattern_match(msg, specific_patterns)
        if best_cat != IntentCategory.OTHER:
            return {"intent": best_cat, "confidence": best_score}

        best_cat, best_score = self._best_pattern_match(msg, generic_patterns)
        return {"intent": best_cat, "confidence": best_score}

    # ── 投票合并 ──────────────────────────────────────────────────────────────

    def _vote(self, llm: Dict, emb: Dict, pat: Dict) -> tuple[IntentCategory, float, Dict[str, float]]:
        """加权投票。返回最终意图、融合置信度和各路来源得分。"""
        source_scores = {
            "llm": float(llm.get("confidence", 0.0) or 0.0),
            "embedding": float(emb.get("confidence", 0.0) or 0.0),
            "pattern": float(pat.get("confidence", 0.0) or 0.0),
        }
        if llm.get("failed"):
            if emb.get("intent") != IntentCategory.OTHER and emb.get("confidence", 0.0) > 0:
                return emb["intent"], source_scores["embedding"], source_scores
            if pat.get("intent") != IntentCategory.OTHER and pat.get("confidence", 0.0) > 0:
                return pat["intent"], source_scores["pattern"], source_scores
            return IntentCategory.OTHER, 0.0, source_scores

        if self._embedding_enabled:
            weights = [(llm, 0.7), (emb, 0.2), (pat, 0.1)]
        else:
            weights = [(llm, 0.85), (pat, 0.15)]
        scores: Dict[IntentCategory, float] = {}
        for result, w in weights:
            cat  = result.get("intent", IntentCategory.OTHER)
            conf = result.get("confidence", 0.0)
            scores[cat] = scores.get(cat, 0.0) + w * conf

        best = max(scores, key=scores.get)  # type: ignore
        best_score = scores[best]
        pat_intent = pat.get("intent", IntentCategory.OTHER)
        pat_conf = float(pat.get("confidence", 0.0) or 0.0)
        if best in _GENERIC_INTENTS and pat_intent in _SPECIFIC_INTENTS and pat_conf >= 0.5 and best_score < 0.8:
            source_scores["refined_by_pattern"] = pat_conf
            return pat_intent, max(best_score, pat_conf), source_scores
        if best_score < self.threshold:
            return IntentCategory.OTHER, best_score, source_scores
        return best, best_score, source_scores

    # ── 实体提取 ──────────────────────────────────────────────────────────────

    def _extract_entities(self, message: str) -> Dict[str, List[str]]:
        """用规则提取安全领域高价值实体，避免每次识别都额外调用 LLM。"""
        message = self._clean_text(message)
        equipment_ids = re.findall(
            r"(?:设备号?|位号|编号)\s*[:：#]?\s*([A-Za-z0-9_-]{2,32})", message
        ) + re.findall(r"\b([A-Z]{1,3}-\d{2,4}(?:-[A-Z0-9]{1,4})?)\b", message)
        return {
            "equipment_id": self._unique(equipment_ids),
            "work_type": self._unique([w for w in _WORK_TYPES if w in message]),
            "area": self._unique(re.findall(r"([\u4e00-\u9fa5]{2,8}?(?:车间|装置|罐区|库房|工段|班组))", message)),
            "chemical": self._unique([c for c in _CHEMICAL_VOCAB if c in message]),
            "date": self._unique(re.findall(r"(今天|明天|昨天|本周|这周|下周|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)", message)),
        }

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    async def _load_template_embeddings(self) -> None:
        """懒加载所有模板的 Embedding（只在首次调用时执行）。"""
        missing = [cat for cat in _TEMPLATES if cat not in self._tpl_embeddings]
        if not missing:
            return

        all_texts = [t for cat in missing for t in _TEMPLATES[cat]]
        vecs = [await self._embed_text(text) for text in all_texts]
        idx = 0
        for cat in missing:
            n = len(_TEMPLATES[cat])
            self._tpl_embeddings[cat] = vecs[idx: idx + n]
            idx += n

    async def _embed_text(self, text: str) -> List[float]:
        """
        生成文本向量。

        如果未来接入的官方/兼容客户端提供 embeddings.create，会优先使用远端向量；
        当前 Anthropic SDK 没有该资源时，退化为字符 n-gram 哈希向量。这样不会因为
        Embedding 服务缺失导致三路融合中断。
        """
        embeddings = getattr(self.client, "embeddings", None)
        if embeddings is not None:
            try:
                resp = await embeddings.create(model="voyage-3-lite", input=[text])
                return list(resp.data[0].embedding)
            except Exception as ex:
                logger.warning(f"远端 Embedding 失败，使用本地向量兜底: {ex}")

        return self._local_embedding(text)

    @staticmethod
    def _local_embedding(text: str, dims: int = 256) -> List[float]:
        """稳定的字符 n-gram 哈希向量，用于无远端 Embedding 时的语义近似匹配。"""
        normalized = text.lower().strip()
        vec = [0.0] * dims
        tokens = set()
        for n in (1, 2, 3):
            if len(normalized) >= n:
                tokens.update(normalized[i:i + n] for i in range(len(normalized) - n + 1))
        if not tokens:
            tokens.add(normalized)

        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return vec

    def _urgency(self, message: str, intent: IntentCategory) -> UrgencyLevel:
        msg = message.lower()
        for level, kws in _URGENCY_KEYWORDS.items():
            if any(kw in msg for kw in kws):
                return level
        if intent in (
            IntentCategory.ESCALATION,
            IntentCategory.EMERGENCY_RESPONSE,
            IntentCategory.INCIDENT_REPORT,
            IntentCategory.EQUIPMENT_ALARM,
        ):
            return UrgencyLevel.HIGH
        return UrgencyLevel.LOW

    def _cache_key(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> str:
        payload = {"message": self._clean_text(message)[:200]}
        if history:
            payload["history"] = [
                {
                    "role": self._clean_text(item.get("role", ""))[:20],
                    "content": self._clean_text(item.get("content", ""))[:160],
                }
                for item in history[-3:]
            ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _unique(values: List[str]) -> List[str]:
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))

    @staticmethod
    def _best_pattern_match(
        message: str,
        patterns: Dict[IntentCategory, List[str]],
    ) -> tuple[IntentCategory, float]:
        best_cat, best_score = IntentCategory.OTHER, 0.0
        for cat, kws in patterns.items():
            hits = sum(1 for kw in kws if kw in message)
            if not hits:
                continue
            # 单个明确业务关键词就给可用置信度；多个关键词命中时提高置信度。
            score = min(1.0, 0.5 + 0.25 * (hits - 1))
            if score > best_score:
                best_score, best_cat = score, cat
        return best_cat, best_score

    @staticmethod
    def _intent_group(intent: IntentCategory) -> str:
        return _INTENT_GROUPS.get(intent, "general")

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 HTTP 客户端编码 prompt 时崩溃。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @property
    def cache_stats(self) -> Dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "size": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": self.cache_hits / total if total else 0.0,
        }
