"""
亮点：多 Agent 路由与编排（安全生产域）

核心问题：多 Agent 情况下如何做 Routing？

路由策略（三层决策）：
  1. 意图路由 —— 根据 IntentCategory 直接映射到专属 Agent
     （设备工艺安全 / 安全合规应急 / 安全生产协调 / 人工升级接待）
  2. 性能路由 —— 同类 Agent 有多个时，选成功率最高、延迟最低的
  3. 降级路由 —— 专属 Agent 不可用时，自动降级到 SafetyCoordinationAgent

并行协作：
  - 复杂问题（如"设备报警 + 作业票办理"）可同时派发给多个 Agent
  - 结果由 Orchestrator 合并后返回

升级机制：
  - CRITICAL 紧急度 / 转人工意图 / Agent 输出建议升级 → 路由到 EscalationAgent
"""
import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from core.llm_utils import extract_text_content

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

class AgentType(Enum):
    GENERAL    = "general"     # 安全生产协调（兜底）
    EQUIPMENT  = "equipment"   # 设备与工艺安全
    COMPLIANCE = "compliance"  # 安全合规与应急
    ESCALATION = "escalation"  # 人工升级接待


@dataclass
class AgentStats:
    """Agent 运行时统计，供 Monitor 和路由决策使用。"""
    total:     int   = 0
    success:   int   = 0
    total_ms:  float = 0.0
    monitor_penalty: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.0

    def routing_score(self) -> float:
        """路由评分：成功率高、延迟低的 Agent 得分高。"""
        latency_score = 1.0 / (1.0 + self.avg_ms / 1000)
        base_score = self.success_rate * 0.7 + latency_score * 0.3
        return base_score * max(0.0, 1.0 - self.monitor_penalty)


@dataclass
class AgentResponse:
    agent_type:  AgentType
    content:     str
    success:     bool
    confidence:  float = 1.0
    latency_ms:  float = 0.0
    escalate:    bool  = False   # 是否需要升级


@dataclass
class Request:
    message:     str
    user_id:     str
    conv_id:     str
    context:     str = ""        # 来自 MemoryManager 的格式化上下文
    history:     Optional[List[Dict[str, str]]] = None  # 对话历史，传给意图识别
    entities:    Dict[str, List[str]] = field(default_factory=dict)
    intent:      Optional[IntentCategory] = None
    intent_group: Optional[str] = None
    urgency:     Optional[UrgencyLevel]   = None
    intent_confidence: float = 1.0
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class OrchestratorResult:
    request_id:  str
    response:    str
    agent_type:  AgentType
    intent:      Optional[IntentCategory]
    escalated:   bool  = False
    latency_ms:  float = 0.0
    agent_types: List[AgentType] = field(default_factory=list)
    primary_agent: Optional[AgentType] = None
    supporting_agents: List[AgentType] = field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0


@dataclass
class RoutingDecision:
    """一次请求的结构化路由决策。"""
    primary_agent: AgentType
    supporting_agents: List[AgentType] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0

    @property
    def agent_types(self) -> List[AgentType]:
        return [self.primary_agent] + self.supporting_agents

    @property
    def multi_agent(self) -> bool:
        return bool(self.supporting_agents)


# ── 基础 Agent ────────────────────────────────────────────────────────────────

