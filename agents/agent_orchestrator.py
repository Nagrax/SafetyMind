"""
亮点：多 Agent 路由与编排（安全生产域）——OCTO 式"员工/专家"解耦

架构（借鉴 OCTO：Employee Agent 有记忆的编排者 / Expert Agent 无状态执行者）：
  - EmployeeAgent（员工层编排者）：
      代表一线员工，读取 MemoryContext（会话摘要/相关历史/人员安全画像/最近对话），
      统一负责上下文装配（ExpertTask 任务包，一次装配、全部专家共享）与路由决策
      （CRITICAL 模板短路 → 澄清检查 → 领域评分路由 → 单/并行派发 → 升级检查）。
  - 领域专家（设备/合规/综合/升级，BaseAgent 子类）：
      无状态执行者。不再各自拼装 [背景信息]/[结构化实体]，只接收员工层装配好的
      任务包，叠加自身 system prompt 与动态 Skills 后调 LLM。
      专家保持可池化（_pool）、可并行（run_parallel 输出块格式不变）。
  - AgentOrchestrator（对外门面）：
      持有专家池与意图识别器，run() 仍是唯一入口——api 层与
      evaluation/evaluator.py 直调 orchestrator.run() 的用法不受影响。

路由策略（三层决策，与解耦前语义一致）：
  1. 意图路由 —— 根据 IntentCategory 直接映射到专属 Agent
     （设备工艺安全 / 安全合规应急 / 安全生产协调 / 人工升级接待）
  2. 性能路由 —— 同类 Agent 有多个时，选成功率最高、延迟最低的
  3. 降级路由 —— 专属 Agent 不可用时，自动降级到 SafetyCoordinationAgent

并行协作：
  - 复杂问题（如"设备报警 + 作业票办理"）可同时派发给多个 Agent
  - 结果由员工层合并后返回（[agent - 主处理/辅助处理] 块格式不变）

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
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from core.llm_utils import extract_text_content

if TYPE_CHECKING:  # 仅类型标注：agents 层不依赖 memory 模块运行时导入（chromadb/redis 重）
    from memory.conversation_memory import MemoryContext

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
    total:     int   = 0          # 已受理的调用数（含在途）
    success:   int   = 0
    failed:    int   = 0          # 已完成的失败数（2026-10-29 新增）
    total_ms:  float = 0.0
    monitor_penalty: float = 0.0

    @property
    def completed(self) -> int:
        """已完成的调用数（成功 + 失败）。"""
        return self.success + self.failed

    @property
    def in_flight(self) -> int:
        """在途调用数：已受理但还没返回。

        2026-10-29 实测缺陷：此前 success_rate = success / total，把**还在跑**的调用
        算成失败。真机上单次 /chat 生成 60~120s，Monitor 每 10s 采集一次，于是日志里
        持续刷 `[ERROR] compliance_0 的 agent_success_rate = 0.000，阈值 0.9`——
        实际是"第一个请求还没返回"，不是"成功率为 0"。同一错误值还会经
        `_routing_penalty` 反过来压该 Agent 的路由评分（成功率 <0.90 直接扣分），
        等于用"在途"惩罚"正在干活"的 Agent。
        """
        return max(0, self.total - self.completed)

    @property
    def success_rate(self) -> float:
        """只按**已完成**调用计算；没有完成样本时给 1.0（不虚报异常）。"""
        return self.success / self.completed if self.completed else 1.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.completed if self.completed else 0.0

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
    tool_trace:  List[Any] = field(default_factory=list)  # P0-A：本次使用的工具调用轨迹


@dataclass
class Request:
    message:     str
    user_id:     str
    conv_id:     str
    context:     str = ""        # 调用方已格式化的文本上下文（评测/CLI 兜底链路）
    history:     Optional[List[Dict[str, str]]] = None  # 对话历史，传给意图识别
    entities:    Dict[str, List[str]] = field(default_factory=dict)
    intent:      Optional[IntentCategory] = None
    intent_group: Optional[str] = None
    urgency:     Optional[UrgencyLevel]   = None
    intent_confidence: float = 1.0
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    # 员工层新增（可选，向后兼容）：结构化 MemoryContext（会话摘要/相关历史/
    # 人员安全画像/最近对话）。给出时由 EmployeeAgent 统一渲染装配，优先于 context。
    memory_context: Optional["MemoryContext"] = None
    # RAG 知识块（api 层检索结果），同样交由员工层统一拼装进任务包。
    knowledge:   str = ""


@dataclass
class ExpertTask:
    """员工层装配好的一次性任务包：专家只执行，不再自行装配上下文。

    messages 已包含 [背景信息]/[结构化实体]/用户消息的完整 LLM 消息序列，
    全部（并行）专家共享同一份——与解耦前各专家自拼的结果逐字节一致。
    """
    request_id: str
    message:    str
    context:    str = ""
    entities:   Dict[str, List[str]] = field(default_factory=dict)
    messages:   List[Dict[str, str]] = field(default_factory=list)


@dataclass
class OrchestratorResult:
    request_id:  str
    response:    str
    agent_type:  AgentType
    intent:      Optional[IntentCategory]
    escalated:   bool  = False
    escalation_basis: str = ""   # urgency / intent / model_hint / safety_net / ""（见 escalation_basis()）
    latency_ms:  float = 0.0
    agent_types: List[AgentType] = field(default_factory=list)
    primary_agent: Optional[AgentType] = None
    supporting_agents: List[AgentType] = field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    tool_trace:  List[Any] = field(default_factory=list)  # P0-A：本次请求的工具调用轨迹


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


# ── 专家层（无状态执行者）─────────────────────────────────────────────────────

def escalation_basis(urgency: "UrgencyLevel", intent: "IntentCategory",
                     model_hint: bool) -> str:
    """升级依据分类（2026-10-29 新增，additive）：把"升级"拆成确定性与模型建议两部分。

    ## 为什么

    `/chat` 与 `/chat/stream` 的同题对照实测（benchmarks/eval_stream_parity.py）：
    12 题里有 **3 题**两个端口的 `escalated` 不一致——根因是 `escalated` 把
    "模型正文里出现了『转人工/建议上报』等关键词"也算作升级，而生成文本是随机的。
    这既是"同一条问题要不要升级"的不稳定，也是测试无法断言的字段。

    ## 语义

    - `urgency`：CRITICAL 紧急度（确定性，独立于意图置信度）
    - `intent` ：意图本身是 ESCALATION（确定性）
    - `model_hint`：模型正文里的升级关键词（**随机**，只作建议，不应被自动化断言）
    - `""` ：不升级

    `escalated` 仍为"三者任一"（保持既有行为，不降低安全性），但下游/测试可以按
    `escalation_basis` 区分确定性部分，避免被模型措辞左右。
    """
    if urgency == UrgencyLevel.CRITICAL:
        return "urgency"
    if intent == IntentCategory.ESCALATION:
        return "intent"
    if model_hint:
        return "model_hint"
    return ""


class BaseAgent:
    """领域专家基类：无状态、可池化、可并行。

    只负责执行本身——计时/统计/升级检查与 LLM 调用；上下文装配已上移到
    员工层（EmployeeAgent.assemble_task），专家收到的 task.messages 即可直接发。
    """

    agent_type: AgentType
    system_prompt: str

    def __init__(self, client: AsyncAnthropic, model: str, skill_manager: Optional[Any] = None,
                 tool_executor: Optional[Any] = None):
        self._client = client
        self._model  = model
        self._skill_manager = skill_manager
        # P0-A：可选工具执行器。None = 旧行为（单次 create，不传 tools）。
        self._tool_executor = tool_executor
        self._last_tool_trace: List[Any] = []
        self.stats   = AgentStats()

    async def handle(self, task: ExpertTask) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        try:
            content = await self._call_llm(task)
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
                tool_trace=list(self._last_tool_trace),
            )
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self.stats.failed += 1
            self.stats.total_ms += ms
            logger.error(f"{self.agent_type.value} 处理失败: {ex}")
            return AgentResponse(
                agent_type=self.agent_type,
                content="抱歉，处理您的请求时出现问题，请稍后重试。",
                success=False,
                latency_ms=ms,
                tool_trace=list(self._last_tool_trace),
            )

    async def _call_llm(self, task: ExpertTask) -> str:
        # 专家不再注入背景：task.messages 由员工层装配（含 [背景信息]/[结构化实体]）。
        if self._tool_executor is None:
            # 未接工具执行器：与改造前逐字节一致的单次调用（无 tools）。
            try:
                _max_tokens = int(os.getenv("SAFETYMIND_MAX_TOKENS", "1024"))
            except ValueError:
                _max_tokens = 1024
            from core.llm_params import temperature_param
            _extra = {}
            _t = temperature_param()
            if _t is not None:
                _extra["temperature"] = _t      # 默认不发（None），见 core/llm_params.py
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=_max_tokens,
                system=self._build_system_prompt(task.message),
                messages=task.messages,
                **_extra,
            )
            return extract_text_content(resp.content)

        # 接工具执行器：走真实工具回路，执行治理（白名单/预算/超时/降级）在
        # ToolExecutor 统一边界内完成；本层只取最终文本并记下轨迹。
        self._last_tool_trace = []
        result = await self._tool_executor.run(
            system=self._build_system_prompt(task.message),
            messages=task.messages,
        )
        self._last_tool_trace = list(result.tool_trace)
        if result.degraded:
            logger.warning(
                f"{self.agent_type.value} 工具回路降级: terminated={result.terminated} "
                f"error={result.error}"
            )
        text = result.text
        if not text and result.degraded:
            # 端点与无工具降级调用都失败（terminated=="error"）：与"未接工具执行器"时的
            # 异常路径保持一致——交由 handle() 走失败分支。否则降级文案会命中
            # _needs_escalation 的"转人工"等关键词，凭空触发升级并抑制隐患工单闸门
            # （2026-10 回归：test_hazard_tickets E2E 高危自动建单）。
            if result.terminated == "error" and result.error:
                raise RuntimeError(f"工具回路失败: {result.error}")
            # 有界预算/工具不可用但对话可继续：给出中性降级说明（不含升级关键词）
            text = "抱歉，本次未能取得可用回答，请稍后重试。"
        return text

    def _build_system_prompt(self, message: str) -> str:
        """把动态加载的 Skills 拼入 system prompt，让业务规则随请求生效。

        末尾再追加可选的"作答风格"约束（`SAFETYMIND_ANSWER_STYLE=brief`）：
        延迟瓶颈在生成侧，缩短输出是唯一不换模型的杠杆，但要由 A/B 定价（见
        benchmarks/eval_answer_length_ab.py）。默认不追加，行为不变。
        """
        from core.llm_params import answer_style_suffix
        base = self.system_prompt
        if self._skill_manager is not None:
            skill_prompt = self._skill_manager.prompt_for(message, self.agent_type.value)
            if skill_prompt:
                base = f"{base}\n\n[动态 Skills]\n{skill_prompt}"
        return base + answer_style_suffix()

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


# ── 员工层（有记忆的编排者）───────────────────────────────────────────────────

class EmployeeAgent:
    """
    一线员工侧的编排者（OCTO 式解耦的唯一有状态编排层）。

    职责：
      1. 上下文装配 —— 读取 MemoryContext（会话摘要/相关历史/人员安全画像/
         最近对话）与 RAG 知识块，装配成一次性的 ExpertTask 任务包；
         无结构化记忆时退回调用方传入的文本 context（评测/CLI 链路行为不变）。
      2. 路由决策 —— CRITICAL 模板短路（先于澄清，AGENTS.md 铁律 3）→
         低置信澄清 → 领域评分选主/辅专家 → 派发执行（单/并行）→ 升级检查。

    领域专家（BaseAgent 子类）无状态：从池中按 routing_score 选优，
    失败自动降级到 General 专家。
    """

    def __init__(self, pool: Dict[AgentType, List[BaseAgent]]):
        self._pool = pool   # 与门面共享同一份专家池（统计/惩罚回路读写同源）

    # ── 上下文装配（员工层职责之一）──────────────────────────────────────────

    @staticmethod
    def _clean(s: str) -> str:
        return s.encode("utf-8", errors="ignore").decode("utf-8")

    def _memory_block(self, req: Request) -> str:
        """读取记忆：优先结构化 MemoryContext（员工代表用户读过记忆再说话）；
        渲染失败降级回文本 context，绝不阻塞对话。"""
        if req.memory_context is not None:
            try:
                return req.memory_context.to_prompt_text() or ""
            except Exception as ex:
                logger.warning(f"请求 {req.request_id} MemoryContext 渲染失败，退回文本上下文: {ex}")
        return req.context or ""

    def assemble_task(self, req: Request) -> ExpertTask:
        """统一装配任务包：记忆块 + 知识块 → [背景信息]；实体 → [结构化实体]。

        消息序列一次装配、全部（并行）专家共享——与解耦前每个专家各自
        拼装的内容逐字节一致（等价重构）。
        """
        parts = []
        memory_block = self._memory_block(req)
        if memory_block:
            parts.append(memory_block)
        if req.knowledge:
            parts.append(req.knowledge)
        context = "\n\n".join(part for part in parts if part)

        messages = []
        if context:
            messages.append({"role": "user", "content": f"[背景信息]\n{self._clean(context)}"})
            messages.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
        if req.entities:
            entities_text = json.dumps(req.entities, ensure_ascii=False)
            messages.append({"role": "user", "content": f"[结构化实体]\n{self._clean(entities_text)}"})
            messages.append({"role": "assistant", "content": "好的，我会结合这些结构化实体处理。"})
        messages.append({"role": "user", "content": self._clean(req.message)})

        return ExpertTask(
            request_id=req.request_id,
            message=req.message,
            context=context,
            entities=dict(req.entities or {}),
            messages=messages,
        )

    # ── 主入口 ────────────────────────────────────────────────────────────────

    # ── 领域证据切片（真正的上下文隔离，2026-10-26）──────────────────────────
    #
    # 原实现缺陷（实测证实）：assemble_task 只调用一次，run_parallel 把同一份
    # messages 传给全部专家——所谓"隔离上下文"并不存在，实际是
    # 「同上下文 + 不同 system_prompt」。专家看到全量记忆与全量知识，
    # 等于三个不同人设读同一份材料，协作价值有限，还各自消耗完整上下文 token。
    #
    # 本实现按领域把"背景信息"切成相关切片：设备专家只看设备相关的知识/记忆，
    # 合规专家只看法规/作业票相关的。**用户原始问题始终完整保留**（否则专家
    # 会答错题意），切分的只是背景材料。
    #
    # 保守边界：
    #   - 切片按段落（\n\n）打分，段落不足或全不相关时退回完整背景
    #     （宁可多给一点，也不能让专家因为看不到关键材料而答偏）
    #   - SAFETYMIND_EXPERT_ISOLATION=0 可一键回退旧行为（同上下文）
    _EQUIP_DOMAIN_KW = ("设备", "报警", "联锁", "超温", "超压", "振动", "泄漏", "泵",
                        "阀", "管道", "反应釜", "反应器", "压缩机", "停机", "检修",
                        "仪表", "工艺", "温度", "压力", "液位")
    _COMPLY_DOMAIN_KW = ("法规", "标准", "作业票", "动火", "受限空间", "许可", "审批",
                         "危化品", "化学品", "MSDS", "应急", "预案", "事故", "上报",
                         "培训", "资质", "职业健康", "隐患", "台账", "检查")
    _GENERAL_DOMAIN_KW = ("隐患", "排查", "整改", "防护", "劳保", "规程", "制度",
                          "风险", "辨识", "咨询")

    @classmethod
    def _domain_keywords(cls, agent_type: AgentType):
        if agent_type == AgentType.EQUIPMENT:
            return cls._EQUIP_DOMAIN_KW
        if agent_type == AgentType.COMPLIANCE:
            return cls._COMPLY_DOMAIN_KW
        return cls._GENERAL_DOMAIN_KW

    @classmethod
    def _slice_context_for(cls, background: str, agent_type: AgentType,
                           keep_ratio: float = 0.6) -> str:
        """把背景材料切成与该领域相关的段落；不足时退回完整背景。

        这是"真隔离"的核心：不同专家拿到**不同的背景材料**，而不是同一份。
        """
        if not background:
            return ""
        paras = [p for p in background.split("\n\n") if p.strip()]
        if len(paras) <= 2:
            return background           # 段落太少，切了反而丢信息
        kws = cls._domain_keywords(agent_type)
        scored = [(sum(1 for kw in kws if kw in p), p) for p in paras]
        if sum(h for h, _ in scored) == 0:
            return background           # 全不相关：可能材料本身中性，不切
        keep = max(1, int(len(paras) * keep_ratio))
        idx_sorted = sorted(range(len(paras)), key=lambda i: -scored[i][0])[:keep]
        idx_keep = sorted(idx_sorted)
        sliced = "\n\n".join(paras[i] for i in idx_keep)
        return sliced if sliced.strip() else background

    def assemble_task_for(self, req: Request, agent_type: AgentType) -> ExpertTask:
        """按领域装配任务包（真正的上下文隔离）。

        与 assemble_task 的区别：背景信息按该领域相关性切片，
        用户原始问题与结构化实体保持完整。
        """
        parts = []
        background = "\n\n".join(p for p in [self._memory_block(req), req.knowledge] if p)
        if os.getenv("SAFETYMIND_EXPERT_ISOLATION", "1") == "1":
            background = self._slice_context_for(background, agent_type)
        if background:
            parts.append(background)
        context = "\n\n".join(part for part in parts if part)

        messages = []
        if context:
            messages.append({"role": "user", "content": f"[背景信息]\n{self._clean(context)}"})
            messages.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
        if req.entities:
            entities_text = json.dumps(req.entities, ensure_ascii=False)
            messages.append({"role": "user", "content": f"[结构化实体]\n{self._clean(entities_text)}"})
            messages.append({"role": "assistant", "content": "好的，我会结合这些结构化实体处理。"})
        messages.append({"role": "user", "content": self._clean(req.message)})

        return ExpertTask(
            request_id=req.request_id,
            message=req.message,
            context=context,
            entities=dict(req.entities or {}),
            messages=messages,
        )

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

    async def handle(self, req: Request, t0: Optional[float] = None) -> OrchestratorResult:
        """
        员工层处理一次请求：路由决策 → 装配任务包 → 派发专家 → 升级检查。
        t0 由门面（AgentOrchestrator.run）传入，保证 latency_ms 口径与
        解耦前一致（含意图识别耗时）。
        """
        t0 = t0 or time.monotonic()

        # CRITICAL 紧急事态：模板化应急响应（零 LLM 依赖）。
        # 设计依据：紧急情况的秒级确定性响应优于生成式回答（更快、无幻觉、
        # 端点故障时安全关键路径在线）；SAFETYMIND_CRITICAL_TEMPLATE=0 可关闭。
        # 注意：必须先于澄清追问——紧急度独立于意图置信度（如"被困""救命"是
        # CRITICAL 关键词但不在意图 pattern 词表，LLM 死时意图=OTHER 低置信），
        # 紧急消息绝不能被澄清追问拦截。
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
                escalation_basis="urgency",
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.ESCALATION],
                primary_agent=AgentType.ESCALATION,
                routing_reason="CRITICAL 紧急事态，模板化应急响应（零 LLM 依赖）",
                routing_confidence=1.0,
            )

        # ── 紧急安全网（2026-10-28 并发轴实测新增）─────────────────────────
        # 触发缺陷：负载下意图识别退化（LLM 路径超时→BGE 兜底），urgency 不再是
        # CRITICAL，CRITICAL 模板因此被跳过；随后工具回路预算耗尽，用户拿到的
        # 是"工具调用未在预算内完成，未获得可用的最终回答"——**紧急问句没有应急步骤**，
        # 安全底线被击穿（并发实测 5/32）。
        # 修法：用本地零成本预检做第二道网（与 /chat 入口同一份规则，已含
        # 泄漏×伤亡组合）。命中即模板化，**绝不让紧急问句落到工具预算文案**。
        # 与入口分流的关系：入口分流负责"快"，这里是"兜底正确"——两者同源规则，
        # 入口超时/被绕开时仍能保证应急步骤。SAFETYMIND_EMERGENCY_SAFETY_NET=0 可关。
        if (os.getenv("SAFETYMIND_CRITICAL_TEMPLATE", "1") == "1"
                and os.getenv("SAFETYMIND_EMERGENCY_SAFETY_NET", "1") == "1"):
            try:
                from core import emergency as _emg
                _sig = _emg.precheck(req.message)
            except Exception:
                _sig = None
            if _sig is not None:
                template = self.CRITICAL_TEMPLATES.get(req.intent, self.CRITICAL_GENERIC)
                logger.warning(
                    f"请求 {req.request_id} 紧急安全网命中（{_sig.get('reason', '')[:60]}），"
                    f"模板化响应（零 LLM 依赖，原 urgency={req.urgency}）")
                return OrchestratorResult(
                    request_id=req.request_id,
                    response=template,
                    agent_type=AgentType.ESCALATION,
                    intent=req.intent,
                    escalated=True,
                    escalation_basis="safety_net",
                    latency_ms=(time.monotonic() - t0) * 1000,
                    agent_types=[AgentType.ESCALATION],
                    primary_agent=AgentType.ESCALATION,
                    routing_reason="紧急安全网命中，模板化应急响应（零 LLM 依赖）",
                    routing_confidence=1.0,
                )

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

        # 复杂问题自动并行协作，例如同一句同时涉及设备报警和作业票办理。
        decision = self._route_decision(req)
        if decision.multi_agent:
            return await self.run_parallel(req, decision)

        # 执行主专家（员工层装配任务包，含降级）
        task = self.assemble_task(req)
        response = await self._execute(task, decision.primary_agent)

        # 升级检查（依据分类见 escalation_basis 的注释：确定性部分 vs 模型建议）
        _basis = escalation_basis(req.urgency, req.intent, bool(response.escalate))
        escalated = bool(_basis)
        if escalated:
            logger.warning(f"请求 {req.request_id} 触发升级: basis={_basis} urgency={req.urgency}")
            # 生产环境：此处创建工单、通知安全值班/应急指挥

        return OrchestratorResult(
            request_id=req.request_id,
            response=response.content,
            agent_type=response.agent_type,
            intent=req.intent,
            escalated=escalated,
            escalation_basis=_basis,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[response.agent_type],
            primary_agent=decision.primary_agent,
            supporting_agents=[],
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            tool_trace=list(response.tool_trace),
        )

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        """
        并行派发给多个专家，合并结果。
        适用于复杂问题（如同时涉及设备处置和作业票合规）。
        任务包由员工层装配一次，全部并行专家共享。
        """
        t0 = time.monotonic()
        agent_types = decision.agent_types
        # 上下文隔离（2026-10-26）：每个专家按自己领域装配任务包，
        # 不再共享同一份全量上下文。SAFETYMIND_EXPERT_ISOLATION=0 回退旧行为。
        if os.getenv("SAFETYMIND_EXPERT_ISOLATION", "1") == "1":
            tasks = [self._execute(self.assemble_task_for(req, at), at) for at in agent_types]
        else:
            task = self.assemble_task(req)
            tasks = [self._execute(task, at) for at in agent_types]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并：主 Agent 在前，辅助 Agent 在后。
        parts = []
        # P0-A：汇总主/辅专家各自的工具调用轨迹（主在前、辅在后），无调用时为空列表。
        tool_trace: List[Any] = []
        for r in responses:
            if isinstance(r, AgentResponse) and r.success:
                role = "主处理" if r.agent_type == decision.primary_agent else "辅助处理"
                parts.append(f"[{r.agent_type.value} - {role}]\n{r.content}")
            if isinstance(r, AgentResponse):
                tool_trace.extend(r.tool_trace or [])

        combined = "\n\n".join(parts) if parts else "抱歉，所有 Agent 均处理失败。"
        _hint = any(isinstance(r, AgentResponse) and r.escalate for r in responses)
        _basis = escalation_basis(req.urgency, req.intent, _hint)
        escalated = bool(_basis)

        return OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            agent_type=decision.primary_agent,
            intent=req.intent,
            escalated=escalated,
            escalation_basis=_basis,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[
                r.agent_type for r in responses
                if isinstance(r, AgentResponse) and r.success
            ] or agent_types,
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            tool_trace=tool_trace,
        )

    # ── 路由逻辑 ──────────────────────────────────────────────────────────────

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
        # 辅助专家判定（2026-10-26 重做，修复"多 Agent 永不触发"的设计缺陷）。
        #
        # 原实现：score >= 0.45 and score >= primary_score * 0.55
        # 缺陷：意图命中直接给 0.75，关键词最多再加 0.45，所以主专家分数 ~1.2，
        #       辅助专家上限 0.45，永远满足不了 0.45 >= 1.2*0.55=0.66。
        #       实测 12 条复合问题触发率 0%——并行多 Agent 等于死代码。
        #
        # 新实现：改用「独立跨域证据」而非分数比例。理由是分数比例本身就不该
        # 用来决定协作：一个复合问题里两个领域各自有强证据，不该因为某个领域的
        # 意图置信度更高就把另一个领域整体丢弃。判定条件（三条同时满足）：
        #   1) 该领域不是 GENERAL（GENERAL 是兜底，不构成"专家协作"）
        #   2) 该领域有**实质证据**：意图专属分 >= 0.75 或 关键词命中 >= 2 个
        #      （单个关键词命中太容易误触发，实测 1 个词就协作会过度并行）
        #   3) 该领域与主专家不同
        # 另外：辅助专家数量上限 2，避免 3 个专家并行把延迟推高（每次并行都是
        # 一次完整 LLM 调用，2 个已经让延迟翻倍；这是延迟与完整性的显式权衡）。
        supporting_agents = []
        for agent_type, score in ordered[1:]:
            if agent_type == AgentType.GENERAL or agent_type == primary_agent:
                continue
            if self._has_cross_domain_evidence(req, agent_type):
                supporting_agents.append(agent_type)
            if len(supporting_agents) >= 2:
                break

        reason = self._routing_reason(req, available_scores, primary_agent, supporting_agents)
        return RoutingDecision(
            primary_agent=primary_agent,
            supporting_agents=supporting_agents,
            reason=reason,
            confidence=round(min(primary_score, 1.0), 3),
        )

    # 各领域的关键词表（与 _domain_scores 共用，避免两处漂移）
    _EQUIPMENT_KWS = (
        "报警", "超温", "超压", "超液位", "联锁", "esd", "sis", "dcs", "仪表",
        "泵", "压缩机", "反应器", "反应釜", "换热器", "阀门", "管道", "振动",
        "腐蚀", "泄漏", "故障", "停机", "检修", "检维修", "液位", "温度", "压力",
    )
    _COMPLIANCE_KWS = (
        "作业票", "动火", "受限空间", "高处作业", "吊装", "临时用电", "断路",
        "许可证", "审批", "危化品", "危险化学品", "重大危险源", "事故", "上报",
        "应急", "预案", "法规", "标准", "安全生产法", "处罚", "考核", "培训",
        "特种作业", "职业健康", "职业病", "体检", "工伤", "化学品",
    )
    # 强域标志词：单个出现就足以证明"这个问题涉及该领域"，不需要凑够 2 个。
    # 理由是这些词在安全生产语境里几乎没有歧义（"动火"一定是作业票范畴，
    # "超温"一定是设备范畴），而 "泄漏" 这类词可能只是描述背景。
    _EQUIPMENT_STRONG = ("联锁", "超温", "超压", "跳车", "停机", "振动", "反应釜",
                        "反应器", "压缩机", "换热器", "esd", "sis")
    _COMPLIANCE_STRONG = ("动火", "受限空间", "作业票", "高处作业", "吊装", "许可证",
                          "临时用电", "断路", "重大危险源", "特种作业")

    def _has_cross_domain_evidence(self, req: Request, agent_type: AgentType) -> bool:
        """该领域是否有**实质**跨域证据（用于决定是否并行协作）。

        判据分三档，刻意保守（过度并行会让每次请求都多做一次完整 LLM 调用）：
          1) 意图本身就是该领域 → 是
          2) 命中 1 个**强域标志词**（"动火""联锁"这类无歧义词）→ 是
          3) 命中 >=2 个普通关键词 → 是
        单命中的一个普通词（如只说"泄漏"）不足以再拉一个专家进来。
        """
        msg = req.message.lower()
        intent = req.intent
        if agent_type == AgentType.EQUIPMENT:
            if intent in (IntentCategory.EQUIPMENT_ALARM, IntentCategory.EQUIPMENT_MAINT):
                return True
            if any(kw in msg for kw in self._EQUIPMENT_STRONG):
                return True
            return sum(1 for kw in self._EQUIPMENT_KWS if kw in msg) >= 2
        if agent_type == AgentType.COMPLIANCE:
            if intent in (IntentCategory.HAZARD_REPORT, IntentCategory.WORK_PERMIT,
                          IntentCategory.INCIDENT_REPORT, IntentCategory.CHEMICAL_SAFETY,
                          IntentCategory.TRAINING_CERT, IntentCategory.INSPECTION_AUDIT,
                          IntentCategory.OCC_HEALTH):
                return True
            if any(kw in msg for kw in self._COMPLIANCE_STRONG):
                return True
            return sum(1 for kw in self._COMPLIANCE_KWS if kw in msg) >= 2
        return False

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

    async def _execute(self, task: ExpertTask, agent_type: AgentType) -> AgentResponse:
        """执行专家，失败时降级到 GeneralAgent。"""
        agent = self._best_agent(agent_type)
        if agent is None:
            agent = self._best_agent(AgentType.GENERAL)
        if agent is None:
            return AgentResponse(
                agent_type=AgentType.GENERAL,
                content="服务暂时不可用，请稍后重试。",
                success=False,
            )

        response = await agent.handle(task)

        # 专属 Agent 失败时降级到 GeneralAgent
        if not response.success and agent_type != AgentType.GENERAL:
            logger.warning(f"{agent_type.value} 失败，降级到 GeneralAgent")
            fallback = self._best_agent(AgentType.GENERAL)
            if fallback:
                response = await fallback.handle(task)

        return response


# ── 编排门面（对外契约入口）───────────────────────────────────────────────────

class AgentOrchestrator:
    """
    多 Agent 编排门面。

    持有专家池与意图识别器，把请求交给员工层（EmployeeAgent）完成
    上下文装配与路由。对外 run()/run_parallel()/recognize_intent()/
    get_stats()/update_routing_penalties() 的签名与返回结构与解耦前一致，
    api 层（_chat_core）与评测器（evaluation/evaluator.py 直调 run()）
    不受影响。
    """

    def __init__(
        self,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        skill_manager: Optional[Any] = None,
        tool_executor: Optional[Any] = None,
    ):
        from core.llm_utils import llm_client_kwargs, make_llm_client
        client = make_llm_client(api_key=api_key, base_url=base_url or "",
                                 model=model, **llm_client_kwargs())

        # 意图识别器用**可分离的模型**（2026-10-29 实测）：意图是 19 分类 + 小 JSON 任务，
        # 轻量模型同样准但快 6 倍（test2 n=150：0.980 vs 0.980，回落 14652ms → 2347ms），
        # 而它覆盖约 42.7% 请求。此前 api/main.py 的分离只作用在"对外暴露给 Evaluator"
        # 的那个实例上，真正跑 /chat 的是这里——两处必须用同一个选择函数，否则改了个寂寞。
        from core.llm_params import intent_model_name
        self._intent_recognizer = IntentRecognizer(api_key=api_key, base_url=base_url,
                                                   model=intent_model_name(model))
        self._skill_manager = skill_manager
        # P0-A：可选的工具执行器（None = 旧行为）。四个专家共享同一个实例，
        # 由统一边界控权限与预算，避免主辅专家各写一遍执行逻辑。
        self._tool_executor = tool_executor

        # 专家池：每种类型可有多个实例（水平扩展）；员工层与门面共享同一份
        self._pool: Dict[AgentType, List[BaseAgent]] = {
            AgentType.GENERAL:    [SafetyCoordinationAgent(client, model, skill_manager, tool_executor)],
            AgentType.EQUIPMENT:  [EquipmentSafetyAgent(client, model, skill_manager, tool_executor)],
            AgentType.COMPLIANCE: [ComplianceEmergencyAgent(client, model, skill_manager, tool_executor)],
            AgentType.ESCALATION: [EscalationAgent(client, model, skill_manager, tool_executor)],
        }
        self._employee = EmployeeAgent(self._pool)

    def set_skill_manager(self, skill_manager: Optional[Any]) -> None:
        """更新 SkillManager 引用，供运行时重载或测试替换使用。"""
        self._skill_manager = skill_manager
        for agents in self._pool.values():
            for agent in agents:
                agent._skill_manager = skill_manager

    def set_tool_executor(self, tool_executor: Optional[Any]) -> None:
        """更新 ToolExecutor 引用，供运行时替换或测试使用（与 set_skill_manager 同款）。"""
        self._tool_executor = tool_executor
        for agents in self._pool.values():
            for agent in agents:
                agent._tool_executor = tool_executor

    async def recognize_intent(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ):
        """对外暴露意图识别，供 API 层先判断是否需要 RAG 等前置能力。"""
        return await self._intent_recognizer.recognize(message, history=history)

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def run(self, req: Request) -> OrchestratorResult:
        """
        处理一次请求的完整流程：
          意图识别（未识别时补全）→ 员工层装配任务包 + 路由决策 → 执行 → 结果
        """
        t0 = time.monotonic()

        # 意图识别（如果调用方已识别则跳过）
        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent  = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence

        # t0 传入员工层：latency_ms 口径不变（含意图识别耗时）
        return await self._employee.handle(req, t0=t0)

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        """并行派发给多个专家，合并结果（员工层实现，门面转发保持原签名）。"""
        return await self._employee.run_parallel(req, decision)

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
                    # additive：让 Monitor/看板能区分"在途"与"失败"（旧消费方不受影响）
                    "completed":    agent.stats.completed,
                    "failed":       agent.stats.failed,
                    "in_flight":    agent.stats.in_flight,
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
