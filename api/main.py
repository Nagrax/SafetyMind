
import asyncio
import json
import logging
import os
import pathlib
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional


_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Header, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BANNER = r"""
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
   ╔══════════════════════╗
   ║   SafetyMindv2.0     ║
   ║   智能安全 AI 系统    ║
   ╚══════════════════════╝
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
"""

# ── 全局组件（lifespan 中初始化）─────────────────────────────────────────────
_orchestrator = None
_memory       = None
_tool_manager = None
_monitor      = None
_evaluator    = None
_skill_manager = None
_conv_store   = None
_audit        = None

def _anthropic_cfg() -> Dict[str, Any]:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    cfg: Dict[str, Any] = {
        "api_key":  key,
        "model":    os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip(),
    }
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _monitor, _evaluator, _skill_manager, _conv_store, _audit

    print(BANNER, flush=True)

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from core.intent_recognizer import IntentRecognizer
    from evaluation.evaluator import EndToEndEvaluator
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from core.skill_loader import SkillManager
    from memory.conversation_store import ConversationStore
    from core.audit_store import AuditStore

    cfg = _anthropic_cfg()
    logger.info(f"模型: {cfg['model']}  base_url: {cfg.get('base_url', '(官方)')}")

    # 意图识别器（Orchestrator 内部也会创建，这里单独暴露给 Evaluator）
    recognizer = IntentRecognizer(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # Skills：启动时从目录加载业务能力说明，并在 Agent 调用 LLM 时动态注入。
    skills_dir = os.getenv("SAFETYMIND_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(
        root_dir=skills_dir,
        max_prompt_chars=int(os.getenv("SAFETYMIND_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    _skill_manager.load()

    # Agent 编排器
    _orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
    )

    # 记忆管理器（Redis 工作记忆 + ChromaDB 情景记忆/用户画像）
    _memory = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "./data/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # MCP 工具管理器 + RAG 知识库（基于 ChromaDB 的真实检索）
    _tool_manager = MCPToolManager(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )
    kb = KnowledgeBase(
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        # 知识库与对话记忆分目录存储：一边损坏不连坐另一边，也更利于单独重建
        chroma_path=os.getenv("CHROMA_KB_PERSIST_DIRECTORY", "./data/kb_chroma"),
    )
    logger.info(f"知识库已加载: {await kb.doc_count_async()} 个文档片段")

    def knowledge_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
        query = params.get("query", "")
        return [{
            "title": "知识库降级结果",
            "content": f"知识库暂时不可用，未能完成对“{query}”的法规制度检索。请稍后重试，或联系安全值班人员确认。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    _tool_manager.register(Tool(
        name="knowledge_search",
        description="搜索知识库（基于 ChromaDB 向量检索）",
        handler=kb.search_handler,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
        cache_ttl=300.0,
        supports_rerank=True,
        fallback=knowledge_fallback,
    ))

    # 性能监控（可选启动 Prometheus）
    prom_port = int(os.getenv("PROMETHEUS_PORT", "0")) or None
    _monitor = PerformanceMonitor(
        orchestrator=_orchestrator,
        tool_manager=_tool_manager,
        interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        prometheus_port=prom_port,
    )
    await _monitor.start()

    # 评测器
    _evaluator = EndToEndEvaluator(
        orchestrator=_orchestrator,
        recognizer=recognizer,
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        baseline_path=os.getenv("EVAL_BASELINE_PATH", "/app/data/eval/baseline.json"),
    )

    # 会话历史持久化（跨天可恢复；写入失败不阻断对话）
    _conv_store = ConversationStore()

    # 审计/升级/工单存储（SQLite；AUDIT_ENABLED=0 可整体关闭）
    _audit = None
    if os.getenv("AUDIT_ENABLED", "1") == "1":
        try:
            _audit = AuditStore()
            logger.info(f"审计存储已启用: {os.getenv('AUDIT_DB_PATH', 'data/audit.db')}")
        except Exception as ex:
            logger.warning(f"审计存储初始化失败（对话不受影响）: {ex}")

    # 意图引擎后台预热：启用 SAFETYMIND_BGE=1 时，首条消息不再等本地模型冷加载
    # （冷加载约 20-40s，预热后意图阶段毫秒级直出）。
    if os.getenv("SAFETYMIND_BGE", "") == "1":
        asyncio.create_task(_orchestrator.recognize_intent("系统预热"))

    logger.info("SafetyMind 已就绪")
    yield

    await _monitor.stop()
    if _memory is not None:
        await _memory.close()
    logger.info("SafetyMind 已关闭")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="SafetyMind 安全生产智能助手",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 公网访问门（可选）：.env 设置 ACCESS_TOKEN 后，业务端点要求 X-Access-Token 头。
#    公开项：前端静态资源、健康检查、用户端配置、API 文档。
#    管理端点另有 ADMIN_TOKEN 双重门。留空 = 不启用（本地单人形态）。
_PUBLIC_EXACT = {"/", "/health", "/config", "/metrics", "/monitor",
                 "/docs", "/redoc", "/openapi.json", "/favicon.ico"}
_PUBLIC_PREFIX = ("/assets/", "/docs/", "/api/python/health", "/api/python/config",
                  "/api/python/docs", "/api/python/openapi.json")


@app.middleware("http")
async def access_gate(request: Request, call_next):
    token = os.getenv("ACCESS_TOKEN", "").strip()
    if token:
        path = request.url.path
        is_public = (path in _PUBLIC_EXACT
                     or path.startswith(_PUBLIC_PREFIX)
                     or request.method == "OPTIONS")
        if not is_public:
            supplied = request.headers.get("X-Access-Token", "")
            if supplied != token:
                return JSONResponse({"detail": "需要访问令牌（X-Access-Token）"}, status_code=401)
    return await call_next(request)


class AccessTokenInput(BaseModel):
    value: str


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message:     str
    user_id:     str = "anonymous"
    conv_id:     Optional[str] = None


class ChatResponse(BaseModel):
    conv_id:     str
    response:    str
    intent:      str
    intent_group: str = "other"
    agent_type:  str
    agent_types: List[str] = Field(default_factory=list)
    primary_agent: str = ""
    supporting_agents: List[str] = Field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    escalated:   bool
    latency_ms:  float
    knowledge_used: bool = False
    entities: Dict[str, List[str]] = Field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_source_scores: Dict[str, float] = Field(default_factory=dict)
    request_id:  str = ""   # 升级留痕编号（用户端"复制上报编号"用）


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/config")
async def app_config():
    """用户端展示配置（转人工值班电话等）。无敏感信息，可公开。"""
    return {
        "escalation_phone": os.getenv("ESCALATION_PHONE", "").strip(),
        "product": "SafetyMind",
    }


# ── 会话历史（跨天可恢复）───────────────────────────────────────────────────
@app.get("/conversations", tags=["会话历史"])
async def list_conversations(user_id: str = "anonymous"):
    """当前用户的历史会话列表（按最近更新倒序）。"""
    if _conv_store is None:
        raise HTTPException(503, "会话存储未初始化")
    return {"conversations": _conv_store.list_conversations(user_id)}


@app.get("/conversations/{conv_id}/messages", tags=["会话历史"])
async def get_conversation_messages(conv_id: str, user_id: str = "anonymous"):
    """读取某历史会话的完整消息（用于恢复继续对话）。"""
    if _conv_store is None:
        raise HTTPException(503, "会话存储未初始化")
    return {"conv_id": conv_id, "messages": _conv_store.get_messages(user_id, conv_id)}


@app.delete("/conversations/{conv_id}", tags=["会话历史"])
async def delete_conversation(conv_id: str, user_id: str = "anonymous"):
    if _conv_store is None:
        raise HTTPException(503, "会话存储未初始化")
    return {"deleted": _conv_store.delete_conversation(user_id, conv_id)}


# ── 审计 / 升级 / 工单（管理端点）────────────────────────────────────────────
def _admin_auth_required() -> bool:
    """管理密码何时必须：
    - .env 显式设 SAFETYMIND_DEPLOYED=1（服务器反代部署）→ 必须，即使 uvicorn 只绑回环
      （公网经反代可达，鉴权判定不能被绑定地址欺骗）；
    - 否则按 API_HOST 判定：非回环 = 网络暴露 = 必须；回环（桌面/手机窗口）= 免密，
      兑现"本机自用无需密码"，也避免 bootstrap 与输入互相堵死的死锁。"""
    if os.getenv("SAFETYMIND_DEPLOYED", "").strip() == "1":
        return True
    host = os.getenv("API_HOST", "0.0.0.0").strip().lower()  # 与 uvicorn 绑定默认值一致：未声明视为网络暴露
    return host not in ("127.0.0.1", "localhost", "::1")


def require_admin(x_admin_token: str = Header(default="", alias="X-Admin-Token")):
    """管理端点鉴权：部署形态（API_HOST 非回环）要求 .env 配置 ADMIN_TOKEN 并以
    请求头 X-Admin-Token 访问；未配置 = 锁定（防裸奔上线）。
    本地单人形态自动放行——管理功能保护的是"多人在网络上访问"的场景。"""
    if not _admin_auth_required():
        return
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            503, "服务以网络模式运行但未设置管理密码：请在 .env 配置 ADMIN_TOKEN（首次可在设置页一键生成）")
    if x_admin_token != expected:
        raise HTTPException(401, "管理密码无效")


@app.get("/audit", tags=["审计"])
async def audit_list(limit: int = 100, escalated_only: bool = False, _admin: None = Depends(require_admin)):
    if _audit is None:
        raise HTTPException(503, "审计存储未启用（AUDIT_ENABLED=0）")
    return {"entries": _audit.list_audit(limit=limit, escalated_only=escalated_only)}


@app.get("/audit/stats", tags=["审计"])
async def audit_stats(_admin: None = Depends(require_admin)):
    """安环部周报数据源：请求数/升级数/未确认升级/工单闭环与超期。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用（AUDIT_ENABLED=0）")
    return _audit.stats()


@app.get("/upgrades", tags=["审计"])
async def upgrades_list(limit: int = 50, unacked_only: bool = False, _admin: None = Depends(require_admin)):
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    return {"upgrades": _audit.list_upgrades(limit=limit, unacked_only=unacked_only)}


@app.post("/upgrades/{upgrade_id}/ack", tags=["审计"])
async def upgrade_ack(upgrade_id: int, _admin: None = Depends(require_admin)):
    """值班确认：收到并处理了该次升级。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    return {"acked": _audit.ack_upgrade(upgrade_id)}


class TicketInput(BaseModel):
    title: str
    type: str = "hazard"          # hazard | emergency | permit | maintenance
    priority: str = "normal"      # low | normal | high | critical
    assignee: str = ""
    due_hours: Optional[float] = None   # 相对当前时间的限期（小时）


@app.get("/tickets", tags=["审计"])
async def tickets_list(status: Optional[str] = None, limit: int = 100,
                       _admin: None = Depends(require_admin)):
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    return {"tickets": _audit.list_tickets(status=status, limit=limit)}


@app.post("/tickets", tags=["审计"])
async def ticket_create(body: TicketInput, _admin: None = Depends(require_admin)):
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    due = (time.time() + body.due_hours * 3600) if body.due_hours else None
    tid = _audit.create_ticket(body.title, type_=body.type, priority=body.priority,
                               assignee=body.assignee, due_ts=due)
    return {"ticket_id": tid}


@app.post("/tickets/{ticket_id}/close", tags=["审计"])
async def ticket_close(ticket_id: int, _admin: None = Depends(require_admin)):
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    return {"closed": _audit.close_ticket(ticket_id)}


# ── .env 可视化配置（管理端点；白名单 + 掩码 + 原子写）───────────────────────
@app.post("/config/admin/bootstrap", tags=["审计"])
async def admin_config_bootstrap():
    """首次设置：ADMIN_TOKEN 未配置时一键生成管理密码（仅未配置时可用，先到先得）。
    已配置后此端点永久失效——改密码必须凭现有密码走 /config/admin。"""
    from core.env_config import EnvConfigManager
    _h = os.getenv("API_HOST", "0.0.0.0").strip().lower()
    if _h not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(403, "网络部署形态禁止远程 bootstrap：请管理员在服务器 .env 预设 ADMIN_TOKEN 后重启")
    if os.getenv("ADMIN_TOKEN", "").strip():
        raise HTTPException(409, "管理密码已配置；修改需凭现有密码在设置页操作")
    import secrets
    token = "sm-" + secrets.token_urlsafe(12)
    mgr = EnvConfigManager(os.getenv("ENV_FILE_PATH", os.path.join(_ROOT, ".env")))
    mgr.write_config({"ADMIN_TOKEN": token})
    return {"admin_token": token, "hint": "请妥善保存，此窗口关闭后不再显示"}


@app.get("/config/admin", tags=["审计"])
async def admin_config_get(_admin: None = Depends(require_admin)):
    from core.env_config import EnvConfigManager
    mgr = EnvConfigManager(os.getenv("ENV_FILE_PATH", os.path.join(_ROOT, ".env")))
    host = os.getenv("API_HOST", "0.0.0.0").strip().lower()
    auth_mode = "local" if host in ("127.0.0.1", "localhost", "::1") else "network"
    return {"auth_mode": auth_mode, "config": mgr.read_config()}


@app.post("/config/admin", tags=["审计"])
async def admin_config_post(body: Dict[str, Any], _admin: None = Depends(require_admin)):
    from core.env_config import EnvConfigManager
    mgr = EnvConfigManager(os.getenv("ENV_FILE_PATH", os.path.join(_ROOT, ".env")))
    try:
        result = mgr.write_config(body.get("values") or {})
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    result["hint"] = ("以下配置需重启服务生效: " + ", ".join(result["restart_required"])) \
        if result["restart_required"] else "全部即时生效"
    return result


@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "agents": _orchestrator.get_stats()}


@app.get("/skills", tags=["Skills"])
async def skills_summary():
    """查看当前已加载的 Skills，便于确认热加载结果和排查解析错误。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"])
async def reload_skills():
    """运行时重新扫描 Skill 目录，不需要重启服务。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    主对话接口。完整流程：
      记忆读取 → 意图识别 → Agent 路由 → 执行 → 记忆写入
    """
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    from agents.agent_orchestrator import Request as OrcReq
    from memory.conversation_memory import MsgRole

    conv_id = req.conv_id or str(uuid.uuid4())

    # 1. 读取记忆上下文
    mem_ctx = await _memory.get_context(req.user_id, conv_id, query=req.message)

    # 2. 构建编排请求（含对话历史，用于意图识别上下文）
    history = [
        {"role": m.role.value, "content": m.content}
        for m in mem_ctx.recent_messages[-5:]
    ] if mem_ctx.recent_messages else None

    intent_result = await _orchestrator.recognize_intent(req.message, history=history)

    # RAG 门控（意图级 + 紧急度级）：寒暄/反馈/转人工/无关意图不检索；
    # CRITICAL 且应急模板开启时同样跳过 RAG——模板不使用知识库。
    # 注意与 SAFETYMIND_CRITICAL_TEMPLATE 联动：模板关闭时紧急消息仍需 RAG 上下文走 LLM。
    from core.intent_recognizer import UrgencyLevel
    knowledge_text, knowledge_used = "", False
    _template_on = os.getenv("SAFETYMIND_CRITICAL_TEMPLATE", "1") == "1"
    if not (_template_on and intent_result.urgency == UrgencyLevel.CRITICAL):
        knowledge_text, knowledge_used = await _build_knowledge_context(req.message, intent=intent_result.intent)
    context_parts = [mem_ctx.to_prompt_text()]
    if knowledge_text:
        context_parts.append(knowledge_text)
    full_context = "\n\n".join(part for part in context_parts if part)

    orch_req = OrcReq(
        message=req.message,
        user_id=req.user_id,
        conv_id=conv_id,
        context=full_context,
        history=history,
        entities=intent_result.entities,
        intent=intent_result.intent,
        intent_group=intent_result.intent_group,
        urgency=intent_result.urgency,
        intent_confidence=intent_result.confidence,
    )

    # 3. 执行
    result = await _orchestrator.run(orch_req)

    # 4. 写入记忆
    await _memory.add_message(req.user_id, conv_id, MsgRole.USER, req.message)
    await _memory.add_message(req.user_id, conv_id, MsgRole.ASSISTANT, result.response)

    # 5. 异步更新用户画像（不阻塞响应）
    asyncio.create_task(_memory.update_profile(req.user_id, conv_id))

    # 6. 会话历史落盘（跨天可恢复）+ 审计留痕 + CRITICAL 升级事件。
    #    必须在 return 之前：失败仅告警，绝不阻断响应。
    if _conv_store is not None:
        try:
            _conv_store.append_message(req.user_id, conv_id, "user", req.message)
            _conv_store.append_message(req.user_id, conv_id, "assistant", result.response,
                                       escalated=result.escalated, request_id=result.request_id)
        except Exception as ex:
            logger.warning(f"会话历史写入失败: {ex}")

    if _audit is not None:
        try:
            degraded = bool(getattr(intent_result, "degraded", False))
            _audit.audit_request(result.request_id, req.user_id, conv_id, req.message,
                                 intent_result.intent.value if intent_result.intent else "other",
                                 intent_result.urgency.value if intent_result.urgency else "low",
                                 result.escalated, result.latency_ms,
                                 degraded=degraded, knowledge_used=knowledge_used)
            if result.escalated:
                upgrade_id = _audit.add_upgrade(result.request_id, req.user_id, conv_id,
                                                req.message, intent_result.intent.value or "other")
                _audit.create_ticket(f"应急升级: {' '.join(req.message.split())[:60]}",
                                     type_="emergency", priority="critical",
                                     source_request_id=result.request_id)
                webhook = os.getenv("ESCALATION_WEBHOOK_URL", "").strip()
                if webhook:
                    msg_digest = " ".join(req.message.split())[:60]

                    async def _notify(upgrade_id=upgrade_id, webhook=webhook,
                                      rid=result.request_id, msg_digest=msg_digest):
                        from core.audit_store import notify_escalation_blocking
                        ok = await asyncio.to_thread(notify_escalation_blocking, webhook, {
                            "request_id": rid,
                            "text": f"【SafetyMind 应急升级】编号 {rid}\n内容: {msg_digest}\n时间: {time.strftime('%m-%d %H:%M:%S')}\n已自动建单并留痕，请值班确认。",
                        })
                        if ok:
                            _audit.mark_notified(upgrade_id, "webhook")

                    asyncio.create_task(_notify())
        except Exception as ex:
            logger.warning(f"审计/升级记录失败: {ex}")

    return ChatResponse(
        conv_id=conv_id,
        response=result.response,
        intent=result.intent.value if result.intent else "other",
        intent_group=intent_result.intent_group,
        agent_type=result.agent_type.value,
        agent_types=[agent_type.value for agent_type in result.agent_types],
        primary_agent=result.primary_agent.value if result.primary_agent else result.agent_type.value,
        supporting_agents=[agent_type.value for agent_type in result.supporting_agents],
        routing_reason=result.routing_reason,
        routing_confidence=result.routing_confidence,
        escalated=result.escalated,
        latency_ms=round(result.latency_ms, 1),
        knowledge_used=knowledge_used,
        entities=intent_result.entities,
        intent_confidence=round(intent_result.confidence, 4),
        intent_source_scores=intent_result.source_scores,
        request_id=result.request_id,
    )


async def _build_knowledge_context(message: str, intent=None, top_k: int = 3) -> tuple[str, bool]:
    """
    为 /chat 主链路构建 RAG 知识上下文。

    这里复用 MCPToolManager 的查询改写、并行召回、重排、fallback 能力。
    """
    if _tool_manager is None:
        return "", False
    if not _should_use_knowledge(message, intent=intent):
        return "", False
    try:
        result = await _tool_manager.search_with_rewrite("knowledge_search", message, top_k=top_k)
        if not result.success or not isinstance(result.data, list) or not result.data:
            return "", False

        parts = ["[知识库检索结果]"]
        used = False
        for i, item in enumerate(result.data[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "未命名文档"))
            content = str(item.get("content", "")).strip()
            score = item.get("score", "")
            if not content:
                continue
            used = True
            parts.append(f"{i}. 标题: {title}\n   相关度: {score}\n   内容: {content[:600]}")

        if not used:
            return "", False
        parts.append("请优先依据以上知识库内容回答；如果知识库内容不足，再结合安全生产通用知识说明，并提示需要核实。")
        return "\n".join(parts), True
    except Exception as ex:
        logger.warning(f"构建知识库上下文失败: {ex}")
        return "", False


def _should_use_knowledge(message: str, intent=None) -> bool:
    """跳过纯寒暄，安全业务类问题才检索知识库，避免无关 RAG 干扰回复。"""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    intent_value = getattr(intent, "value", intent)
    if intent_value in {"greeting", "feedback", "escalation", "other"}:
        return False
    if intent_value in {
        "general_consult", "hazard_inspection", "hazard_report",
        "equipment_alarm", "equipment_maintenance", "work_permit",
        "regulation_query", "emergency_response", "incident_report",
        "chemical_safety", "ppe_inquiry", "training_cert",
        "inspection_audit", "occupational_health", "safety_document",
    }:
        return True
    greetings = {"你好", "您好", "嗨", "hi", "hello", "hey", "早上好", "晚上好"}
    if msg in greetings:
        return False
    business_keywords = [
        "安全", "隐患", "作业票", "动火", "受限空间", "许可证", "审批", "危化品",
        "化学品", "报警", "超温", "超压", "泄漏", "事故", "上报", "应急", "预案",
        "法规", "标准", "培训", "取证", "特种作业", "防护", "劳保", "规程", "制度",
        "设备", "检维修", "检修", "职业健康", "职业病", "体检", "车间", "装置",
        "msds", "ppe", "gb", "sop",
    ]
    return len(msg) >= 4 or any(kw in msg for kw in business_keywords)


@app.get("/monitor")
async def monitor_summary():
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标入口。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/search")
async def search(query: str, top_k: int = 5):
    """
    演示检索优化链路：查询改写 → 并行召回 → 重排 → Top-K。
    展示 MCP 工具调用的核心亮点。含两次 LLM 调用，延迟为秒级。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.search_with_rewrite("knowledge_search", query, top_k=top_k)
    return {"query": query, "results": result.data, "reranked": result.reranked}


@app.post("/search/direct")
async def search_direct(query: str, top_k: int = 5):
    """
    直连向量检索（bge 嵌入 + 余弦）：跳过查询改写与 LLM 重排，几十毫秒返回。
    供前端"检索测试"使用——其语义是"验证文档能否被找到"，不需要完整优化链路。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    tool = _tool_manager._tools.get("knowledge_search")
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    results = await kb.search_async(query, top_k=top_k)
    return {"query": query, "results": results, "reranked": False}


class DocInput(BaseModel):
    """单篇文档输入。"""
    title:   str
    content: str


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: List[DocInput]


class EvalIntentInput(BaseModel):
    """意图识别评测用例。"""
    message: str
    expected_intent: str
    context: Optional[Dict[str, Any]] = None


class EvalDialogInput(BaseModel):
    """对话质量评测用例。question 单轮，turns 多轮。"""
    question: Optional[str] = None
    turns: Optional[List[str]] = None
    user_id: Optional[str] = None
    conv_id: Optional[str] = None


class EvalRunInput(BaseModel):
    """评测请求。为空时使用内置默认用例。"""
    intent_cases: Optional[List[EvalIntentInput]] = None
    dialog_cases: Optional[List[EvalDialogInput]] = None


@app.post("/knowledge/add", tags=["知识库"])
async def add_knowledge(body: BatchDocInput):
    """
    批量导入文档到知识库。

    文档会自动切片（每片 500 字）并存入 ChromaDB，ChromaDB 内置 Embedding 模型自动向量化。

    示例请求体：
    ```json
    {
      "documents": [
        {"title": "受限空间作业管理规定", "content": "作业前30分钟内取样分析，氧含量19.5%~21%..."},
        {"title": "动火作业分级要求", "content": "特殊动火不超过8小时，一级动火不超过24小时..."}
      ]
    }
    ```
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    count = await kb.add_documents_async([{"title": d.title, "content": d.content} for d in body.documents])
    total = await kb.doc_count_async()
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": total}


@app.post("/knowledge/upload", tags=["知识库"])
async def upload_knowledge(file: UploadFile = File(...)):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "..."}, ...]`

    文件大小限制：10MB
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")

    text = content.decode("utf-8", errors="ignore")
    filename = file.filename or "unknown"

    if filename.endswith(".json"):
        import json as _json
        try:
            docs = _json.loads(text)
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
    else:
        # txt / md：整个文件作为一篇文档
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": text}]

    count = await kb.add_documents_async(docs)
    total = await kb.doc_count_async()
    return {
        "message": f"文件 {filename} 导入成功",
        "added_chunks": count,
        "total_chunks": total,
    }


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats():
    """查看知识库统计信息（文档片段总数）。"""
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    return {"total_chunks": await kb.doc_count_async()}


def _eval_last_report_path() -> pathlib.Path:
    """最近一次评测报告的持久化路径（EVAL_LAST_REPORT_PATH 可覆盖）。

    默认随仓库 data/eval/（Docker 内 _ROOT=/app，与 baseline.json 同目录）。
    """
    default = pathlib.Path(_ROOT) / "data" / "eval" / "last_report.json"
    return pathlib.Path(os.getenv("EVAL_LAST_REPORT_PATH", str(default)))


@app.post("/eval/run")
async def run_eval(body: Optional[EvalRunInput] = None):
    """运行内置评测用例，返回评测报告。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    from evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase

    if body and body.intent_cases is not None:
        intent_cases = [
            IntentTestCase(
                message=c.message,
                expected_intent=c.expected_intent,
                context=c.context,
            )
            for c in body.intent_cases
        ]
    else:
        intent_cases = DEFAULT_INTENT_CASES

    if body and body.dialog_cases is not None:
        dialog_cases = [
            c.model_dump(exclude_none=True)
            for c in body.dialog_cases
        ]
    else:
        dialog_cases = DEFAULT_DIALOG_CASES

    report = await _evaluator.run(
        intent_cases=intent_cases,
        dialog_cases=dialog_cases,
    )
    data = {
        "timestamp":       report.timestamp,
        "pass_rate":       report.pass_rate,
        "total":           report.total,
        "passed":          report.passed,
        "avg_scores":      report.avg_scores,
        "regressions":     report.regressions,
        "recommendations": report.recommendations,
        "results": [
            {
                "test_id": r.test_id,
                "passed": r.passed,
                "scores": r.scores,
                "detail": r.detail,
                "metadata": r.metadata,
            }
            for r in report.results
        ],
    }
    # 持久化最近一次报告：服务重启后评测页仍可经 GET /eval/last 展示，不再空白。
    # 持久化失败不阻断本次响应（评测结果已在内存中返回）。
    try:
        path = _eval_last_report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as ex:
        logger.warning(f"评测报告持久化失败（不影响本次响应）: {ex}")
    return data


@app.get("/eval/last")
async def last_eval_report():
    """返回最近一次评测报告（/eval/run 完成时持久化到 data/eval/last_report.json）。

    尚无报告或文件损坏时返回 200 + 明确空态结构（available=false），
    前端评测页据此显示空态，而不是报错或空白。
    """
    path = _eval_last_report_path()
    if not path.exists():
        return {"available": False, "message": "尚未运行评测，暂无历史报告"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        logger.warning(f"评测报告读取失败: {ex}")
        return {"available": False, "message": f"最近一次评测报告读取失败（{path.name} 损坏）"}


# ── 前端同源服务（桌面/单进程模式）───────────────────────────────────────────
# 前端默认通过 /api/python 前缀访问后端（与 Nginx 反代路径一致）。
# 这里把全部 API 路由再挂一份到该前缀下，并将 frontend/dist 作为站点根路径，
# 使 desktop.pyw / `python -m api.main` 单进程即可同时服务前端与后端。
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRoute

for _route in list(app.routes):
    if isinstance(_route, APIRoute) and not _route.path.startswith("/api/python"):
        app.router.routes.append(APIRoute(
            f"/api/python{_route.path}",
            _route.endpoint,
            methods=list(_route.methods),
            response_model=_route.response_model,
            name=f"frontend_alias_{_route.name}",
            include_in_schema=False,
        ))


# Swagger 文档不在上面的 API 路由里（FastAPI 以独立形式挂载），单独补别名，
# 供前端"API 文档"按钮（{baseUrl}/docs）在桌面/Web/开发三种模式下都可用。
from fastapi.openapi.docs import get_swagger_ui_html


@app.get("/api/python/docs", include_in_schema=False, tags=["Docs"])
async def api_docs_alias():
    return get_swagger_ui_html(
        openapi_url="/api/python/openapi.json",
        title=f"{app.title} — API 文档",
    )


@app.get("/api/python/openapi.json", include_in_schema=False, tags=["Docs"])
async def api_openapi_alias():
    return app.openapi()


_FRONTEND_DIST = pathlib.Path(_ROOT) / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")


# ── 交互式 CLI ────────────────────────────────────────────────────────────────
async def _cli():
    print(BANNER)
    print("SafetyMind CLI — 输入 quit 退出\n")

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from memory.conversation_memory import MemoryManager, MsgRole
    from core.skill_loader import SkillManager

    cfg = _anthropic_cfg()
    skill_manager = SkillManager(
        root_dir=os.getenv("SAFETYMIND_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")),
        max_prompt_chars=int(os.getenv("SAFETYMIND_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    skill_manager.load()
    orch = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=skill_manager,
    )
    mem  = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "localhost"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "./data/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    user_id, conv_id = "cli_user", str(uuid.uuid4())

    while True:
        try:
            msg = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见 ʕ•ᴥ•ʔ")
            break
        if not msg or msg.lower() in ("quit", "exit", "退出"):
            print("再见 ʕ•ᴥ•ʔ")
            break

        ctx = await mem.get_context(user_id, conv_id, query=msg)
        history = [
            {"role": m.role.value, "content": m.content}
            for m in ctx.recent_messages[-5:]
        ] if ctx.recent_messages else None
        req = Request(message=msg, user_id=user_id, conv_id=conv_id, context=ctx.to_prompt_text(), history=history)
        result = await orch.run(req)

        await mem.add_message(user_id, conv_id, MsgRole.USER, msg)
        await mem.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)

        print(f"\nSafetyMind [{result.agent_type.value}]: {result.response}\n")

    await mem.close()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(_cli())
    else:
        uvicorn.run(
            "api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