class BaseAgent:
    """所有 Agent 的基类，封装 LLM 调用和统计。"""

    agent_type: AgentType
    system_prompt: str

    def __init__(self, client: AsyncAnthropic, model: str, skill_manager: Optional[Any] = None):
        self._client = client
        self._model  = model
        self._skill_manager = skill_manager
        self.stats   = AgentStats()

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        try:
            content = await self._call_llm(req)
            ms = (time.monotonic() - t0) * 1000
            self.stats.success += 1
            self.stats.total_ms += ms
            escalate = self._needs_escalation(content)
            return AgentResponse(
                agent_type=self.agent_type,
                content=content,
                success=True,
                latency_ms=ms,
                escalate=escalate,
            )
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self.stats.total_ms += ms
            logger.error(f"{self.agent_type.value} 处理失败: {ex}")
            return AgentResponse(
                agent_type=self.agent_type,
                content="抱歉，处理您的请求时出现问题，请稍后重试。",
                success=False,
                latency_ms=ms,
            )

    async def _call_llm(self, req: Request) -> str:
        def _clean(s: str) -> str:
            return s.encode("utf-8", errors="ignore").decode("utf-8")

        messages = []
        if req.context:
            messages.append({"role": "user", "content": f"[背景信息]\n{_clean(req.context)}"})
            messages.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
        if req.entities:
            entities_text = json.dumps(req.entities, ensure_ascii=False)
            messages.append({"role": "user", "content": f"[结构化实体]\n{_clean(entities_text)}"})
            messages.append({"role": "assistant", "content": "好的，我会结合这些结构化实体处理。"})
        messages.append({"role": "user", "content": _clean(req.message)})

        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=self._build_system_prompt(req),
            messages=messages,
        )
        return extract_text_content(resp.content)

    def _build_system_prompt(self, req: Request) -> str:
        """把动态加载的 Skills 拼入 system prompt，让业务规则随请求生效。"""
        if self._skill_manager is None:
            return self.system_prompt
        skill_prompt = self._skill_manager.prompt_for(req.message, self.agent_type.value)
        if not skill_prompt:
            return self.system_prompt
        return f"{self.system_prompt}\n\n[动态 Skills]\n{skill_prompt}"

    def _needs_escalation(self, content: str) -> bool:
        """检测 Agent 是否建议升级（简单关键词检测）。"""
        keywords = ["转人工", "人工客服", "人工处理", "升级处理", "应急指挥", "上报核实", "无法处理", "escalate", "specialist"]
        return any(kw in content for kw in keywords)


class SafetyCoordinationAgent(BaseAgent):
    """安全生产协调（兜底）：首轮咨询、隐患排查、规程法规类通用问题。"""
    agent_type    = AgentType.GENERAL
    system_prompt = (
        "你是 SafetyMind 安全生产助手。友好、简洁地回答用户的安全生产与作业咨询问题。"
        "如果问题超出你的能力范围，明确说明并建议升级给安全专家或转人工处置。"
    )


class EquipmentSafetyAgent(BaseAgent):
    """设备与工艺安全：报警处置、检维修、联锁与泄漏。"""
    agent_type    = AgentType.EQUIPMENT
    system_prompt = (
        "你是设备与工艺安全专家。专注于：设备异常、工艺参数报警、介质泄漏、联锁与检维修处置。"
        "提供清晰、可执行的应急处置步骤。遇到需要停产或专业处置的问题，说明需要升级。"
    )


class ComplianceEmergencyAgent(BaseAgent):
    """安全合规与应急：作业票证、危化品、事故上报、应急响应。"""
    agent_type    = AgentType.COMPLIANCE
    system_prompt = (
        "你是安全合规与应急专家。专注于：作业票证、许可审批、危化品管理、事故上报与应急响应。"
        "对合规与应急事项保持准确和审慎。涉及重大风险处置时，说明需要升级给应急指挥或上级。"
    )


class EscalationAgent(BaseAgent):
    """人工升级接待：不自行处置重大风险，负责确认诉求、留痕和引导升级路径。"""
    agent_type    = AgentType.ESCALATION
    system_prompt = (
        "你是安全生产升级接待专员。用户已被路由到人工/升级通道，你的职责是："
        "1) 快速确认关键信息（地点/装置、涉事设备或介质、是否有人员受伤被困）；"
        "2) 明确告知已记录并转应急指挥或安全值班人员，不在对话中自行处置重大风险；"
        "3) 如现场存在火灾、爆炸、中毒、泄漏等紧迫风险，优先提示先撤离、先报告、按应急预案行动。"
        "回复要简短、镇定、可执行。"
    )


# ── 编排器 ────────────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    多 Agent 编排器。

    路由逻辑（三层）：
      1. 意图 → Agent 类型映射
      2. 同类多实例时按 routing_score() 选最优
      3. 专属 Agent 失败时降级到 GeneralAgent
    """

    # 意图 → Agent 类型的静态映射（路由表）
    _INTENT_ROUTING: Dict[IntentCategory, AgentType] = {
        IntentCategory.EQUIPMENT_ALARM: AgentType.EQUIPMENT,
        IntentCategory.EQUIPMENT_MAINT: AgentType.EQUIPMENT,
        IntentCategory.HAZARD_REPORT:      AgentType.COMPLIANCE,
        IntentCategory.WORK_PERMIT:        AgentType.COMPLIANCE,
        IntentCategory.INCIDENT_REPORT:    AgentType.COMPLIANCE,
        IntentCategory.CHEMICAL_SAFETY:    AgentType.COMPLIANCE,
        IntentCategory.TRAINING_CERT:      AgentType.COMPLIANCE,
        IntentCategory.INSPECTION_AUDIT:   AgentType.COMPLIANCE,
        IntentCategory.OCC_HEALTH:         AgentType.COMPLIANCE,
        IntentCategory.ESCALATION:         AgentType.ESCALATION,
        # 其余意图（隐患排查/法规/防护/规程/一般咨询/问候等）→ GENERAL（默认）
    }

    def __init__(
        self,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        skill_manager: Optional[Any] = None,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncAnthropic(**kwargs)

        self._intent_recognizer = IntentRecognizer(api_key=api_key, base_url=base_url, model=model)
        self._skill_manager = skill_manager

        # Agent 池：每种类型可有多个实例（水平扩展）
        self._pool: Dict[AgentType, List[BaseAgent]] = {
            AgentType.GENERAL:    [SafetyCoordinationAgent(client, model, skill_manager)],
            AgentType.EQUIPMENT:  [EquipmentSafetyAgent(client, model, skill_manager)],
            AgentType.COMPLIANCE: [ComplianceEmergencyAgent(client, model, skill_manager)],
            AgentType.ESCALATION: [EscalationAgent(client, model, skill_manager)],
        }

    def set_skill_manager(self, skill_manager: Optional[Any]) -> None:
        """更新 SkillManager 引用，供运行时重载或测试替换使用。"""
        self._skill_manager = skill_manager
        for agents in self._pool.values():
            for agent in agents:
                agent._skill_manager = skill_manager

    async def recognize_intent(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ):
        """对外暴露意图识别，供 API 层先判断是否需要 RAG 等前置能力。"""
        return await self._intent_recognizer.recognize(message, history=history)

    # ── 主入口 ────────────────────────────────────────────────────────────────

    # CRITICAL 紧急事态的模板化应急响应（零 LLM 依赖）：
    # 紧急情况的秒级确定性响应优于生成式回答（更快、无幻觉、端点故障时在线）。
    # 内容为通用应急要点，现场处置以本单位应急预案和应急指挥指令为准。
    CRITICAL_TEMPLATES = {
        IntentCategory.EMERGENCY_RESPONSE: (
            """【紧急事态·立即行动】您的上报已自动触发应急响应，请按顺序执行：
1. 自身防护：佩戴防护用品，严禁贸然进入危险区域；
2. 疏散隔离：组织无关人员向上风向撤离，设置警戒线；
3. 控制源头：在确保自身安全的前提下切断泄漏源/电源/火源；
4. 呼叫求援：立即拨打本单位应急电话及 119/120，说明地点、物质、伤亡情况；
5. 现场引导：等候专业处置力量，提供您掌握的信息。
【已自动完成】升级流程已触发，应急指挥将收到本条上报（时间/内容已留痕）。
重要提示：以上为通用应急要点，现场处置以本单位应急预案和应急指挥指令为准。"""
        ),
        IntentCategory.INCIDENT_REPORT: (
            """【事故上报·立即行动】您的上报已自动触发升级流程：
1. 抢救伤员、保护现场，防止事故扩大；
2. 立即拨打本单位应急电话及 120（如有人员伤亡），并按规定时限上报；
3. 保留现场证据（照片/位置/时间/涉事设备与物质）；
4. 配合调查，不瞒报、不谎报、不迟报。
【已自动完成】本条上报已留痕，安全值班人员将收到通知。"""
        ),
    }
    CRITICAL_GENERIC = (
        """【紧急事态·已触发升级】您的上报属于紧急安全事项：
1. 立即撤离危险区域并组织人员疏散；
2. 拨打本单位应急电话及 119/120；
3. 本条上报已留痕，应急指挥将收到通知。
重要提示：现场处置以本单位应急预案和应急指挥指令为准。"""
    )

    async def run(self, req: Request) -> OrchestratorResult:
        """
        处理一次请求的完整流程：
          意图识别 → 路由选 Agent → 执行 → 检查升级 → 返回结果
        """
        t0 = time.monotonic()

        # 1. 意图识别（如果调用方已识别则跳过）
        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent  = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence

        if self._needs_clarification(req):
            return OrchestratorResult(
                request_id=req.request_id,
                response="我还不能确定您要处理的是哪类安全问题。请补充一下是隐患排查、隐患上报、作业票咨询、设备报警，还是事故应急？",
                agent_type=AgentType.GENERAL,
                intent=req.intent,
                escalated=False,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.GENERAL],
                primary_agent=AgentType.GENERAL,
                routing_reason="低置信度 OTHER 意图，先澄清用户需求",
                routing_confidence=req.intent_confidence,
            )

        # CRITICAL 紧急事态：模板化应急响应（零 LLM 依赖）。
        # 设计依据：紧急情况的秒级确定性响应优于生成式回答（更快、无幻觉、
        # 端点故障时安全关键路径在线）；SAFETYMIND_CRITICAL_TEMPLATE=0 可关闭。
        if (os.getenv("SAFETYMIND_CRITICAL_TEMPLATE", "1") == "1"
                and req.urgency == UrgencyLevel.CRITICAL):
            template = self.CRITICAL_TEMPLATES.get(req.intent, self.CRITICAL_GENERIC)
            logger.warning(f"请求 {req.request_id} CRITICAL 紧急事态，模板化响应（零 LLM 依赖）")
            return OrchestratorResult(
                request_id=req.request_id,
                response=template,
                agent_type=AgentType.ESCALATION,
                intent=req.intent,
                escalated=True,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.ESCALATION],
                primary_agent=AgentType.ESCALATION,
                routing_reason="CRITICAL 紧急事态，模板化应急响应（零 LLM 依赖）",
                routing_confidence=1.0,
            )

        # 复杂问题自动并行协作，例如同一句同时涉及设备报警和作业票办理。
        decision = self._route_decision(req)
        if decision.multi_agent:
            return await self.run_parallel(req, decision)

        # 2. 执行主 Agent（含降级）
        response = await self._execute(req, decision.primary_agent)

        # 4. 升级检查
        escalated = False
        if response.escalate or req.urgency == UrgencyLevel.CRITICAL or req.intent == IntentCategory.ESCALATION:
            escalated = True
            logger.warning(f"请求 {req.request_id} 触发升级: urgency={req.urgency}")
            # 生产环境：此处创建工单、通知安全值班/应急指挥

        return OrchestratorResult(
            request_id=req.request_id,
            response=response.content,
            agent_type=response.agent_type,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[response.agent_type],
            primary_agent=decision.primary_agent,
            supporting_agents=[],
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
        )

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        """
        并行派发给多个 Agent，合并结果。
        适用于复杂问题（如同时涉及设备处置和作业票合规）。
        """
        t0 = time.monotonic()
        agent_types = decision.agent_types
        tasks = [self._execute(req, at) for at in agent_types]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并：主 Agent 在前，辅助 Agent 在后。
        parts = []
        for r in responses:
            if isinstance(r, AgentResponse) and r.success:
                role = "主处理" if r.agent_type == decision.primary_agent else "辅助处理"
                parts.append(f"[{r.agent_type.value} - {role}]\n{r.content}")

        combined = "\n\n".join(parts) if parts else "抱歉，所有 Agent 均处理失败。"
        escalated = any(isinstance(r, AgentResponse) and r.escalate for r in responses)

        return OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            agent_type=decision.primary_agent,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[
                r.agent_type for r in responses
                if isinstance(r, AgentResponse) and r.success
            ] or agent_types,
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
        )

    # ── 路由逻辑 ──────────────────────────────────────────────────────────────

    def _route(self, intent: Optional[IntentCategory], urgency: Optional[UrgencyLevel]) -> AgentType:
        """
        三层路由决策：
          1. 意图映射
          2. 紧急度覆盖（CRITICAL 直接升级）
          3. 默认 GENERAL
        """
        if urgency == UrgencyLevel.CRITICAL:
            return AgentType.ESCALATION

        if intent and intent in self._INTENT_ROUTING:
            target = self._INTENT_ROUTING[intent]
            # 如果目标类型有可用实例则使用，否则降级
            if target in self._pool and self._pool[target]:
                return target

        return AgentType.GENERAL

    def _route_decision(self, req: Request) -> RoutingDecision:
        """
        结构化路由决策。

        先处理紧急/转人工，再用领域分数决定主 Agent 和辅助 Agent。
        这样可以表达“主处理 + 辅助诊断”，避免关键词命中后无主次地拼接。
        """
        if req.urgency == UrgencyLevel.CRITICAL:
            return RoutingDecision(
                primary_agent=AgentType.ESCALATION,
                reason="紧急度为 CRITICAL，触发升级路由",
                confidence=1.0,
            )

        if req.intent == IntentCategory.ESCALATION:
            return RoutingDecision(
                primary_agent=AgentType.ESCALATION,
                reason=f"意图为 {req.intent.value if req.intent else 'unknown'}，触发升级路由",
                confidence=max(req.intent_confidence, 0.8),
            )

        scores = self._domain_scores(req)
        available_scores = {
            agent_type: score
            for agent_type, score in scores.items()
            if agent_type == AgentType.GENERAL or self._pool.get(agent_type)
        }
        if not available_scores:
            return RoutingDecision(
                primary_agent=AgentType.GENERAL,
                reason="无可用专属 Agent，降级到 GeneralAgent",
                confidence=0.1,
            )

        ordered = sorted(available_scores.items(), key=lambda item: item[1], reverse=True)
        primary_agent, primary_score = ordered[0]
        supporting_agents = [
            agent_type
            for agent_type, score in ordered[1:]
            if agent_type != AgentType.GENERAL and score >= 0.45 and score >= primary_score * 0.55
        ]

        reason = self._routing_reason(req, available_scores, primary_agent, supporting_agents)
        return RoutingDecision(
            primary_agent=primary_agent,
            supporting_agents=supporting_agents,
            reason=reason,
            confidence=round(min(primary_score, 1.0), 3),
        )

    def _domain_scores(self, req: Request) -> Dict[AgentType, float]:
        """按意图、关键词和实体为各领域 Agent 打分。"""
        msg = req.message.lower()
        scores = {
            AgentType.GENERAL: 0.1,
            AgentType.EQUIPMENT: 0.0,
            AgentType.COMPLIANCE: 0.0,
        }

        if req.intent in (
            IntentCategory.HAZARD_INSPECTION,
            IntentCategory.REGULATION_QUERY,
            IntentCategory.PPE_INQUIRY,
            IntentCategory.SAFETY_DOCUMENT,
            IntentCategory.GENERAL_CONSULT,
            IntentCategory.GREETING,
            IntentCategory.FEEDBACK,
            IntentCategory.OTHER,
        ):
            scores[AgentType.GENERAL] += 0.55

        if req.intent in (
            IntentCategory.EQUIPMENT_ALARM,
            IntentCategory.EQUIPMENT_MAINT,
        ):
            scores[AgentType.EQUIPMENT] += 0.75

        if req.intent in (
            IntentCategory.HAZARD_REPORT,
            IntentCategory.WORK_PERMIT,
            IntentCategory.INCIDENT_REPORT,
            IntentCategory.EMERGENCY_RESPONSE,
            IntentCategory.CHEMICAL_SAFETY,
            IntentCategory.TRAINING_CERT,
            IntentCategory.INSPECTION_AUDIT,
            IntentCategory.OCC_HEALTH,
        ):
            scores[AgentType.COMPLIANCE] += 0.75

        equipment_kws = [
            "报警", "超温", "超压", "超液位", "联锁", "esd", "sis", "dcs", "仪表",
            "泵", "压缩机", "反应器", "反应釜", "换热器", "阀门", "管道", "振动",
            "腐蚀", "泄漏", "故障", "停机", "检修", "检维修", "液位", "温度", "压力",
        ]
        compliance_kws = [
            "作业票", "动火", "受限空间", "高处作业", "吊装", "临时用电", "断路",
            "许可证", "审批", "危化品", "危险化学品", "重大危险源", "事故", "上报",
            "应急", "预案", "法规", "标准", "安全生产法", "处罚", "考核", "培训",
            "特种作业", "职业健康", "职业病", "体检", "工伤", "化学品",
        ]
        general_kws = [
            "隐患", "排查", "检查表", "整改", "防护", "劳保", "用品", "规程",
            "制度", "操作规程", "咨询", "帮助", "风险", "辨识", "双重预防",
        ]

        equipment_hits  = sum(1 for kw in equipment_kws if kw in msg)
        compliance_hits = sum(1 for kw in compliance_kws if kw in msg)
        general_hits    = sum(1 for kw in general_kws if kw in msg)

        scores[AgentType.EQUIPMENT]  += min(0.45, equipment_hits * 0.18)
        scores[AgentType.COMPLIANCE] += min(0.45, compliance_hits * 0.18)
        scores[AgentType.GENERAL]    += min(0.35, general_hits * 0.12)

        entities = req.entities or {}
        if entities.get("equipment_id"):
            scores[AgentType.EQUIPMENT] += 0.2
        if entities.get("work_type") or entities.get("chemical"):
            scores[AgentType.COMPLIANCE] += 0.2

        return {agent_type: round(score, 3) for agent_type, score in scores.items()}

    @staticmethod
    def _routing_reason(
        req: Request,
        scores: Dict[AgentType, float],
        primary_agent: AgentType,
        supporting_agents: List[AgentType],
    ) -> str:
        score_text = ", ".join(
            f"{agent_type.value}={score:.2f}"
            for agent_type, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
        )
        support_text = ", ".join(agent.value for agent in supporting_agents) or "none"
        intent = req.intent.value if req.intent else "unknown"
        return (
            f"intent={intent}, group={req.intent_group or 'unknown'}, "
            f"primary={primary_agent.value}, supporting={support_text}, scores=[{score_text}]"
        )

    def _collaboration_targets(self, req: Request) -> List[AgentType]:
        """
        判断是否需要多个 Agent 并行协作。

        意图识别通常只返回一个主意图；这里用领域关键词补充检测复合问题，
        例如"反应器超温报警且需要动火检修"需要设备和合规 Agent 同时处理。
        """
        msg = req.message.lower()
        targets: List[AgentType] = []

        equipment_kws = [
            "报警", "超温", "超压", "联锁", "泵", "压缩机", "反应器", "反应釜",
            "阀门", "管道", "振动", "泄漏", "故障", "停机", "检修", "检维修",
        ]
        compliance_kws = [
            "作业票", "动火", "受限空间", "高处", "吊装", "许可证", "审批",
            "危化品", "事故", "上报", "应急", "预案", "化学品",
        ]

        if req.intent in (
            IntentCategory.EQUIPMENT_ALARM,
            IntentCategory.EQUIPMENT_MAINT,
        ) or any(kw in msg for kw in equipment_kws):
            targets.append(AgentType.EQUIPMENT)
        if req.intent in (
            IntentCategory.HAZARD_REPORT,
            IntentCategory.WORK_PERMIT,
            IntentCategory.INCIDENT_REPORT,
            IntentCategory.EMERGENCY_RESPONSE,
            IntentCategory.CHEMICAL_SAFETY,
            IntentCategory.TRAINING_CERT,
            IntentCategory.INSPECTION_AUDIT,
            IntentCategory.OCC_HEALTH,
        ) or any(kw in msg for kw in compliance_kws):
            targets.append(AgentType.COMPLIANCE)

        # 保持顺序去重，并只返回当前有实例的 Agent 类型。
        deduped = list(dict.fromkeys(targets))
        return [agent_type for agent_type in deduped if self._pool.get(agent_type)]

    @staticmethod
    def _needs_clarification(req: Request) -> bool:
        """低置信度且无明确意图时，先追问，避免误路由。"""
        if req.intent != IntentCategory.OTHER:
            return False
        text = (req.message or "").strip()
        if len(text) <= 2:
            return False
        return req.intent_confidence < 0.5

    def _best_agent(self, agent_type: AgentType) -> Optional[BaseAgent]:
        """
        性能路由：从同类 Agent 中选 routing_score() 最高的。
        这是"基于在线表现动态调整路由"的核心。
        """
        agents = self._pool.get(agent_type, [])
        if not agents:
            return None
        return max(agents, key=lambda a: a.stats.routing_score())

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        """执行 Agent，失败时降级到 GeneralAgent。"""
        agent = self._best_agent(agent_type)
        if agent is None:
            agent = self._best_agent(AgentType.GENERAL)
        if agent is None:
            return AgentResponse(
                agent_type=AgentType.GENERAL,
                content="服务暂时不可用，请稍后重试。",
                success=False,
            )

        response = await agent.handle(req)

        # 专属 Agent 失败时降级到 GeneralAgent
        if not response.success and agent_type != AgentType.GENERAL:
            logger.warning(f"{agent_type.value} 失败，降级到 GeneralAgent")
            fallback = self._best_agent(AgentType.GENERAL)
            if fallback:
                response = await fallback.handle(req)

        return response

    # ── 统计（供 Monitor 读取）────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        result = {}
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                result[key] = {
                    "total":        agent.stats.total,
                    "success_rate": round(agent.stats.success_rate, 3),
                    "avg_ms":       round(agent.stats.avg_ms, 1),
                    "monitor_penalty": round(agent.stats.monitor_penalty, 3),
                    "routing_score": round(agent.stats.routing_score(), 3),
                }
        return result

    def update_routing_penalties(self, penalties: Dict[str, float]) -> None:
        """
        接收 Monitor 的在线表现反馈，动态调整路由惩罚项。

        penalties 的 key 使用 get_stats() 中的 agent key，例如 technical_0。
        """
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                penalty = penalties.get(key, 0.0)
                agent.stats.monitor_penalty = min(max(penalty, 0.0), 0.9)
