
import asyncio
import json
import logging
import os
import pathlib
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Dict, List, Optional


_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import uvicorn
from dotenv import load_dotenv
from fastapi.responses import StreamingResponse
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Header, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

load_dotenv()

# 紧急消息本地快速预检（零 LLM/零 IO）：复用 core.intent_recognizer 的 CRITICAL 词表与组合规则，
# 在普通对话闸门之前完成分流；未命中的紧急消息仍走原链路由 orchestrator 模板兜底。
from core import emergency  # noqa: E402  （需在 _ROOT 入 sys.path 之后导入）
from core import auth as subject_auth  # noqa: E402  （S-05：服务端主体与数据隔离）

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
_skill_evolution = None
_conv_store   = None
_audit        = None

def _intent_model_name(gen_model: str) -> str:
    """意图模型选择（薄封装，实现在 core/llm_params.intent_model_name）。

    为什么允许分离（2026-10-29 实测）：意图识别是"19 分类 + 小 JSON"任务，
    轻量模型同样准但快 6 倍（test2 n=150：准确率同为 0.980，低置信回落
    14652ms → 2347ms），而它覆盖约 42.7% 的请求。
    """
    from core.llm_params import intent_model_name
    return intent_model_name(gen_model)


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
    global _orchestrator, _memory, _tool_manager, _monitor, _evaluator, _skill_manager, _conv_store, _audit, _skill_evolution

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
    # 意图模型可与生成模型分离（2026-10-29 实测）：意图是"19 分类 + 小 JSON"任务，
    # 用轻量模型同样准、但快 6 倍。test2 n=150、BGE+回落同口径对比：
    #   raccoon/deepseek：准确率 0.980、低置信回落 14652ms
    #   aggregate/qwen3-8-flash：准确率 0.980、低置信回落 2347ms（−84%）
    # 低置信回落覆盖约 42.7% 请求，所以这是应用层能拿到的最大延迟收益。
    # SAFETYMIND_INTENT_MODEL 未设置时沿用生成模型（行为不变）。
    _intent_model = _intent_model_name(cfg["model"])
    recognizer = IntentRecognizer(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=_intent_model,
    )
    if _intent_model != cfg["model"]:
        logger.info(f"意图模型与生成模型分离: intent={_intent_model} gen={cfg['model']}")

    # Skills：启动时从目录加载业务能力说明，并在 Agent 调用 LLM 时动态注入。
    skills_dir = os.getenv("SAFETYMIND_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(
        root_dir=skills_dir,
        max_prompt_chars=int(os.getenv("SAFETYMIND_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    _skill_manager.load()

    # Skill 自进化一期：反馈采集 → 候选抽取 → 人工审批落盘（core/skill_evolution.py）
    from core.skill_evolution import SkillEvolution
    _skill_evolution = SkillEvolution(skills_root=skills_dir)
    logger.info(f"Skill 自进化已启用: 反馈={_skill_evolution.feedback_path}")

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

    # 模型后台预热：意图 BGE 与知识库嵌入器是两个独立实例、各自懒加载一份模型
    # （2 核上各 ~40s）。不预热时重启后首条消息付第一份、首条 RAG 消息再付第二份
    # （线上实测：冷启动后第一条 43.5s、下一条非生成段 49s）。预热在后台串行进行，
    # 不阻塞 startup；失败不影响功能（回落懒加载）。
    async def _warm_models():
        try:
            if os.getenv("SAFETYMIND_BGE", "") == "1":
                r = getattr(_orchestrator, "_intent_recognizer", None)
                bge = getattr(r, "_bge", None) if r else None
                if bge is not None:
                    await bge.try_recognize("系统预热", pattern_fn=r._pattern_recognize)
                    logger.info("预热完成：意图 BGE")
            await kb.search_async("系统预热", top_k=1)
            logger.info("预热完成：知识库嵌入器")
        except Exception as ex:
            logger.warning(f"模型预热失败（将懒加载兜底）: {ex}")

    asyncio.create_task(_warm_models())

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

    # 结构化资源库（MSDS 化学品 + 安全资料目录），失败不阻断启动
    try:
        from mcp.resource_library import ResourceLibrary
        _reslib = ResourceLibrary()
        logger.info(f"资源库已加载: {json.dumps(_reslib.stats(), ensure_ascii=False)}")

        def resource_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
            return {"error": f"资源库暂时不可用: {error}", "fallback": True}

        _tool_manager.register(Tool(
            name="msds_lookup",
            description="查询化学品安全技术说明书（MSDS 16 段结构化数据，支持别名与 CAS 号，可按章节取内容）",
            handler=_reslib.msds_handler,
            schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["lookup", "hot", "stats"]},
                    "name": {"type": "string", "description": "化学品名称或别名，如 乙醇/烧碱"},
                    "cas": {"type": "string"},
                    "section": {"type": "string", "description": "1-16 或章节名，如 泄漏应急处理"},
                    "top_k": {"type": "integer"},
                },
            },
            cache_ttl=120.0,
            fallback=resource_fallback,
        ))
        _tool_manager.register(Tool(
            name="resource_search",
            description="检索安全资料目录（documents=制度台账/培训课件/检查表，videos=事故案例，standards=法规标准）",
            handler=_reslib.resource_handler,
            schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string", "enum": ["documents", "videos", "standards"]},
                    "top_k": {"type": "integer"},
                },
            },
            cache_ttl=120.0,
            fallback=resource_fallback,
        ))
        _tool_manager.register(Tool(
            name="ledger_checklist",
            description="给定安全管理台账类型（培训/隐患/危化品/应急/特种设备/作业票/相关方/职业健康/安全投入），生成支撑文件清单并标注资源库中已有哪些、还缺哪些",
            handler=_reslib.ledger_handler,
            schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["checklist", "list"]},
                    "ledger": {"type": "string", "description": "台账类型，如 危化品台账"},
                },
            },
            cache_ttl=120.0,
            fallback=resource_fallback,
        ))
    except Exception as ex:
        logger.warning(f"资源库初始化失败（对话不受影响）: {ex}")

    # ── K03/K07：corpus（统一导入库）接入检索/工具链（全部加法式；无 ACTIVE build 时自动降级）──
    #    三件事：
    #      1) 注册 corpus_search / asset_open 两个工具（进入工具执行白名单）；
    #      2) 把 corpus 三路召回注册为 knowledge_base 的 corpus 提供者，
    #         使 /chat 注入与 /search/direct 在同一条检索链上并入 corpus 命中；
    #      3) 命中 body 走正文证据，命中 card 只作"仅目录"定位提示（见 _corpus_knowledge_context）。
    try:
        from corpus.search import CorpusSearch
        from corpus.search import open_asset as corpus_open_asset
        from mcp.knowledge_base import SCOPES_CONTEXT, set_corpus_provider

        def _corpus_search_sync(query: str, top_k: int, scopes):
            cs = CorpusSearch()          # 缺省 ACTIVE build；无则抛错由上层降级
            try:
                return cs.search(query, top_k=top_k, scopes=scopes)
            finally:
                cs.close()

        def corpus_provider(query: str, scopes=None):
            """knowledge_base 的 corpus 提供者：返回 corpus.search 结构（失败抛错由 KB 侧吞掉）。"""
            return _corpus_search_sync(query, int(os.getenv("SAFETYMIND_CORPUS_TOP_K", "6")), scopes)

        async def corpus_search_handler(params: Dict[str, Any], context: Any) -> Dict[str, Any]:
            scopes = params.get("scopes")
            if scopes is None and isinstance(context, dict):
                scopes = context.get("scopes")
            if scopes is None:
                scopes = SCOPES_CONTEXT.get()
            top_k = int(params.get("top_k") or 8)
            return await asyncio.to_thread(_corpus_search_sync,
                                           str(params.get("query", "")), top_k, scopes)

        def corpus_search_fallback(params, context, error):
            return {"query": str(params.get("query", "")), "blocks": [], "cards": [],
                    "build": None, "fallback": True, "error": f"corpus 检索不可用: {error}"}

        _tool_manager.register(Tool(
            name="corpus_search",
            description=("检索通用安全工程资料库（K03 统一导入库）：FTS+短目录卡+精选正文向量三路召回，"
                         "返回正文块 blocks（evidence_kind=body）与仅目录项 cards（evidence_kind=catalog）；"
                         "目录卡不得当作正文证据，命中卡只用于定位"),
            handler=corpus_search_handler,
            schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                    "scopes": {"type": "array", "description": "访问范围；缺省取请求级范围"},
                },
                "required": ["query"],
            },
            cache_ttl=0.0,          # scopes 不进缓存键 → 关闭缓存，防跨主体串味（与 S-05 同口径）
            fallback=corpus_search_fallback,
        ))

        async def asset_open_handler(params: Dict[str, Any], context: Any) -> Dict[str, Any]:
            key = str(params.get("asset") or params.get("asset_id") or params.get("sha256") or "")
            return await asyncio.to_thread(corpus_open_asset, key)

        def asset_open_fallback(params, context, error):
            return {"found": False, "is_materialized": False,
                    "error": f"corpus 原件访问不可用: {error}"}

        _tool_manager.register(Tool(
            name="asset_open",
            description=("打开资料原件：返回本地路径/字节/mime/解析状态；只给原件与定位，"
                         "不把正文当证据；未落盘时如实回未取得状态"),
            handler=asset_open_handler,
            schema={"type": "object",
                    "properties": {"asset": {"type": "string",
                                             "description": "asset_id / sha256 / 来源 item_id"}},
                    "required": ["asset"]},
            cache_ttl=0.0,
            fallback=asset_open_fallback,
        ))
        set_corpus_provider(corpus_provider)
        logger.info("corpus 接入已就绪: 工具 corpus_search / asset_open；corpus 提供者已注册")
    except Exception as ex:
        logger.warning(f"corpus 接入初始化失败（既有链路不受影响）: {ex}")

    # ── P0-A 真实工具执行回路（只读白名单 + 双预算）：把已注册工具暴露给专家模型，
    #    由统一执行器控权限/预算/超时，专家只提调用意图，不各自执行副作用。
    #    未注册、白名单为空或服务端不支持 tools 时，执行器自动降级为原单轮调用（行为回退）。
    try:
        from anthropic import AsyncAnthropic
        from core.llm_utils import llm_client_kwargs, make_llm_client
        from core.tool_execution import ToolExecutor
        _client_kwargs: Dict[str, Any] = {"api_key": cfg["api_key"]}
        if cfg.get("base_url"):
            _client_kwargs["base_url"] = cfg["base_url"]
        _allow = [n.strip() for n in os.getenv(
            "SAFETYMIND_TOOL_ALLOWLIST",
            "knowledge_search,msds_lookup,resource_search,ledger_checklist,"
            "corpus_search,asset_open").split(",")
            if n.strip()]
        _tool_executor = ToolExecutor(
            client=make_llm_client(api_key=cfg["api_key"], base_url=cfg.get("base_url", ""), model=cfg["model"], **llm_client_kwargs()),
            model=cfg["model"],
            tool_manager=_tool_manager,
            allowlist=_allow,
            max_rounds=int(os.getenv("SAFETYMIND_TOOL_MAX_ROUNDS", "3")),
            max_calls=int(os.getenv("SAFETYMIND_TOOL_MAX_CALLS", "4")),
        )
        _orchestrator.set_tool_executor(_tool_executor)
        logger.info(f"工具执行回路已启用: allowlist={_allow}")
    except Exception as ex:
        logger.warning(f"工具执行回路初始化失败（退回无工具单轮调用）: {ex}")

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

    # 隐患工单逾期巡检（PerformanceMonitor 同款后台循环模式）：首次逾期记
    # overdue_notify 时间线并发值班 webhook（check_overdue_tickets 幂等，只发一次）；
    # SAFETYMIND_HAZARD_OVERDUE_CHECK=0 关闭。失败只告警，绝不影响主服务。
    hazard_overdue_task = None
    if _audit is not None and os.getenv("SAFETYMIND_HAZARD_OVERDUE_CHECK", "1") == "1":
        async def _hazard_overdue_loop():
            interval = float(os.getenv("SAFETYMIND_HAZARD_OVERDUE_CHECK_S", "300"))
            while True:
                try:
                    for t in _audit.check_overdue_tickets():
                        _enqueue_ticket_notification(
                            t["id"], t.get("source_request_id", ""),
                            f"【SafetyMind 工单逾期】#{t['id']} {t['title']}\n"
                            f"整改期限已过仍未闭环，请立即督办。\n"
                            f"时间: {time.strftime('%m-%d %H:%M:%S')}", reason="overdue")
                except Exception as ex:
                    logger.warning(f"隐患逾期巡检失败: {ex}")
                await asyncio.sleep(interval)
        hazard_overdue_task = asyncio.create_task(_hazard_overdue_loop())

    # 通知 outbox 后台投递（可靠通知）：建单/派单时通知与工单同事务落库，由本循环
    # 领取投递、指数退避重试、进程重启后继续投递（悬挂行由 reap 复位）。
    # SAFETYMIND_OUTBOX_ENABLED=0 关闭；间隔/批量/最大次数可配。失败只告警，绝不拖垮服务。
    outbox_task = None
    if _audit is not None and os.getenv("SAFETYMIND_OUTBOX_ENABLED", "1") == "1":
        async def _outbox_loop():
            from core.audit_store import notify_escalation_blocking
            from core.notification_outbox import deliver_pending
            interval = float(os.getenv("SAFETYMIND_OUTBOX_INTERVAL_S", "5"))
            batch = int(os.getenv("SAFETYMIND_OUTBOX_BATCH", "10"))
            max_attempts = int(os.getenv("SAFETYMIND_OUTBOX_MAX_ATTEMPTS", "5"))

            def _sender(url: str, payload: Dict[str, Any]) -> bool:
                # 同步阻塞的 HTTP 投递：整个 deliver_pending 在线程内执行，不阻塞事件循环
                return notify_escalation_blocking(url, payload)

            while True:
                try:
                    stats = await asyncio.to_thread(
                        deliver_pending, _audit, _sender, batch, max_attempts,
                        5.0, 300.0, 120.0, _on_outbox_sent)
                    if stats.get("sent") or stats.get("failed") or stats.get("reaped"):
                        logger.info(
                            f"outbox 投递：领取 {stats['claimed']} / 送达 {stats['sent']}"
                            f" / 失败 {stats['failed']} / 悬挂复位 {stats['reaped']}")
                except Exception as ex:
                    logger.warning(f"outbox 投递循环失败（不影响服务）: {ex}")
                await asyncio.sleep(interval)
        outbox_task = asyncio.create_task(_outbox_loop())

    # 意图引擎后台预热：启用 SAFETYMIND_BGE=1 时，首条消息不再等本地模型冷加载
    # （冷加载约 20-40s，预热后意图阶段毫秒级直出）。
    if os.getenv("SAFETYMIND_BGE", "") == "1":
        asyncio.create_task(_orchestrator.recognize_intent("系统预热"))

    logger.info("SafetyMind 已就绪")
    yield

    if hazard_overdue_task is not None:
        hazard_overdue_task.cancel()
    if outbox_task is not None:
        outbox_task.cancel()
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


# 对话链路并发闸门：免费 LLM + 2 核 torch 推理下，20 路并发对话会吃满 CPU
# 把毫秒级检索也饿死（压测实测 search p50 6.5s）。限制同时 4 路进对话链路，
# 其余请求在 asyncio 层排队——LLM 是外部瓶颈，排队比雪崩好。
_CHAT_GATE = asyncio.Semaphore(int(os.getenv("CHAT_MAX_CONCURRENCY", "4")))
# 排队上限：闸门满时最多再等这么久。LLM 客户端已统一 60s 超时（llm_client_kwargs），
# 单路最长可预期；但线上压测实测无上限排队会把客户端拖到 180s+ 才断。
# 超时回 429 让前端提示稍后重试，比无限挂死体面。设 0 = 关闭（恢复旧行为）。
_CHAT_QUEUE_TIMEOUT = float(os.getenv("CHAT_QUEUE_TIMEOUT_S", "180"))

# ── 确定性查询快车道（2026-10-28 并发轴实测新增）──────────────────────────
# 缺陷：确定性查询（CAS/化学品参数）单发实测 96ms，但 8 路并发下 p50 变成 **21.9s**——
# 因为它和长 RAG/生成请求共用 _CHAT_GATE（容量 4），排在重请求后面。
# 对用户意味着：安全员查"乙醇闪点是多少"要等别人 40s 的长回答先跑完。
# 修法：把**零 LLM 的确定性查询**分流到独立闸门，不与生成请求争容量。
# 预筛是纯本地正则/词表（零 I/O、零 LLM），且**保守**：只有同时命中
# "化学品名或 CAS + 查询意图词 + 无处置类词" 才进快车道；
# 万一是误判，请求只是少排了队，_chat_core 内部仍会走完整逻辑（不会答偏）。
_FAST_GATE = asyncio.Semaphore(int(os.getenv("FAST_LANE_MAX_CONCURRENCY", "8")))

# ── 检索链路闸门：/search 与 /search/direct 各含本地 BGE 推理（CPU 密集），
# 线上压测实测 2 路并发即可在 2 核机器上把事件循环饿死（连 /health 都无响应，
# 撤载后 30-40s 才恢复）。闸门内限流，闸门外排队，超时回 429。
_SEARCH_GATE = asyncio.Semaphore(int(os.getenv("SEARCH_MAX_CONCURRENCY", "2")))
_SEARCH_QUEUE_TIMEOUT = float(os.getenv("SEARCH_QUEUE_TIMEOUT_S", "60"))

# ── 应急快速通道闸门（独立于 _CHAT_GATE）：紧急消息不得排在普通对话队列后面，
# 也不能反过来挤占普通链路。拿不到容量时不 429，而是把副作用降级为"记录待补"
# 并只返回经领域审核的最小响应（触点越少越可靠）。
_EMERGENCY_GATE = asyncio.Semaphore(int(os.getenv("EMERGENCY_MAX_CONCURRENCY", "8")))
EMERGENCY_QUEUE_TIMEOUT_S = float(os.getenv("EMERGENCY_QUEUE_TIMEOUT_S", "0.5"))


async def _acquire_with_timeout(gate: asyncio.Semaphore, timeout_s: float, what: str) -> bool:
    """带排队上限的闸门获取：超时抛 429（Retry-After 5s）。timeout_s<=0 时无限等待。"""
    if timeout_s <= 0:
        await gate.acquire()
        return True
    try:
        await asyncio.wait_for(gate.acquire(), timeout=timeout_s)
        return True
    except asyncio.TimeoutError:
        raise HTTPException(
            429, f"{what}当前繁忙，请稍后重试（排队超时 {timeout_s:.0f}s）",
            headers={"Retry-After": "5"})

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


# ── S-05 服务端主体与数据隔离（单一入口）──────────────────────────────────────
# 本地单人形态（无 ACCESS_TOKEN、未部署、绑定回环）行为逐字节不变：主体 = 客户端 user_id
# 的可信本地身份（全角色 + 全资料范围），所有隔离分支都不生效。
# 多用户/网络形态且已配置 SAFETYMIND_USERS 时：主体由 X-User-Token 解析，
# 请求的会话/工单/资料范围一律以服务端主体为准（客户端 user_id 不能作为授权依据）。
def _subject_for(user_token: str, user_id: str = "") -> subject_auth.Subject:
    """单一主体入口：从请求头 X-User-Token（对齐 X-Access-Token/X-Admin-Token 风格）
    + 客户端 user_id 解析服务端主体。"""
    return subject_auth.resolve_subject(user_token, user_id)


def _isolation_enforced() -> bool:
    """是否强制主体隔离（多用户/网络形态且已配置主体目录）。"""
    return subject_auth.isolation_enforced()


def _subject_id_of(req) -> str:
    """响应主体标识（additive）：多用户形态回服务端主体 id，本地形态回空串。"""
    return req.user_id if _isolation_enforced() else ""


@contextmanager
def _scoped_knowledge(subject: subject_auth.Subject):
    """多用户形态下按主体资料范围约束**本次请求的全部知识库检索**。

    除了显式传 scopes 的检索端点，还覆盖模型驱动的工具调用（P0-A 执行器）与 /chat 的
    RAG 注入——实现方式是库侧请求级 contextvar 绑定（asyncio.to_thread 复制上下文，
    线程内同样生效）。同时把 knowledge_search 工具缓存关掉：缓存键不含资料范围，
    开启时会跨主体串味（同一 query 复用别人的命中）。
    本地形态或资料范围不受限的主体直接放行，行为不变。
    """
    if not _isolation_enforced() or subject.unrestricted:
        yield
        return
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager is not None else None
    kb = getattr(tool.handler, "__self__", None) if tool is not None else None
    token = kb.bind_scopes(subject.scopes) if kb is not None else None
    old_ttl = tool.cache_ttl if tool is not None else None
    try:
        if tool is not None:
            tool.cache_ttl = 0.0
        yield
    finally:
        if tool is not None and old_ttl is not None:
            tool.cache_ttl = old_ttl
        if kb is not None and token is not None:
            kb.reset_scopes(token)


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
    # 升级依据（additive，2026-10-29）：urgency/intent 是确定性的，model_hint 取决于
    # 模型正文措辞（同一问题两次可能不同）——测试与自动化应断言 basis 而不是 escalated。
    escalation_basis: str = ""
    latency_ms:  float
    total_ms:    float = 0.0   # 全链路耗时（排队+记忆+意图+RAG+生成）；0=旧版后端未上报
    knowledge_used: bool = False
    entities: Dict[str, List[str]] = Field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_source_scores: Dict[str, float] = Field(default_factory=dict)
    request_id:  str = ""   # 升级留痕编号（用户端"复制上报编号"用）
    # ── 隐患工单闸门（分层触发，additive 字段不破坏旧契约）──
    # none=不建单 | suggested=中档建议（用户端确认卡，点 /hazard/confirm 才建单）
    # auto_ticket=高危自动建单 | merged=24h 内同实体重复上报，已合并进既有单
    hazard_action: str = "none"
    hazard_ticket_id: int = 0
    hazard_suggestion: Optional[Dict[str, Any]] = None   # suggested 时的确认卡数据
    # ── 紧急快速分流（additive 字段，默认值 = 旧行为，不破坏既有契约）──
    # 处置状态区文案：{recorded/ticket/notify}，四态如实表达（未送达绝不写"已通知"）
    emergency_status: Dict[str, str] = Field(default_factory=dict)
    emergency_fastpath: bool = False                     # True = 走了零 LLM 快速分流
    urgency: str = ""                                    # 紧急度名（快速分流为 critical；""=旧后端未上报）
    # ── P0-B 证据引用（additive 字段，默认空 = 旧行为）──
    # 每条：{index,title,doc_id,chunk_id,section,score,source,version,status,
    #         snippet,catalog_only} —— 前端据此展示可定位的依据；引用必须来自本次实际证据集合。
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    # ── P0-A 工具轨迹（additive）：本次请求真实发生的工具调用（名称/成功/耗时/错误），
    #    不是"口头声称已查询"；便于集成断言"用户输入触发正确工具且结果进入回答"。──
    tools_used: List[Dict[str, Any]] = Field(default_factory=list)
    # ── S-05 服务端主体（additive，默认空 = 本地形态/旧后端）：多用户形态下为服务端
    #    解析出的主体 id（客户端 user_id 不得作为授权依据，响应回填便于前端与断言核对）。──
    subject_id:  str = ""


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/config")
async def app_config():
    """用户端展示配置（转人工值班电话等）。无敏感信息，可公开。"""
    return {
        "escalation_phone": os.getenv("ESCALATION_PHONE", "").strip(),
        "product": "SafetyMind",
    }


# ── 会话历史（跨天可恢复）───────────────────────────────────────────────────
# 多用户形态（S-05）下 user_id 一律取服务端主体：客户端传的 user_id 既不作为归属依据，
# 也不影响读取范围——别人的会话不在本主体目录下，所以读列表/读消息/删除都不命中
# （返回空列表/空消息/deleted=false，不泄露"存在但无权限"）。
def _conv_owner(user_id: str, user_token: str) -> str:
    """会话归属主体：多用户形态 = 服务端主体 id，本地形态 = 客户端 user_id（现状不变）。"""
    if _isolation_enforced():
        return _subject_for(user_token, user_id).subject_id
    return user_id


@app.get("/conversations", tags=["会话历史"])
async def list_conversations(user_id: str = "anonymous",
                             x_user_token: str = Header(default="", alias="X-User-Token")):
    """当前用户的历史会话列表（按最近更新倒序）。"""
    if _conv_store is None:
        raise HTTPException(503, "会话存储未初始化")
    return {"conversations": _conv_store.list_conversations(_conv_owner(user_id, x_user_token))}


@app.get("/conversations/{conv_id}/messages", tags=["会话历史"])
async def get_conversation_messages(conv_id: str, user_id: str = "anonymous",
                                    x_user_token: str = Header(default="", alias="X-User-Token")):
    """读取某历史会话的完整消息（用于恢复继续对话）。"""
    if _conv_store is None:
        raise HTTPException(503, "会话存储未初始化")
    return {"conv_id": conv_id, "messages": _conv_store.get_messages(
        _conv_owner(user_id, x_user_token), conv_id)}


@app.delete("/conversations/{conv_id}", tags=["会话历史"])
async def delete_conversation(conv_id: str, user_id: str = "anonymous",
                              x_user_token: str = Header(default="", alias="X-User-Token")):
    if _conv_store is None:
        raise HTTPException(503, "会话存储未初始化")
    return {"deleted": _conv_store.delete_conversation(
        _conv_owner(user_id, x_user_token), conv_id)}


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


@app.get("/audit/ticket-effectiveness", tags=["审计"])
async def ticket_effectiveness(_admin: None = Depends(require_admin)):
    """工单闭环**实效性**审计（2026-10-26 新增）。

    与 /audit/stats 的区别：stats 回答"有多少工单"，这里回答"工单真的闭环了吗"。
    安环部实际考核的四项：
      ① 超期未闭环数（超期=失职，首要整改项）
      ② 按期闭环率、验收留痕率（无验收签字 = 未真正闭环）
      ③ 重复隐患组数（同地点同类反复 = 整改停留在治标）
      ④ 重大隐患报备率（法定要求，未报备即风险）
    样本不足时如实返回 note，不编造数字。
    """
    try:
        from benchmarks.audit_ticket_effectiveness import audit as _audit_eff
        return await asyncio.to_thread(
            _audit_eff, os.getenv("AUDIT_DB_PATH", os.path.join(_ROOT, "data", "audit.db")))
    except Exception as ex:
        raise HTTPException(503, f"实效性审计不可用: {ex}")


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


class TicketCloseInput(BaseModel):
    """旧兼容闭单入口的整改证据（S-05，可选）：多用户形态下缺证据不得闭单。
    note=验收批注；evidence=整改证据说明；artifact=整改产物（文件名/链接等）。"""
    note: str = ""
    evidence: str = ""
    artifact: str = ""


@app.post("/tickets/{ticket_id}/close", tags=["审计"])
async def ticket_close(ticket_id: int, body: Optional[TicketCloseInput] = None,
                       _admin: None = Depends(require_admin),
                       x_user_token: str = Header(default="", alias="X-User-Token")):
    """旧两态闭环入口。本地形态行为不变（无 body 也能关）。
    多用户形态（S-05）：缺整改证据 400；无验收者/管理员角色 403 ——
    "缺整改证据不能经正常或旧兼容入口闭单"，处理者不能越权验收。
    store 层 close_ticket 语义不动（被 store 级测试直接调用）。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    if _isolation_enforced():
        evidence = body or TicketCloseInput()
        if not (evidence.note.strip() or evidence.evidence.strip()):
            raise HTTPException(400, "缺整改证据不能经正常或旧兼容入口闭单：请提供验收批注或整改证据")
        subject = _subject_for(x_user_token, "")
        if not subject.has_role("verifier", "admin"):
            raise HTTPException(403, "无权验收：需要验收者或管理员角色")
    return {"closed": _audit.close_ticket(ticket_id)}


# ── 工单五阶段回路（brief→discuss→execute→feedback→accept/reject，借鉴 OCTO Loop）──
class TicketTransitionInput(BaseModel):
    """阶段流转：to_stage ∈ brief|discuss|execute|feedback|accept|reject；
    actor/note 作为操作者批注随时间线落库。"""
    to_stage: str
    actor: str = "admin"
    note: str = ""


@app.post("/tickets/{ticket_id}/transition", tags=["审计"])
async def ticket_transition(ticket_id: int, body: TicketTransitionInput,
                            _admin: None = Depends(require_admin),
                            x_user_token: str = Header(default="", alias="X-User-Token")):
    """五阶段流转：合法流转返回新状态，非法跳转/未知阶段 400，工单不存在 404。

    多用户形态（S-05）角色边界：accept/reject 需要主体含 verifier 或 admin，否则 403
    （处理者不能越权验收）；accept 另要求非空验收批注，空批注 400。本地形态不生效。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    if _isolation_enforced() and body.to_stage in ("accept", "reject"):
        subject = _subject_for(x_user_token, body.actor)
        if not subject.has_role("verifier", "admin"):
            raise HTTPException(403, "无权验收：需要验收者或管理员角色")
        if body.to_stage == "accept" and not (body.note or "").strip():
            raise HTTPException(400, "验收必须填写验收批注（验收批注不得为空）")
    result = _audit.transition_ticket(ticket_id, body.to_stage,
                                      actor=body.actor, note=body.note)
    if not result["ok"]:
        if result.get("kind") == "not_found":
            raise HTTPException(404, result["error"])
        raise HTTPException(400, result["error"])
    return result


@app.get("/tickets/{ticket_id}/timeline", tags=["审计"])
async def ticket_timeline(ticket_id: int, _admin: None = Depends(require_admin)):
    """完整时间线：建单简报 → 历次流转（操作者/批注/时间戳）→ 通知/兼容事件。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    ticket = _audit.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(404, f"工单不存在: {ticket_id}")
    timeline = _audit.get_ticket_timeline(ticket_id)
    return {"ticket": ticket, "timeline": timeline or []}


@app.get("/tickets/progress/{request_id}", tags=["审计"])
async def ticket_progress(request_id: str,
                          x_user_token: str = Header(default="", alias="X-User-Token")):
    """用户端升级卡片（克制展示）：按上报编号查工单五阶段进度与最新批注。
    免管理令牌——上报编号本身即凭据（仅上报者可见）；刻意只回进度骨架与
    最新一条批注，完整时间线/简报细节仍在管理端 GET /tickets/{id}/timeline。
    无关联工单回 200 + found=false（前端降级为"工单生成中"空态，不当作错误）。

    S-05：多用户形态下还必须是该编号的归属主体（工单/升级事件的 user_id 与主体一致），
    否则同样回 found=false——不区分"不存在"与"不属于你"，不泄露存在性。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    if _isolation_enforced():
        subject = _subject_for(x_user_token, "")
        owner = _audit.owner_of_request(request_id)
        if owner is None or owner != subject.subject_id:
            return {"found": False}
    progress = _audit.get_ticket_progress(request_id)
    if progress is None:
        return {"found": False}
    return progress


# ── 隐患工单闭环（分层触发：HIGH+ 自动建单 / MEDIUM 确认卡；对齐双重预防机制）──
def _create_hazard_ticket(audit, decision: Dict[str, Any], brief: Dict[str, Any],
                          request_id: str, user_id: str, actor: str) -> tuple:
    """按闸门裁决建隐患工单。去重先行：同用户 24h 内同实体（设备/区域/化学品）
    未关闭隐患单不重复建，合并 repeat_report 事件（防工单贬值）。返回 (ticket_id, merged)。"""
    dup = audit.find_open_hazard_duplicate(user_id, brief.get("entities") or {})
    if dup is not None:
        audit.record_ticket_event(
            dup["id"], "repeat_report", actor=actor,
            note="24 小时内同实体重复上报，合并进本工单",
            detail={"request_id": request_id, "severity": decision.get("severity", "")})
        return dup["id"], True
    priority = "high" if decision.get("severity") == "major" else "normal"
    title = decision.get("summary") or f"隐患上报: {brief.get('message_digest', '')[:60]}"
    try:
        deadline_h = float(decision.get("deadline_hours") or 168)
    except (TypeError, ValueError):
        deadline_h = 168.0
    tid = audit.create_ticket(
        title, type_="hazard", priority=priority, source_request_id=request_id,
        due_ts=time.time() + deadline_h * 3600, brief=brief, actor=actor,
        user_id=user_id, severity=decision.get("severity", ""),
        responsible_dept=decision.get("dept", ""))
    return tid, False


def _enqueue_ticket_notification(ticket_id: int, request_id: str, text: str,
                                 reason: str = "create") -> int:
    """工单通知入 outbox（幂等键 ticket:{id}:{reason}:webhook）。

    未配置渠道时仍入队（channel='' + last_error='未配置通知渠道'），绝不伪装成已送达；
    实际投递与"已实收"时间线由后台 worker 在渠道真正受理（sent）后经 _on_outbox_sent 补写。
    reason: create（建单/确认卡派单）| overdue（逾期巡检升级提醒）。
    """
    if _audit is None:
        return 0
    webhook = os.getenv("ESCALATION_WEBHOOK_URL", "").strip()
    try:
        return _audit.enqueue_notification(
            idempotency_key=f"ticket:{ticket_id}:{reason}:webhook",
            channel=webhook,
            payload={"request_id": request_id, "text": text,
                     "timeline": {"ticket_id": ticket_id, "note": "派单 webhook 已实收",
                                  "to_stage": "brief", "request_id": request_id}},
            request_id=request_id, ticket_id=ticket_id)
    except Exception as ex:                                    # 入队失败只告警，不阻断对话
        logger.warning(f"工单通知入队失败（ticket #{ticket_id}）: {ex}")
        return 0


def _on_outbox_sent(row: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """outbox 投递成功后的补痕（worker 回调）：升级事件标记已通知 + 工单时间线写"已实收"。

    只在渠道真正受理（sent）后调用——"已实收"不得在建单/入队时预写，
    未配置或投递失败时 upgrade_events.notified 保持 0（前端契约不变）。
    """
    if _audit is None:
        return
    try:
        upgrade_id = row.get("upgrade_id")
        if upgrade_id:
            _audit.mark_notified(int(upgrade_id), "webhook")
    except Exception as ex:
        logger.warning(f"升级事件标记通知失败: {ex}")
    try:
        tl = payload.get("timeline") if isinstance(payload, dict) else None
        ticket_id = (tl or {}).get("ticket_id") or row.get("ticket_id")
        if ticket_id:
            _audit.record_ticket_event(
                int(ticket_id), "notify_webhook", actor="system",
                note=(tl or {}).get("note") or "值班 webhook 已实收",
                to_stage=(tl or {}).get("to_stage") or "brief",
                detail={"request_id": (tl or {}).get("request_id") or row.get("request_id", "")})
    except Exception as ex:
        logger.warning(f"工单通知时间线记录失败: {ex}")


class HazardConfirmRequest(BaseModel):
    """确认卡点击（镜像 /feedback 哲学：request_id 即凭据，免管理令牌——
    建议只发给上报者本人，且 24h 过期）。"""
    request_id: str
    user_id: str = ""
    conv_id: str = ""


@app.post("/hazard/confirm", tags=["审计"])
async def hazard_confirm(body: HazardConfirmRequest,
                         x_user_token: str = Header(default="", alias="X-User-Token")):
    """确认上报建议 → 建隐患工单（brief 阶段起步，五阶段回路闭环）。
    建议不存在/已处理/已过期回 404；重复上报自动合并（merged=true）。

    S-05：多用户形态下建议归属必须与服务端主体一致，否则 403（改 user_id 不能越权）；
    建议不存在/已处理仍按既有语义优先回 404。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    sug = _audit.get_hazard_suggestion(body.request_id)
    if sug is None:
        raise HTTPException(404, "上报建议不存在、已处理或已过期（24 小时内有效）")
    payload = sug.get("payload") or {}
    decision = payload.get("decision") or {}
    brief = payload.get("brief") or {}
    if _isolation_enforced():
        subject = _subject_for(x_user_token, body.user_id)
        if str(sug.get("user_id") or "") != subject.subject_id:
            raise HTTPException(403, "无权处理该上报事项")
        user_id = subject.subject_id
    else:
        user_id = body.user_id or sug.get("user_id") or "anonymous"
    tid, merged = _create_hazard_ticket(_audit, decision, brief,
                                        body.request_id, user_id, actor=user_id)
    _audit.mark_hazard_suggestion(body.request_id, "merged" if merged else "confirmed",
                                  ticket_id=tid)
    _enqueue_ticket_notification(
        tid, body.request_id,
        f"【SafetyMind 隐患工单】编号 {body.request_id}\n{decision.get('summary', '')}\n"
        f"级别: {'重大' if decision.get('severity') == 'major' else '一般'}"
        f" · 派发: {decision.get('dept', '安环部')}"
        f" · 限期: {decision.get('deadline_hours', '')} 小时\n"
        f"时间: {time.strftime('%m-%d %H:%M:%S')}\n请责任部门限期整改，验收走工单五阶段。")
    return {"ok": True, "ticket_id": tid, "merged": merged}


class TicketPatchInput(BaseModel):
    """研讨阶段人工改判（PATCH /tickets/{id}）：改定级/派发部门/责任人/限期，
    改动前后值都落 patch 时间线，全程留痕。"""
    severity: Optional[str] = None         # major | general
    responsible_dept: Optional[str] = None
    assignee: Optional[str] = None
    priority: Optional[str] = None         # low | normal | high | critical
    due_hours: Optional[float] = None      # 相对当前时间的新限期
    note: str = ""


@app.patch("/tickets/{ticket_id}", tags=["审计"])
async def ticket_patch(ticket_id: int, body: TicketPatchInput,
                       _admin: None = Depends(require_admin)):
    """白名单字段修改 + 时间线留痕；工单不存在 404，无有效字段 400。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    fields: Dict[str, Any] = {}
    if body.severity is not None:
        fields["severity"] = body.severity
    if body.responsible_dept is not None:
        fields["responsible_dept"] = body.responsible_dept
    if body.assignee is not None:
        fields["assignee"] = body.assignee
    if body.priority is not None:
        fields["priority"] = body.priority
    if body.due_hours is not None:
        fields["due_ts"] = time.time() + body.due_hours * 3600
    result = _audit.update_ticket_fields(ticket_id, fields, actor="admin", note=body.note)
    if not result["ok"]:
        if result.get("kind") == "not_found":
            raise HTTPException(404, result["error"])
        raise HTTPException(400, result["error"])
    return result


@app.get("/tickets/overdue", tags=["审计"])
async def tickets_overdue(_admin: None = Depends(require_admin)):
    """逾期未闭环工单清单（整改期限已过且未验收）。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    return {"tickets": _audit.list_overdue_tickets()}


# ── .env 可视化配置（管理端点；白名单 + 掩码 + 原子写）───────────────────────
@app.post("/config/admin/bootstrap", tags=["审计"])
async def admin_config_bootstrap():
    """首次设置：ADMIN_TOKEN 未配置时一键生成管理密码（仅未配置时可用，先到先得）。
    已配置后此端点永久失效——改密码必须凭现有密码走 /config/admin。"""
    from core.env_config import EnvConfigManager
    _deployed = os.getenv("SAFETYMIND_DEPLOYED", "").strip() == "1"
    _h = os.getenv("API_HOST", "0.0.0.0").strip().lower()
    if _deployed or _h not in ("127.0.0.1", "localhost", "::1"):
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


class FeedbackRequest(BaseModel):
    """用户对回答的反馈（👍/👎/评语）。负反馈或带评语时后台提炼 skill 候选。"""
    request_id: str
    rating: str                      # "up" | "down"
    comment: str = ""
    user_id: str = ""
    conv_id: str = ""
    message_head: str = ""
    answer_head: str = ""


@app.post("/feedback", tags=["Skills"])
async def submit_feedback(fb: FeedbackRequest):
    """记录用户反馈；负反馈/评语触发后台候选抽取（不阻塞响应，SAFETYMIND_SKILL_EVOLUTION=0 关闭）。"""
    if _skill_evolution is None:
        raise HTTPException(503, "Skill 自进化未初始化")
    if fb.rating not in ("up", "down"):
        raise HTTPException(422, "rating 必须是 up/down")
    rec = _skill_evolution.record_feedback(
        request_id=fb.request_id, rating=fb.rating, comment=fb.comment,
        user_id=fb.user_id, conv_id=fb.conv_id,
        message_head=fb.message_head, answer_head=fb.answer_head)
    # 偏好卡片沉淀（OCTO 式资产沉淀，挂在反馈信号上）：规则抽取 + SQLite 极快写入，
    # 内部全量 try/except 降级，失败只告警绝不阻断反馈响应。
    if _memory is not None:
        await _memory.absorb_feedback(
            fb.user_id, request_id=fb.request_id, rating=fb.rating,
            comment=fb.comment, message_head=fb.message_head, answer_head=fb.answer_head)
    if fb.rating == "down" or fb.comment.strip():
        from core.skill_evolution import spawn_extraction
        skills_meta = [{
            "name": s.name, "keywords": s.keywords,
            "content": s.content, "path": s.path,
        } for s in _skill_manager.skills] if _skill_manager else []
        spawn_extraction(_skill_evolution, rec, skills_meta,
                         api_key=os.getenv("ANTHROPIC_API_KEY", ""),
                         base_url=os.getenv("ANTHROPIC_BASE_URL", ""),
                         model=os.getenv("ANTHROPIC_MODEL", "glm-4.7-flash"))
    return {"ok": True, "recorded": True}


@app.get("/skills/candidates", tags=["Skills"])
async def list_skill_candidates(status: str = "pending", _: None = Depends(require_admin)):
    """Skill 自进化候选清单（管理端点，X-Admin-Token）。status=all 看全部。"""
    if _skill_evolution is None:
        raise HTTPException(503, "Skill 自进化未初始化")
    return {"candidates": _skill_evolution.list_candidates(
        None if status == "all" else status)}


@app.post("/skills/candidates/{cand_id}/approve", tags=["Skills"])
async def approve_skill_candidate(cand_id: str, _: None = Depends(require_admin)):
    """批准候选：add=新建 skills/evolved_*/SKILL.md；merge=追加修订段（先存快照）。落盘后自动热加载。"""
    if _skill_evolution is None:
        raise HTTPException(503, "Skill 自进化未初始化")
    result = _skill_evolution.approve(cand_id)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "审批失败"))
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return {"ok": True, "written": result.get("written"),
            "skills_count": len(_skill_manager.skills)}


@app.post("/skills/candidates/{cand_id}/reject", tags=["Skills"])
async def reject_skill_candidate(cand_id: str, _: None = Depends(require_admin)):
    """拒绝候选（记录在案，反馈不删）。"""
    if _skill_evolution is None:
        raise HTTPException(503, "Skill 自进化未初始化")
    result = _skill_evolution.reject(cand_id)
    if not result.get("ok"):
        raise HTTPException(404, "候选不存在或已处理")
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest,
               x_user_token: str = Header(default="", alias="X-User-Token")):
    """
    主对话接口。完整流程：
      记忆读取 → 意图识别 → Agent 路由 → 执行 → 记忆写入
    """
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    # S-05 主体归属：多用户形态下服务端主体覆盖客户端 user_id——记忆、会话落盘、
    # 审计、工单/升级归属全部以主体为准（客户端改 user_id 读不到别人的数据）。
    # 本地形态保持现状：req.user_id 即客户端传值（桌面单人可信边界）。
    subject = _subject_for(x_user_token, req.user_id)
    if _isolation_enforced():
        req = req.model_copy(update={"user_id": subject.subject_id})

    # 紧急消息预检分流（SAFETYMIND_EMERGENCY_FASTPATH=0 关闭）：本地规则（零 LLM/零记忆）
    # 在普通对话闸门之前命中即直出最小响应，急事不与普通请求争排队。
    # 注意：命中与否都不改变 orchestrator 既有的 CRITICAL 模板路径——未命中的紧急消息
    # 仍走完整链路并在 orchestrator 里模板化（漏检兜底，规则不弱化）。
    if os.getenv("SAFETYMIND_EMERGENCY_FASTPATH", "1") == "1":
        sig = emergency.precheck(req.message)
        if sig is not None:
            return await _emergency_fast_path(req, sig, t0=time.perf_counter())

    from agents.agent_orchestrator import Request as OrcReq
    from memory.conversation_memory import MsgRole

    conv_id = req.conv_id or str(uuid.uuid4())

    # 对话链路并发闸门：意图+生成全程持锁（记忆读取在内，
    # 20 并发下后到者在 asyncio 层排队，保护检索/统计等轻端点不被 CPU 饿死）
    # 确定性查询走独立快车道：见 _FAST_GATE 的说明（8 并发下 p50 21.9s → 目标 <1s）
    _lane = _FAST_GATE if _is_pure_chemical_lookup(req.message) else _CHAT_GATE
    await _acquire_with_timeout(_lane, _CHAT_QUEUE_TIMEOUT, "对话")
    t_start = time.perf_counter()
    try:
        # S-05 资料范围：多用户形态下绑定主体 scopes（覆盖 RAG 注入与模型驱动的工具调用）
        with _scoped_knowledge(subject):
            return await _chat_core(req, conv_id, OrcReq, MsgRole, t_start)
    finally:
        _lane.release()


async def _chat_core(req, conv_id: str, OrcReq, MsgRole, t_start: float):
    # 1. 读取记忆上下文
    mem_ctx = await _memory.get_context(req.user_id, conv_id, query=req.message)

    # 2. 构建编排请求（含对话历史，用于意图识别上下文）
    history = [
        {"role": m.role.value, "content": m.content}
        for m in mem_ctx.recent_messages[-5:]
    ] if mem_ctx.recent_messages else None

    intent_result = await _orchestrator.recognize_intent(req.message, history=history)

    # 澄清式反问（SAFETYMIND_CLARIFY，默认开）：BGE 直出落在低置信带 [tau, 上限) 时，
    # 该带实测判对率仅 77-87%（test 全量分带统计），用一个本地模板反问替代可能答非所问的
    # 完整生成。次轮用户重述/选边后重走识别（重述通常置信度升高直达；简答 A/B 时因置信
    # 不足落入 LLM 链且带 history，可正确解析）。紧急消息与交互类意图永不反问。
    _CLARIFY_SPECIFIC = {
        "equipment_alarm", "equipment_maintenance", "hazard_report",
        "hazard_inspection", "incident_report", "emergency_response",
        "work_permit", "regulation_query", "chemical_safety", "training_cert",
        "inspection_audit", "occupational_health", "ppe_inquiry", "safety_document",
    }
    _clarify_on = os.getenv("SAFETYMIND_CLARIFY", "1") == "1"
    _clarify_hi = float(os.getenv("SAFETYMIND_CLARIFY_MAX_CONF", "0.55"))
    _tau_now = float(os.getenv("SAFETYMIND_BGE_TAU", "0.80"))
    # 事实型问题跳过"先反问"（2026-10-29，**默认开启**）：问数值/条款时反问只会让用户
    # 多点一次。控温 A/B（6 题×3 次）：澄清率 17%→0%、事实覆盖 0.56→0.70、
    # 配对 accuracy +0.133 / concrete +0.20 / safety 0；未控温那轮 accuracy 是 −0.267，
    # 说明单靠小样本噪声会给出相反结论（见 benchmarks/eval_clarify_factual_ab.py）。
    from core.intent_recognizer import is_factual_question as _is_factual
    _skip_factual = os.getenv("SAFETYMIND_CLARIFY_SKIP_FACTUAL", "1") == "1"
    from core.intent_recognizer import UrgencyLevel as _UL
    if (
        _clarify_on
        and intent_result.source_scores.get("laya_fused")
        and _tau_now <= intent_result.confidence < _clarify_hi
        and intent_result.urgency != _UL.CRITICAL
        and intent_result.intent.value in _CLARIFY_SPECIFIC
        and (intent_result.alt_intent or "") in _CLARIFY_SPECIFIC
        and intent_result.alt_confidence >= 0.30
        and not (_skip_factual and _is_factual(req.message))
    ):
        from core.laya_recognizer import ZH_KEY
        _v2zh = {v: k for k, v in ZH_KEY.items()}
        a_zh = _v2zh.get(intent_result.intent.value, intent_result.intent.value)
        b_zh = _v2zh.get(intent_result.alt_intent, intent_result.alt_intent)
        clarify_text = (
            f"您的问题可能涉及两类场景，为了答复准确请确认：\n\n"
            f"**A. {a_zh}** —— 关于「{req.message[:30]}」按 {a_zh} 处理流程回答；\n"
            f"**B. {b_zh}** —— 按 {b_zh} 的要求与规范回答。\n\n"
            f"回复 A 或 B，或直接补充一句说明（例如具体场所/作业/环节）。"
        )
        _rid = f"clr-{uuid.uuid4().hex[:12]}"
        await _memory.add_message(req.user_id, conv_id, MsgRole.USER, req.message)
        await _memory.add_message(req.user_id, conv_id, MsgRole.ASSISTANT, clarify_text)
        if _conv_store is not None:
            try:
                _conv_store.append_message(req.user_id, conv_id, "user", req.message)
                _conv_store.append_message(req.user_id, conv_id, "assistant", clarify_text)
            except Exception as ex:
                logger.warning(f"澄清轮会话落盘失败: {ex}")
        if _audit is not None:
            try:
                _audit.audit_request(_rid, req.user_id, conv_id, req.message,
                                     intent_result.intent.value, intent_result.urgency.value,
                                     False, (time.perf_counter() - t_start) * 1000,
                                     degraded=False, knowledge_used=False)
            except Exception as ex:
                logger.warning(f"澄清轮审计留痕失败: {ex}")
        return ChatResponse(
            conv_id=conv_id,
            response=clarify_text,
            intent=intent_result.intent.value,
            intent_group=intent_result.intent_group,
            agent_type="general",
            agent_types=["general"],
            primary_agent="general",
            supporting_agents=[],
            routing_reason=f"意图歧义澄清（置信 {intent_result.confidence:.2f}，次选 {intent_result.alt_intent} {intent_result.alt_confidence:.2f}）",
            routing_confidence=intent_result.confidence,
            escalated=False,
            latency_ms=round((time.perf_counter() - t_start) * 1000, 1),
            total_ms=round((time.perf_counter() - t_start) * 1000, 1),
            knowledge_used=False,
            entities=intent_result.entities,
            intent_confidence=round(intent_result.confidence, 4),
            intent_source_scores={**intent_result.source_scores,
                                  "clarify_alt": intent_result.alt_confidence},
            request_id=_rid,
            subject_id=_subject_id_of(req),        # S-05：多用户形态回服务端主体 id
        )

    # RAG 门控（意图级 + 紧急度级）：寒暄/反馈/转人工/无关意图不检索；
    # CRITICAL 且应急模板开启时同样跳过 RAG——模板不使用知识库。
    # 注意与 SAFETYMIND_CRITICAL_TEMPLATE 联动：模板关闭时紧急消息仍需 RAG 上下文走 LLM。
    from core.intent_recognizer import UrgencyLevel
    knowledge_text, knowledge_used, citations = "", False, []
    tool_direct_text, tool_direct_trace, data_direct = "", [], None
    _template_on = os.getenv("SAFETYMIND_CRITICAL_TEMPLATE", "1") == "1"

    # ── 精确匹配回复缓存（2026-10-28）：命中则跳过 RAG + 生成（省 30s+）。
    # 只做规范化后的精确匹配（不做语义匹配，防"乙醇/甲醇"误命中）；
    # key 含 intent + model + 知识库版本，库改即失效。详见 core/response_cache.py。
    from core.response_cache import CACHE as _rcache
    _model_name = os.getenv("ANTHROPIC_MODEL", "")
    _cached = _rcache.get(req.message, intent_result.intent.value, _model_name)
    if _cached is not None:
        logger.info(f"回复缓存命中: {req.message[:30]} age={_cached.get('cache_age_s')}s")

    if _cached is None and not (_template_on and intent_result.urgency == UrgencyLevel.CRITICAL):
        # P0-B：命中片段与邻近上下文进入注入（不再是"父文档开头 600 字"）
        knowledge_text, knowledge_used, citations = await _build_knowledge_context(
            req.message, intent=intent_result.intent)
        # P0-A：明确资源查询（CAS/化学品名）先做确定性工具调用，证据一定进入回答
        tool_direct_text, tool_direct_trace, data_direct = await _direct_resource_evidence(req.message)
        knowledge_used = bool(knowledge_used or tool_direct_text)
    else:
        data_direct = None
    evidence_text = "\n\n".join(p for p in (knowledge_text, tool_direct_text) if p)
    context_parts = [mem_ctx.to_prompt_text()]
    if evidence_text:
        context_parts.append(evidence_text)
    full_context = "\n\n".join(part for part in context_parts if part)

    # ── 确定性查询前置分流（2026-10-26 目标①）────────────────────────────
    # 纯化学品参数查询 + 资源库命中 → 直接模板化直出，跳过两轮 LLM 往返。
    # 实测依据：工具类 ~16.3s 中约 9.3s 是生成往返；MSDS 直出 <1s。
    # 质量门禁见 _is_pure_chemical_lookup（宁可不加速也不能答偏）。
    # SAFETYMIND_DETERMINISTIC_FASTPATH=0 可整体关闭。
    fast_result = None
    if (_cached is None
            and data_direct is not None
            and os.getenv("SAFETYMIND_DETERMINISTIC_FASTPATH", "1") == "1"
            and intent_result.urgency != UrgencyLevel.CRITICAL
            and _is_pure_chemical_lookup(req.message)):
        fast_reply = _deterministic_fast_reply(req.message, data_direct)
        if fast_reply:
            from agents.agent_orchestrator import AgentType as _FastAgentType
            from agents.agent_orchestrator import OrchestratorResult as _FastOrcResult
            fast_result = _FastOrcResult(
                request_id=f"det-{uuid.uuid4().hex[:12]}",
                response=fast_reply,
                agent_type=_FastAgentType.GENERAL,
                intent=intent_result.intent,
                escalated=False,
                latency_ms=round((time.perf_counter() - t_start) * 1000, 1),
                agent_types=[_FastAgentType.GENERAL],
                primary_agent=_FastAgentType.GENERAL,
                supporting_agents=[],
                routing_reason="确定性查询直出（资源库 MSDS 命中，未调用生成模型）",
                routing_confidence=1.0,
                tool_trace=tool_direct_trace,
            )
            logger.info(f"确定性快路径直出: {req.message[:40]} latency={fast_result.latency_ms}ms")

    if _cached is not None:
        # 从缓存重建结果对象：字段与 OrchestratorResult 一一对应，
        # 其余后处理（记忆/审计/会话/工单闸门/响应装配）照常执行，故留痕不缺席。
        from agents.agent_orchestrator import OrchestratorResult as _OrcResult
        result = _OrcResult(
            request_id=f"cache-{uuid.uuid4().hex[:12]}",
            response=_cached["response"],
            agent_type=_cached["agent_type"],
            intent=intent_result.intent,
            escalated=bool(_cached.get("escalated")),
            latency_ms=round((time.perf_counter() - t_start) * 1000, 1),
            agent_types=_cached.get("agent_types") or [],
            primary_agent=_cached.get("primary_agent"),
            supporting_agents=_cached.get("supporting_agents") or [],
            routing_reason=_cached.get("routing_reason") or "回复缓存命中（精确匹配，跳过 RAG 与生成）",
            routing_confidence=float(_cached.get("routing_confidence") or 1.0),
            tool_trace=_cached.get("tool_trace") or [],
        )
        citations = _cached.get("citations") or []
        knowledge_used = bool(_cached.get("knowledge_used"))
    elif fast_result is not None:
        result = fast_result
    else:
        orch_req = OrcReq(
            message=req.message,
            user_id=req.user_id,
            conv_id=conv_id,
            context=full_context,      # 兜底文本（评测/CLI 链路同款），员工层在结构化记忆缺失时使用
            memory_context=mem_ctx,    # 员工层（EmployeeAgent）读取结构化记忆统一装配
            knowledge=evidence_text,   # RAG 检索块 + 确定性资源查询证据，同交员工层装配
            history=history,
            entities=intent_result.entities,
            intent=intent_result.intent,
            intent_group=intent_result.intent_group,
            urgency=intent_result.urgency,
            intent_confidence=intent_result.confidence,
        )

        # 3. 执行
        result = await _orchestrator.run(orch_req)
        # 写入缓存：仅成功的知识类回答（put 内部还会过滤空/降级/非缓存意图）
        try:
            _rcache.put(req.message, intent_result.intent.value, _model_name, {
                "response": result.response,
                "agent_type": result.agent_type,
                "agent_types": list(result.agent_types or []),
                "primary_agent": result.primary_agent,
                "supporting_agents": list(result.supporting_agents or []),
                "routing_reason": result.routing_reason,
                "routing_confidence": result.routing_confidence,
                "escalated": bool(result.escalated),
                "tool_trace": list(getattr(result, "tool_trace", None) or []),
                "citations": list(citations or []),
                "knowledge_used": bool(knowledge_used),
                "degraded": ("抱歉，处理您的请求时出现问题" in (result.response or "")
                             or "工具调用未在预算内完成" in (result.response or "")),
            })
        except Exception as _cex:
            logger.warning(f"回复缓存写入失败（不影响响应）: {_cex}")

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
                # 需求简报（五阶段回路 brief 阶段）：升级现场一次性落库，
                # intent/urgency/实体/消息摘要/routing_reason/request_id 齐备，
                # 后续研讨/执行/反馈/验收都在此简报之上展开。
                msg_digest = " ".join(req.message.split())[:80]
                brief = {
                    "intent": intent_result.intent.value if intent_result.intent else "other",
                    # UrgencyLevel.value 是整数（1-4），简报存可读名（critical 等）
                    "urgency": (intent_result.urgency.name.lower()
                                if intent_result.urgency else "low"),
                    "entities": intent_result.entities or {},
                    "message_digest": msg_digest,
                    "routing_reason": result.routing_reason,
                    "request_id": result.request_id,
                }
                ticket_id = _audit.create_ticket(
                    f"应急升级: {msg_digest[:60]}", type_="emergency", priority="critical",
                    source_request_id=result.request_id, brief=brief,
                    # 值班通知入 outbox：与建单同事务落库（进程退出后 worker 仍会投递）；
                    # 原"建单时临时任务直发 webhook"由后台 worker 取代，语义等价且更可靠。
                    # "已实收"时间线只由 _on_outbox_sent 在 sent 后补写（未配置渠道不写）。
                    notification={
                        "idempotency_key": f"upgrade:{upgrade_id}:webhook",
                        "channel": os.getenv("ESCALATION_WEBHOOK_URL", "").strip(),
                        "payload": {
                            "request_id": result.request_id,
                            "text": f"【SafetyMind 应急升级】编号 {result.request_id}\n"
                                    f"内容: {msg_digest[:60]}\n"
                                    f"时间: {time.strftime('%m-%d %H:%M:%S')}\n"
                                    f"已自动建单并留痕，请值班确认。",
                            "timeline": {"note": "值班 webhook 已实收（需求简报建单时发出）",
                                         "to_stage": "brief", "request_id": result.request_id},
                        },
                        "request_id": result.request_id,
                        "upgrade_id": upgrade_id,
                    })
        except Exception as ex:
            logger.warning(f"审计/升级记录失败: {ex}")

    # 7. 隐患工单闸门（分层触发：隐患类意图 × HIGH+ 自动建单 / × MEDIUM 确认卡建议）。
    #    应急升级（escalated）已在上面建 emergency 工单，闸门直接跳过防重复。
    #    独立 try/except 降级——闸门任何失败只告警，绝不阻塞对话（仓库降级铁律）。
    hazard_action, hazard_ticket_id, hazard_suggestion = "none", 0, None
    if _audit is not None and not result.escalated:
        try:
            from core import hazard_gate
            decision = hazard_gate.decide(
                intent_result.intent.value if intent_result.intent else "other",
                intent_result.urgency.value if intent_result.urgency else 1,
                intent_result.entities or {}, req.message, False)
            if decision is not None:
                msg_digest_hz = " ".join(req.message.split())[:80]
                brief_hz = {
                    "intent": intent_result.intent.value if intent_result.intent else "other",
                    "urgency": (intent_result.urgency.name.lower()
                                if intent_result.urgency else "low"),
                    "entities": intent_result.entities or {},
                    "message_digest": msg_digest_hz,
                    "routing_reason": result.routing_reason,
                    "request_id": result.request_id,
                    "severity": decision.get("severity", ""),
                    "category": decision.get("category", ""),
                    "basis": decision.get("basis", ""),
                    "dept": decision.get("dept", ""),
                }
                if decision["action"] == "auto":
                    hazard_ticket_id, merged_hz = _create_hazard_ticket(
                        _audit, decision, brief_hz, result.request_id,
                        req.user_id, actor="system")
                    hazard_action = "merged" if merged_hz else "auto_ticket"
                    _enqueue_ticket_notification(
                        hazard_ticket_id, result.request_id,
                        f"【SafetyMind 隐患工单】编号 {result.request_id}\n"
                        f"{decision.get('summary', '')}\n"
                        f"级别: {'重大' if decision.get('severity') == 'major' else '一般'}"
                        f" · 派发: {decision.get('dept', '安环部')}"
                        f" · 限期: {decision.get('deadline_hours', '')} 小时\n"
                        f"时间: {time.strftime('%m-%d %H:%M:%S')}\n"
                        f"高危隐患自动建单，请责任部门限期整改。")
                else:  # suggest：暂存建议，用户点确认卡才建单（24h 内有效）
                    _audit.save_hazard_suggestion(
                        result.request_id, req.user_id, conv_id,
                        {"decision": decision, "brief": brief_hz})
                    hazard_action = "suggested"
                    hazard_suggestion = {
                        "summary": decision.get("summary", ""),
                        "severity": decision.get("severity", "general"),
                        "dept": decision.get("dept", ""),
                        "deadline_hours": decision.get("deadline_hours", 0),
                        "basis": decision.get("basis", ""),
                    }
        except Exception as ex:
            logger.warning(f"隐患工单闸门失败（不影响对话）: {ex}")

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
        escalation_basis=getattr(result, "escalation_basis", ""),
        latency_ms=round(result.latency_ms, 1),
        # 全链路耗时（含排队/记忆/意图/RAG；latency_ms 只覆盖 orchestrator.run，
        # 线上实测两者可差 3-4 倍，监控与前端都需要真实总耗时）
        total_ms=round((time.perf_counter() - t_start) * 1000, 1),
        knowledge_used=knowledge_used,
        entities=intent_result.entities,
        intent_confidence=round(intent_result.confidence, 4),
        intent_source_scores=intent_result.source_scores,
        request_id=result.request_id,
        hazard_action=hazard_action,
        hazard_ticket_id=hazard_ticket_id,
        hazard_suggestion=hazard_suggestion,
        citations=citations,                                        # P0-B 证据引用
        tools_used=tool_direct_trace + _tool_trace_public(getattr(result, "tool_trace", None)),
        subject_id=_subject_id_of(req),                             # S-05 服务端主体 id
    )


# ── 紧急快速分流（零 LLM/零记忆/零 RAG）：有界独立闸门 + 有上限的整体超时 + 副作用待补降级 ──
_EMERGENCY_INTENT = {"emergency": "emergency_response", "incident": "incident_report"}


async def _emergency_acquire_gate() -> bool:
    """尝试获取应急独立闸门：拿不到容量返回 False（绝不 429、绝不排队等主链路）。"""
    if EMERGENCY_QUEUE_TIMEOUT_S <= 0:
        return False
    try:
        await asyncio.wait_for(_EMERGENCY_GATE.acquire(), timeout=EMERGENCY_QUEUE_TIMEOUT_S)
        return True
    except asyncio.TimeoutError:
        return False


def _emergency_conv_id(req) -> str:
    return req.conv_id or str(uuid.uuid4())


async def _emergency_step(fn, *args):
    """快速分流副作用单步执行：线程内跑同步存储调用 + 短超时；失败/超时只告警不抛出。"""
    budget = float(os.getenv("SAFETYMIND_EMERGENCY_STEP_TIMEOUT_S", "0.5"))
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=budget)
    except Exception as ex:
        logger.warning(f"快速分流副作用步骤失败: {type(ex).__name__}: {ex}")
        return None


def _outbox_notify_code(idempotency_key: str, channel: str) -> str:
    """按 outbox 实际状态回处置状态码：未配置渠道 → unconfigured；只有 sent/ack 才算送达/确认。"""
    if not channel:
        return "unconfigured"
    if _audit is None:
        return "unknown"
    try:
        row = next((r for r in _audit.list_notifications(limit=200)
                    if r["idempotency_key"] == idempotency_key), None)
    except Exception:
        return "queued"
    if row is None:
        return "queued"
    return {"ack": "acked", "sent": "sent", "failed": "failed"}.get(row.get("status"), "queued")


def _emergency_ticket_and_outbox(req, sig, rid: str, intent_value: str,
                                 status: Dict[str, Any]) -> bool:
    """升级事件 + 应急工单 + outbox 行（幂等：同一上报编号不重复建单）。

    建单与待发送记录在同一次持锁/同一次 commit 内落库；未配置渠道仍入队。
    """
    webhook = os.getenv("ESCALATION_WEBHOOK_URL", "").strip()
    key = f"emergency:{rid}:webhook"
    existing = _audit.find_ticket_by_request(rid, "emergency")
    if existing:
        status["ticket_id"] = int(existing)
        status["notify"] = _outbox_notify_code(key, webhook)
        return True
    upgrade_id = _audit.add_upgrade(rid, req.user_id, req.conv_id or "", req.message, intent_value)
    digest = " ".join(str(req.message or "").split())[:80]
    ticket_id = _audit.create_ticket(
        f"应急升级: {digest[:60]}", type_="emergency", priority="critical",
        source_request_id=rid,
        brief={"intent": intent_value, "urgency": "critical",
               "entities": emergency.entities(req.message),
               "message_digest": digest,
               "routing_reason": "紧急预检快速分流（零 LLM）",
               "request_id": rid},
        notification={
            "idempotency_key": key,
            "channel": webhook,
            "payload": {
                "request_id": rid,
                "text": f"【SafetyMind 应急升级】编号 {rid}\n内容: {digest[:60]}\n"
                        f"时间: {time.strftime('%m-%d %H:%M:%S')}\n"
                        f"紧急预检已自动建单并留痕，请值班确认。",
                "timeline": {"note": "值班 webhook 已实收（紧急预检快速分流建单时发出）",
                             "to_stage": "brief", "request_id": rid},
            },
            "request_id": rid,
            "upgrade_id": upgrade_id,
        })
    status["ticket_id"] = int(ticket_id)
    status["notify"] = _outbox_notify_code(key, webhook)
    return True


async def _emergency_record_bg(user_id: str, conv_id: str, message: str, response: str,
                               escalated: bool, request_id: str) -> None:
    """快速分流的会话历史/记忆写入（可选，不阻塞返回；任何失败只告警）。"""
    try:
        if _memory is not None:
            from memory.conversation_memory import MsgRole
            await _memory.add_message(user_id, conv_id, MsgRole.USER, message)
            await _memory.add_message(user_id, conv_id, MsgRole.ASSISTANT, response)
    except Exception as ex:
        logger.warning(f"快速分流记忆写入失败: {ex}")
    try:
        if _conv_store is not None:
            _conv_store.append_message(user_id, conv_id, "user", message)
            _conv_store.append_message(user_id, conv_id, "assistant", response,
                                       escalated=escalated, request_id=request_id)
    except Exception as ex:
        logger.warning(f"快速分流会话落盘失败: {ex}")


def _emergency_response(req, sig, status: Dict[str, Any], t0: float, reason: str) -> ChatResponse:
    """由处置状态区组装快速分流响应（latency/total 为真实耗时，编号回填）。"""
    kind = sig.get("kind", "emergency")
    ms = round((time.perf_counter() - t0) * 1000, 1)
    text = emergency.minimal_response(kind, status)
    resp = ChatResponse(
        conv_id=_emergency_conv_id(req),
        response=text,
        intent=_EMERGENCY_INTENT.get(kind, "emergency_response"),
        intent_group="compliance",
        agent_type="escalation",
        agent_types=["escalation"],
        primary_agent="escalation",
        supporting_agents=[],
        routing_reason=reason,
        routing_confidence=1.0,
        escalated=True,
        latency_ms=ms,
        total_ms=ms,
        knowledge_used=False,
        entities=emergency.entities(req.message),
        request_id=status["request_id"],
        emergency_status=emergency.status_lines(status),
        emergency_fastpath=True,
        urgency="critical",
        subject_id=_subject_id_of(req),                            # S-05 服务端主体 id
    )
    # 会话历史/记忆写入不阻塞返回（可选；失败只告警，绝不影响响应）
    if status.get("recorded"):
        asyncio.create_task(_emergency_record_bg(
            req.user_id, resp.conv_id, req.message, text, True, status["request_id"]))
    return resp


async def _emergency_fast_path(req, sig, t0: float, request_id: Optional[str] = None):
    """紧急快速分流：零 LLM/零记忆/零 RAG，整体有超时上限，任何失败只降级不抛出。"""
    timeout = float(os.getenv("SAFETYMIND_EMERGENCY_PATH_TIMEOUT_S", "2.0"))
    rid = request_id or f"emg-{uuid.uuid4().hex[:12]}"
    try:
        return await asyncio.wait_for(
            _emergency_fast_path_core(req, sig, t0, rid), timeout=timeout)
    except Exception as ex:                      # 含 asyncio.TimeoutError：绝不向调用方抛出
        logger.warning(f"紧急快速分流降级（{type(ex).__name__}: {ex}），编号 {rid} 标记记录待补")
        status = {"request_id": rid, "recorded": False, "ticket_id": 0, "notify": "unknown"}
        return _emergency_response(req, sig, status, t0,
                                   "紧急预检快速分流（零 LLM）：记录待补，未产生副作用")


async def _emergency_fast_path_core(req, sig, t0: float, rid: str):
    kind = sig.get("kind", "emergency")
    intent_value = _EMERGENCY_INTENT.get(kind, "emergency_response")
    conv_id = _emergency_conv_id(req)
    status: Dict[str, Any] = {"request_id": rid, "recorded": False, "ticket_id": 0, "notify": "unknown"}

    # 只在自身闸门内产生副作用：拿不到容量（或存储未启用）时降级为"记录待补"，
    # 仍返回最小响应——绝不 429、绝不阻塞主链路。
    acquired = await _emergency_acquire_gate()
    try:
        if not acquired:
            logger.warning(f"应急快速通道容量不足，上报 {rid} 记录待补")
        elif _audit is None:
            logger.warning(f"审计存储未启用，上报 {rid} 记录待补")
        else:
            latency_ms = (time.perf_counter() - t0) * 1000

            def _audit_log():
                _audit.audit_request(rid, req.user_id, conv_id, req.message,
                                     intent_value, "critical", True, latency_ms,
                                     degraded=False, knowledge_used=False)
                return True

            status["recorded"] = bool(await _emergency_step(_audit_log))
            if status["recorded"]:
                if not await _emergency_step(_emergency_ticket_and_outbox,
                                             req, sig, rid, intent_value, status):
                    status["ticket_id"] = status["ticket_id"] or 0
    finally:
        if acquired:
            _EMERGENCY_GATE.release()

    return _emergency_response(req, sig, status, t0,
                               "紧急预检快速分流（零 LLM）：本地规则命中紧急表述，先于普通对话闸门处理")


async def _build_knowledge_context(message: str, intent=None, top_k: int = 2) -> tuple:
    """
    为 /chat 主链路构建 RAG 知识上下文（P0-B：命中条款进入回答并保留引用）。

    直连 kb.search_async（bge 嵌入+余弦，实测几十毫秒）：/chat 每多一跳 LLM 都是秒级
    （限流下单次改写链路可达半分钟），主链路跳过查询改写与 LLM 重排两跳；
    完整改写链路保留给 /search 演示端点。

    证据保真（S-03 修复）：注入的是**真正命中的子块 snippet**及其邻近上下文，
    而不是"父文档开头前 600 字"——关键条款位于长规程尾部时也能进入生成请求。
    返回 (文本, 是否使用知识库, citations)。
    """
    if _tool_manager is None:
        return "", False, []
    if not _should_use_knowledge(message, intent=intent):
        return "", False, []

    parts: List[str] = []
    citations: List[Dict[str, Any]] = []
    used = False

    # ── 既有知识库通道（ChromaDB 父子召回）───────────────────────────────────
    try:
        tool = _tool_manager._tools.get("knowledge_search")
        if tool is not None:
            kb = tool.handler.__self__
            result = await kb.search_async(message, top_k=top_k)
            # 分数过滤（2026-10-29 实测）：未过滤时 precision@3 仅 19.7%，
            # 且存在**负分**片段进入 prompt（实测 min=-0.1998）——负分=比随机还不相关，
            # 注入只会误导模型并白烧 token。默认丢弃 <=阈值的片段。
            # SAFETYMIND_RAG_MIN_SCORE 可调（默认 0.0：丢负分与零分）。
            _min_score = float(os.getenv("SAFETYMIND_RAG_MIN_SCORE", "0") or 0)
            if isinstance(result, list) and result and _min_score > -99:
                _kept = []
                for _it in result:
                    try:
                        _sc = float((_it or {}).get("score"))
                    except (TypeError, ValueError):
                        _kept.append(_it)      # 无分数（旧数据）不参与过滤
                        continue
                    if _sc > _min_score:
                        _kept.append(_it)
                if len(_kept) != len(result):
                    logger.info(f"RAG 分数过滤: {len(result)}→{len(_kept)} 条"
                                f"（阈值 {_min_score}）")
                result = _kept
            if isinstance(result, list) and result:
                per_item_cap = int(os.getenv("SAFETYMIND_KNOWLEDGE_ITEM_CHARS", "1200"))
                kb_lines: List[str] = []
                for i, item in enumerate(result[:top_k], start=1):
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title", "未命名文档"))
                    snippet = str(item.get("snippet") or "").strip()
                    ctx = str(item.get("context_blob") or "").strip()
                    content = str(item.get("content", "")).strip()
                    # 命中子块优先；旧数据无 snippet 时退回父块前部（不使旧数据失效）
                    hit = snippet or content
                    if not hit:
                        continue
                    section = str(item.get("section") or "")
                    score = item.get("score", "")
                    loc = f"{title}{('·' + section) if section else ''}"
                    body = f"命中片段: {hit[:per_item_cap]}"
                    if snippet and ctx:
                        body += f"\n邻近上下文: {ctx[:per_item_cap]}"
                    used = True
                    kb_lines.append(f"{i}. 标题: {loc}\n   相关度: {score}\n   {body}")
                    citations.append({
                        "index": i,
                        "title": title,
                        "doc_id": str(item.get("doc_id") or item.get("parent_id") or ""),
                        "chunk_id": str(item.get("chunk_id") or ""),
                        "content_hash": str(item.get("content_hash") or ""),
                        "section": section,
                        "score": score,
                        "source": str(item.get("source") or ""),
                        "version": str(item.get("version") or ""),
                        "status": str(item.get("status") or ""),
                        "chunk_index": item.get("chunk_index", -1),
                        "total_chunks": item.get("total_chunks", -1),
                        # 引用展示用命中子块本身（不再用 300 字短摘要——命中落在子块尾部时会被截掉）
                        "snippet": str(item.get("snippet") or item.get("snippet_short") or hit)[:600],
                        # 只有目录记录、没有可引用正文时，不得宣称已读取条款
                        "catalog_only": not bool(snippet),
                    })
                if kb_lines:
                    parts.append("[知识库检索结果]")
                    parts.extend(kb_lines)
    except Exception as ex:
        logger.warning(f"构建知识库上下文失败: {ex}")

    # ── K07：corpus 命中并入同一条注入链（正文块优先；目录卡只作定位提示）────
    c_text, c_used, c_cits = _corpus_knowledge_context(message, top_k=top_k,
                                                       start_index=len(citations))
    if c_used:
        parts.append(c_text)
        citations.extend(c_cits)
        used = True

    if not used:
        return "", False, []
    parts.append(
        "请优先依据以上知识库内容回答，并在引用条款处标注【依据N】；"
        "知识库内容不足时结合安全生产通用知识说明并提示需要核实，不得编造条款；"
        "catalog_only=true 的条目只有目录记录，不得声称已读到其正文条款。")
    # 引用序号按合并后的整体顺序重排（KB 与 corpus 两通道共用同一编号体系）
    for n, c in enumerate(citations, start=1):
        c["index"] = n
    return "\n".join(parts), True, citations


# ── corpus 注入闸门（2026-10-29 实测驱动，闭环第 33 轮）────────────────────────
# 【已确认缺陷，修复保留】_corpus_knowledge_context(top_k=…) 的 top_k 从未生效：
#   /chat 决定只注入 2 条时 corpus 仍塞 6 条（实测 7.57 引用/题）。现按 top_k 截断。
# 【噪声问题的候选闸门——默认关闭】实测（benchmarks/eval_rag_context_precision.py）：
#   corpus 通道 126 条引用被裁判判定 0 条相关（RRF 是**排名分**，无关块也拿 0.03；
#   向量通道永远返回最近邻，所以库里没有对应条目时照样注满 6 块）。
#   试过但不可行：余弦阈值（无关块 cos 最高 0.643，与相关块 0.52 量级重叠）、
#   FTS 命中即放行（单字词"门"命中率 100%，仍注满噪声）。
#   词面锚点规则（≥N 个非泛用查询词逐字命中）在**引用精度**上很好：162 个已标注
#   候选块 → 保留 7（5 无关 + 2 相关），相关召回 100%。
#   答案质量证据（两轮 A/B + 一项针对性核查，全部为真实 /chat）：
#     - 顺序 A/B（n=10，分时段各起一次应用）：accuracy −0.40、evidence −0.30、safety −0.35；
#     - 交错 A/B（n=10，同进程每题交替两口径，消除时段漂移）：accuracy −0.05、concrete +0.10、
#       safety **+0.25**、evidence −0.35 —— accuracy 的差距消失了，说明第一轮的 −0.40 主要是
#       时段混淆；evidence 的下降是**干预本身的机械结果**（闸门移除引用 → 能引的东西更少），
#       不是答案变差。
#     - 错误依据核查（对同一批答案问裁判"有没有把不相关条款当依据"）：开闸门 0.25 条/答案
#       （2/8），关闸门 0.75 条/答案（3/4）——方向支持闸门，但样本小，只作辅助证据。
#   综上：把 0% 相关的片段当"依据"注入，在安全场景是正确性风险；两轮 A/B 未发现可检测的
#   质量下降（accuracy 两次分别 −0.40/−0.05，交错后≈0；safety 交错后 +0.25）。
#   → **默认启用**（`SAFETYMIND_CORPUS_MIN_TERM_HITS=2`）；置 0 可完全回退旧行为。
#   仍未做：生成侧温度不可控（API 默认 1.0）导致 n=10 的 A/B 属欠功效，结论按"方向一致 +
#   机制可解释"采信，不宣称"统计显著"。
_CORPUS_GENERIC_TERMS = {
    "安全", "作业", "要求", "检查", "隐患", "依据", "规范", "标准", "管理", "设置",
    "生产", "设备", "内容", "级别", "工作", "情况", "如何", "什么", "多少", "是否",
    "应该", "规定", "哪些", "怎么", "进行", "需要", "属于", "问题", "方面", "相关",
}


def _corpus_query_terms(message: str) -> List[str]:
    """取查询里可作**证据锚点**的词：≥2 字、非泛用词、去重。

    分词口径与 corpus FTS 完全一致（corpus.fts.tokenize_text），避免"索引端能切出来、
    闸门端切不出来"的口径漂移；jieba 不可用时退化为中文字符 n-gram。
    """
    try:
        from corpus.fts import tokenize_text
        raw = tokenize_text(message or "").split()
    except Exception:
        raw = re.findall(r"[\u4e00-\u9fff]{2,}", message or "")
    stop = set(_CORPUS_GENERIC_TERMS)
    extra = os.getenv("SAFETYMIND_CORPUS_TERM_STOPWORDS", "")
    stop.update(x.strip() for x in extra.split(",") if x.strip())
    seen: set = set()
    out: List[str] = []
    for t in raw:
        t = (t or "").strip()
        if len(t) < 2 or t in stop or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _corpus_term_hits(terms: List[str], text: str) -> int:
    """查询锚点词在片段正文中的逐字命中数。"""
    body = text or ""
    return sum(1 for t in terms if t in body)


def _gate_corpus_payload(message: str, blocks: List[Dict[str, Any]],
                         cards: List[Dict[str, Any]], top_k: int) -> tuple:
    """条数上限 +（可选）词面证据闸门。

    返回 (blocks, cards)。`SAFETYMIND_CORPUS_MIN_TERM_HITS`：
      - 2（**当前默认**）→ 至少 2 个查询锚点词逐字出现在片段里才注入
      - N  → 自定义阈值
      - 0  → 关闭词面过滤，只按 top_k 截断（完全回退旧行为）
    上限：正文块与目录卡各自不超过 top_k（旧实现里 top_k 参数实际未生效，
    导致 /chat 决定只注入 2 条时 corpus 仍塞 6 条）。
    """
    cap = max(0, int(top_k))
    try:
        min_hits = int(os.getenv("SAFETYMIND_CORPUS_MIN_TERM_HITS", "2"))
    except ValueError:
        min_hits = 2
    if min_hits <= 0:
        return blocks[:cap], cards[:cap]
    terms = _corpus_query_terms(message)
    if not terms:
        return [], []
    kept_b = [b for b in blocks
              if _corpus_term_hits(terms, str(b.get("content") or "")) >= min_hits]
    kept_c = [c for c in cards
              if _corpus_term_hits(terms, str(c.get("text") or "")) >= min_hits]
    return kept_b[:cap], kept_c[:cap]


def _corpus_knowledge_context(message: str, top_k: int = 3,
                              start_index: int = 0) -> tuple:
    """K07：把 corpus（K03 统一导入库）命中并入注入链。

    - 正文块（evidence_kind=body）进入注入文本与 citations（additive 字段
      evidence_kind / version_id / build_id，复用 S-03 引用结构）；
    - 目录卡（evidence_kind=catalog）**只作定位提示**，citations 里标注
      catalog_only=True，绝不以正文形式进入引用。
    返回 (文本, 是否使用, citations)。
    """
    try:
        from mcp.knowledge_base import SCOPES_CONTEXT, run_corpus_provider
    except Exception:
        return "", False, []
    data = run_corpus_provider(message, SCOPES_CONTEXT.get())
    if not isinstance(data, dict):
        return "", False, []
    build = data.get("build") or {}
    build_id = str(build.get("build_id") or "")
    blocks = [b for b in (data.get("blocks") or []) if isinstance(b, dict)]
    cards = [c for c in (data.get("cards") or []) if isinstance(c, dict)]
    if not blocks and not cards:
        return "", False, []
    # 条数上限 +（可选）词面证据闸门（默认只截断，见 _gate_corpus_payload 上方注释）
    blocks, cards = _gate_corpus_payload(message, blocks, cards, top_k)
    if not blocks and not cards:
        return "", False, []

    lines: List[str] = ["[资料库检索结果]"]
    citations: List[Dict[str, Any]] = []
    idx = int(start_index)
    for b in blocks:
        idx += 1
        title = str(b.get("title") or "未命名资料")
        content = str(b.get("content") or "")
        lines.append(f"{idx}. 标题: {title}\n   相关度: {b.get('score', '')}\n"
                     f"   命中片段: {content[:1200]}")
        citations.append({
            "index": idx, "title": title,
            "doc_id": str(b.get("version_id") or ""),
            "chunk_id": str(b.get("block_id") or ""),
            "content_hash": "", "section": "",
            "score": b.get("score", ""), "source": "corpus",
            "version": str(b.get("version_id") or ""),
            "status": str(b.get("status") or ""),
            "snippet": content[:600],
            "catalog_only": False,
            # additive：corpus 身份（build 可追溯，"同 build 检出"）
            "evidence_kind": "body",
            "version_id": str(b.get("version_id") or ""),
            "build_id": build_id,
            "access_scope": str(b.get("access_scope") or ""),
        })
    for c in cards:
        idx += 1
        title = str(c.get("title") or "未命名资料")
        lines.append(f"{idx}. 标题: {title}（仅目录：{c.get('note', '')}）\n"
                     f"   相关度: {c.get('score', '')}\n   {str(c.get('text') or '')[:400]}")
        citations.append({
            "index": idx, "title": title,
            "doc_id": str(c.get("version_id") or ""),
            "chunk_id": str(c.get("card_id") or ""),
            "content_hash": "", "section": "",
            "score": c.get("score", ""), "source": "corpus",
            "version": str(c.get("version_id") or ""), "status": "",
            "snippet": str(c.get("text") or "")[:600],
            "catalog_only": True,
            # 目录卡绝不以正文形式进入引用
            "evidence_kind": "catalog",
            "version_id": str(c.get("version_id") or ""),
            "build_id": build_id,
            "access_scope": str(c.get("access_scope") or ""),
        })
    lines.append(
        "以上资料库命中：evidence_kind=body 为正文证据（build_id 可追溯同一次构建）；"
        "evidence_kind=catalog / catalog_only=true 的条目仅目录，不得当作条款正文引用。")
    return "\n".join(lines), True, citations


def _should_use_knowledge(message: str, intent=None) -> bool:
    """跳过纯寒暄，安全业务类问题才检索知识库，避免无关 RAG 干扰回复。

    ── 创作类跳过检索（2026-10-26 实测驱动）────────────────────────────────
    实测（docs/PERF_AND_RAG_DECISIONS.md，六类真实任务 n=3）：
      A 纯模型平均 22193ms vs B 带 RAG 平均 14206ms —— **RAG 平均快 36%**，
      因为模型读资料转述比"回忆+组织"更快。唯一变慢的是**创作类任务**：
      "帮我写一条隐患排查记录" A 9484ms → B 16266ms（**慢 6.8s**）。
      原因：创作不需要检索，注入资料反而增加上下文负担。

    所以这里对 `safety_document`（写台账/预案/报告这类创作意图）做**双重判定**：
      - 用户明确要"写/生成/起草"某文档 → 跳过检索（创作不需要资料）
      - 只是"要一份文档模板/清单" → 仍检索（模板本身就在库里）
    `SAFETYMIND_SKIP_RAG_FOR_WRITING=0` 可关闭此逻辑。
    """
    msg = (message or "").strip().lower()
    if not msg:
        return False
    intent_value = getattr(intent, "value", intent)

    # 创作类意图的细分判定（在原有 allow 列表之前短路）
    if (intent_value == "safety_document"
            and os.getenv("SAFETYMIND_SKIP_RAG_FOR_WRITING", "1") == "1"):
        writing_kw = ("写", "生成", "起草", "拟一", "帮我做一份", "编制", "填写")
        template_kw = ("模板", "清单", "表格", "样式", "范文", "样例", "要一份")
        wants_write = any(k in msg for k in writing_kw)
        wants_template = any(k in msg for k in template_kw)
        if wants_write and not wants_template:
            return False     # 纯创作：跳过检索，省 6.8s

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


# ── P0-A 确定性资源查询与工具轨迹（放在检索/紧急段之后：紧急快速分流段内不得出现
#    _tool_manager 等依赖，见 tests/test_emergency_fastpath.py 的静态切片断言）──
async def _direct_resource_evidence(message: str) -> tuple:
    """P0-A 确定性资源查询：命中 CAS 号或已知化学品名时先直接调 msds_lookup。

    对"明确资源查询"不依赖模型是否决定调工具——保证 A01 类场景下证据一定进入回答；
    开放需求仍由执行器的有界模型工具选择处理。返回 (注入文本, 工具轨迹, 结构化数据)。
    第三项 data 供确定性快路径（_deterministic_fast_reply）直出使用；无命中为 None。
    资源不存在时明确输出缺失状态，禁止用模型补造 SDS/CAS/条款。
    """
    if _tool_manager is None:
        return "", [], None
    msg = message or ""
    cas_match = re.search(r"\d{2,7}-\d{2}-\d", msg)
    cas = cas_match.group(0) if cas_match else ""
    try:
        from core.intent_recognizer import _CHEMICAL_VOCAB
    except Exception:
        _CHEMICAL_VOCAB = []
    name = next((c for c in _CHEMICAL_VOCAB if c in msg), "")
    if not cas and not name:
        return "", [], None
    params: Dict[str, Any] = {"action": "lookup"}
    if cas:
        params["cas"] = cas
    if name:
        params["name"] = name
    try:
        res = await _tool_manager.call("msds_lookup", params, {"source": "chat-direct"})
    except Exception as ex:
        logger.warning(f"确定性资源查询失败: {ex}")
        return "", [], None
    trace = [{"name": "msds_lookup", "ok": bool(res.success), "error": res.error,
              "latency_ms": round(float(res.latency_ms or 0.0), 1), "deterministic": True}]
    target = "/".join(filter(None, [name, cas]))
    if not res.success or not isinstance(res.data, dict):
        return (f"[资源库确定性查询] 未能读取“{target}”的 MSDS（工具失败：{res.error}），"
                f"此部分资料缺失，不得编造 SDS 条款。"), trace, None
    data = res.data
    if data.get("found") is False:
        return (f"[资源库确定性查询] 资源库中没有“{target}”的 MSDS 记录，属于资料缺口，"
                f"需补充资料，不得编造 SDS 条款。"), trace, data
    sections = data.get("sections") or {}
    lines = []
    for key, val in list(sections.items())[:8]:
        if val:
            lines.append(f"{key}. {str(val)[:220]}")
    head = (f"[资源库确定性查询] 化学品 {data.get('name', name)}"
            f"（CAS {data.get('cas', cas)}）已命中资源库 MSDS，以下为真实记录：")
    return head + "\n" + "\n".join(lines), trace, data


# ── 确定性查询前置分流（2026-10-26 目标①，实测驱动的替代方案）────────────────
#
# 背景：工具类问答 ~16.3s 中约 9.3s 是第二轮 LLM 往返（把工具结果转述成自然语言）。
# 在 ToolExecutor 内部跳过第二轮曾实测并回退（破坏 5 条轮次契约，见
# core/tool_execution.py 注释）。替代设计：**在 /chat 层对"纯化学品参数查询"直出**——
# 资源库 MSDS 本就是结构化权威数据，模板化输出可读，不需要模型转述。
#
# 保守门禁（宁可不加速也不能答偏）：
#   1. 确定性查询必须**命中**资源库（found=True 且有章节）
#   2. 问题含查询意图词（闪点/沸点/CAS/性质…）
#   3. 问题**不含**处置/建议类词（怎么办/如何/措施…——那需要结合规程推理，必须走模型）
#   4. 长度 ≤60 字（长问题往往带上下文语境）
#   5. 非 CRITICAL（应急走零 LLM 模板，另有路径）
#
# 输出形态（对照上轮实验 B 路径"裸 JSON"的教训）：绝不输出原始 JSON，
# 逐节格式化 + 来源标注 + "未经模型改写"声明 + 追问引导。
_LOOKUP_KEYWORDS = ("闪点", "沸点", "熔点", "爆炸极限", "cas", "msds", "理化",
                    "性质", "特性", "密度", "分子式", "参数", "是什么")
_ADVISORY_KEYWORDS = ("怎么办", "怎么处理", "如何", "怎么", "建议", "措施", "方案",
                      "流程", "为什么", "区别", "混存", "能不能", "可以吗", "应该",
                      "应急处置", "处置", "危险吗", "风险", "泄漏了", "着火")


def _is_pure_chemical_lookup(message: str) -> bool:
    """判定是否为"纯化学品参数查询"——可由资源库直出、无需模型推理。"""
    msg = (message or "").strip()
    if not msg or len(msg) > 60:
        return False
    low = msg.lower()
    if any(k in low for k in _ADVISORY_KEYWORDS):
        return False
    return any(k in low for k in _LOOKUP_KEYWORDS)


def _deterministic_fast_reply(message: str, data: Dict[str, Any]) -> str:
    """把命中资源库的 MSDS 结构化数据格式化为用户可读回答（不经模型）。

    返回空串表示不适合直出，调用方回退到正常 LLM 链路。
    """
    if not isinstance(data, dict) or data.get("found") is False:
        return ""
    sections = data.get("sections") or {}
    lines = []
    for key, val in sections.items():
        if val:
            lines.append(f"{key}. {str(val).strip()[:260]}")
    if not lines:
        return ""
    name = data.get("name") or ""
    cas = data.get("cas") or ""
    head = f"{name}（CAS {cas}）的资源库查询结果："
    body = "\n".join(lines)[:1500]
    tail = ("\n\n以上内容直接来自资源库 MSDS 记录（未经模型改写，无编造风险）。"
            "如需结合本单位规程进一步解读处置措施，请继续追问。")
    return head + "\n" + body + tail


def _tool_trace_public(records) -> List[Dict[str, Any]]:
    """工具轨迹 → 对外精简结构（名称/成功/耗时/错误），不泄露完整参数与结果原文。"""
    out: List[Dict[str, Any]] = []
    for r in records or []:
        out.append({
            "name": str(getattr(r, "name", "") or ""),
            "ok": bool(getattr(r, "ok", False)),
            "error": getattr(r, "error", None),
            "latency_ms": round(float(getattr(r, "latency_ms", 0.0) or 0.0), 1),
        })
    return out


# ── 流式对话（2026-10-28）：/chat/stream ─────────────────────────────────────
# 设计取舍（如实说明）：**不做 token 级穿透**。多 Agent + 工具回路的生成发生在
# 员工层内部，要在那里接流式需要改造整条链路，且会让 /chat/stream 与 /chat
# 行为分叉（同样的问句走不同 prompt/工具策略）——那比慢更危险。
# 因此这里提供的是：
#   1. 快路径（紧急模板 / 缓存命中 / 确定性直出）**立刻**推送完整答案（<300ms）
#   2. 完整链路推送**阶段事件**（检索 / 生成），随后推送最终答案
# 用户侧收益：慢路径不再完全无反馈，快路径真正即时。
# token 级流式列为未做（这里不宣称已实现）。
@app.post("/chat/stream")
async def chat_stream(req: ChatRequest,
                      x_user_token: str = Header(default="", alias="X-User-Token")):
    """SSE 流式对话。事件：meta → (stage*) → delta → done。"""
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    async def gen():
        t0 = time.perf_counter()

        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        role_subject = _subject_for(x_user_token, req.user_id)
        _req = req
        if _isolation_enforced():
            _req = req.model_copy(update={"user_id": role_subject.subject_id})

        # ① 紧急预检：零 LLM，立刻给步骤（安全底线优先于一切流式体验）
        try:
            sig = emergency.precheck(_req.message) if os.getenv(
                "SAFETYMIND_EMERGENCY_FASTPATH", "1") == "1" else None
        except Exception:
            sig = None
        yield sse("meta", {"emergency": bool(sig), "message": _req.message[:60]})
        if sig is not None:
            try:
                resp = await _emergency_fast_path(_req, sig, t0=time.perf_counter())
                body = resp.model_dump() if hasattr(resp, "model_dump") else {}
            except Exception as ex:
                body = {"response": "紧急分流失败，请立即拨打 119/120 并按现场预案处置。",
                        "error": str(ex)[:120]}
            yield sse("delta", {"text": body.get("response") or ""})
            # done 事件带上**完整响应体**（additive，2026-10-29）：前端改用流式后仍需
            # escalated / citations / hazard* / conversation_id 等字段，只回摘要会让
            # "流式版"功能比"非流式版"少（升级卡、依据、建单卡都会丢）。
            yield sse("done", {**body,
                                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                                "path": "emergency"})
            return

        # ② 其余路径复用 /chat 的完整实现（保证行为一致），期间推送阶段事件
        from agents.agent_orchestrator import Request as OrcReq
        from memory.conversation_memory import MsgRole
        conv_id = _req.conv_id or str(uuid.uuid4())
        lane = _FAST_GATE if _is_pure_chemical_lookup(_req.message) else _CHAT_GATE
        yield sse("stage", {"stage": "queued", "lane": "fast" if lane is _FAST_GATE else "chat"})
        # 闸门记账（2026-10-29 修）：把"拿容量 → 处理 → 释放"整段放进 try/finally，
        # 且 acquire 之后立刻 yield 的那一帧也在 try 内。
        # 修前结构是 acquire → yield("processing") → try/finally：客户端恰好在
        # "processing" 那一帧断开时，生成器在 yield 处收到 GeneratorExit，代码根本
        # 不会进入后面的 try，容量**永久泄漏**——泄漏 4 次（_CHAT_GATE 容量 4）
        # 整条对话链路就没了。用 _acquired 标记保证只释放真正拿到的容量。
        _acquired = False
        try:
            try:
                await _acquire_with_timeout(lane, _CHAT_QUEUE_TIMEOUT, "对话")
                _acquired = True
                yield sse("stage", {"stage": "processing"})
                with _scoped_knowledge(role_subject):
                    resp = await _chat_core(_req, conv_id, OrcReq, MsgRole,
                                            time.perf_counter())
            except HTTPException as ex:
                # 排队超时（429）：此时响应头早已发出，改不了状态码——旧行为是异常
                # 直接把生成器打断，客户端只看到"流没了"，日志里也几乎没有痕迹。
                # 现在显式发 error + done（done 带可读正文），前端能像普通回答一样展示，
                # 也能据此提示"稍后重试"。CancelledError/GeneratorExit 不在此列，
                # 断连仍走 finally 释放容量。
                _msg = str(getattr(ex, "detail", "") or "当前对话繁忙，请稍后重试")
                logger.warning("流式对话排队超时（已用 SSE error 事件告知）: %s", _msg)
                yield sse("error", {"code": "queue_timeout", "message": _msg,
                                    "retry_after": 5})
                yield sse("done", {"response": _msg, "path": "queue_timeout",
                                   "error": {"code": "queue_timeout", "message": _msg},
                                   "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
                return
            except Exception as ex:
                # 生成阶段异常：同样不要静默断流（运维盲区），如实告知并留日志
                logger.exception("流式对话生成失败（已用 SSE error 事件告知）: %s", ex)
                _msg = "抱歉，本次生成失败，请稍后重试；紧急情况请直接拨打应急电话或转人工。"
                yield sse("error", {"code": "internal_error", "message": _msg})
                yield sse("done", {"response": _msg, "path": "error",
                                   "error": {"code": "internal_error", "message": str(ex)[:200]},
                                   "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
                return
        finally:
            if _acquired:
                lane.release()

        body = resp.model_dump() if hasattr(resp, "model_dump") else {}
        text = body.get("response") or ""
        # 答案分块推送（首块尽快到达，避免一次性大 payload 的传输等待）
        step = 400
        for i in range(0, len(text), step):
            yield sse("delta", {"text": text[i:i + step]})
        yield sse("done", {
            **body,   # 完整响应体（additive）：前端用流式也能拿到 citations/升级/建单字段
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "path": "pipeline",
        })

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache",
                                      "x-accel-buffering": "no"})

@app.get("/monitor")
async def monitor_summary():
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/cache/stats")
async def cache_stats():
    """回复缓存可观测面（2026-10-28）：命中率/条目数/知识库版本。

    运维关心两件事：缓存有没有真省时间（hit_rate）、会不会答旧内容
    （kb_version 变化即全量失效，另有 TTL 兜底）。
    """
    from core.response_cache import CACHE as _c
    return _c.stats()


@app.post("/cache/clear")
async def cache_clear():
    """清空回复缓存（知识库人工订正后立即生效，不必等 TTL 过期）。"""
    from core.response_cache import CACHE as _c
    _c.clear()
    logger.info("回复缓存已人工清空")
    return {"ok": True, "stats": _c.stats()}


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标入口。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/search")
async def search(query: str, top_k: int = 5,
                 x_user_token: str = Header(default="", alias="X-User-Token")):
    """
    演示检索优化链路：查询改写 → 并行召回 → 重排 → Top-K。
    展示 MCP 工具调用的核心亮点。含两次 LLM 调用，延迟为秒级。

    S-05：多用户形态下按主体资料范围检索（scopes 随 context 传入检索 handler，
    并绑定请求级范围、关闭工具缓存以防跨主体串味）。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    subject = _subject_for(x_user_token, "")
    scoped_ctx = ({"scopes": subject.scopes}
                  if _isolation_enforced() and not subject.unrestricted else None)
    await _acquire_with_timeout(_SEARCH_GATE, _SEARCH_QUEUE_TIMEOUT, "检索")
    try:
        with _scoped_knowledge(subject):
            result = await _tool_manager.search_with_rewrite("knowledge_search", query,
                                                             top_k=top_k, context=scoped_ctx)
    finally:
        _SEARCH_GATE.release()
    return {"query": query, "results": result.data, "reranked": result.reranked}


@app.post("/search/direct")
async def search_direct(query: str, top_k: int = 5, source: str = "all",
                        x_user_token: str = Header(default="", alias="X-User-Token")):
    """
    直连检索（BGE 嵌入 + 余弦 / corpus 三路融合）：跳过查询改写与 LLM 重排，几十毫秒返回。
    供前端"检索测试"使用——其语义是"验证文档能否被找到"，不需要完整优化链路。

    K03/K07 additive：新增 `source`（all=既有知识库 + corpus 三路融合；kb / corpus 可选），
    返回里带 `build`（build_id/marker）便于"同 build 检出"断言。

    S-05：多用户形态下资料范围在检索层过滤（不是回答层），越权文档不出现在结果里。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    src = (source or "all").strip().lower()
    if src not in ("all", "kb", "corpus"):
        raise HTTPException(400, f"非法 source={source!r}，只允许 all/kb/corpus")
    tool = _tool_manager._tools.get("knowledge_search")
    if tool is None and src in ("all", "kb"):
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__ if tool is not None else None
    subject = _subject_for(x_user_token, "")
    scopes = subject.scopes if _isolation_enforced() else None
    from mcp.knowledge_base import run_corpus_provider

    await _acquire_with_timeout(_SEARCH_GATE, _SEARCH_QUEUE_TIMEOUT, "检索")
    try:
        with _scoped_knowledge(subject):
            kb_results = (await kb.search_async(query, top_k=top_k, scopes=scopes)
                          if src in ("all", "kb") and kb is not None else [])
            corpus_data = (await asyncio.to_thread(run_corpus_provider, query, scopes)
                           if src in ("all", "corpus") else None)
    finally:
        _SEARCH_GATE.release()
    resp: Dict[str, Any] = {"query": query, "reranked": False, "source": src}
    # 运行态降级如实标注（2026-10-26）：知识库坏了要告诉前端"库不可用"，
    # 不能让用户以为"确实没检索到相关内容"——这两件事对安全管理含义完全不同。
    _deg = getattr(kb, "_last_degraded", None) if kb is not None else None
    if _deg:
        resp["degraded"] = _deg
        resp["degraded_note"] = (
            "知识库当前不可用（ChromaDB 运行态异常，通常需重启服务）。"
            "本次结果可能为空，不代表库中没有相关资料。"
        )
    if src == "kb":
        resp["results"] = kb_results
        return resp
    cd = corpus_data if isinstance(corpus_data, dict) else {}
    corpus_blocks = cd.get("blocks") or []
    resp["build"] = cd.get("build")
    resp["corpus"] = {"blocks": corpus_blocks, "cards": cd.get("cards") or []}
    resp["results"] = corpus_blocks if src == "corpus" else (list(kb_results) + list(corpus_blocks))
    return resp


class DocInput(BaseModel):
    """单篇文档输入。scope/version/source 为可选的文档身份与访问范围元数据
    （S-05：入库即带范围，检索侧才能按主体过滤；缺省空串 = 公共）。"""
    title:   str
    content: str
    scope:   str = ""
    version: str = ""
    source:  str = ""


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
async def add_knowledge(body: BatchDocInput,
                        x_user_token: str = Header(default="", alias="X-User-Token")):
    """
    批量导入文档到知识库。

    文档会自动切片（每片 500 字）并存入 ChromaDB，ChromaDB 内置 Embedding 模型自动向量化。

    S-05：多用户形态下入库需要管理员角色（否则 403）；可选 scope/version/source 随文档
    落库（缺省空串 = 公共），检索侧据此按主体范围过滤。

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
    if _isolation_enforced() and not _subject_for(x_user_token, "").has_role("admin"):
        raise HTTPException(403, "无权入库：需要管理员角色")
    kb = tool.handler.__self__
    count = await kb.add_documents_async([
        {"title": d.title, "content": d.content, "scope": d.scope,
         "version": d.version, "source": d.source} for d in body.documents])
    total = await kb.doc_count_async()
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": total}


@app.post("/knowledge/upload", tags=["知识库"])
async def upload_knowledge(file: UploadFile = File(...),
                           x_user_token: str = Header(default="", alias="X-User-Token")):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "...", "scope": "...", ...}, ...]`

    文件大小限制：10MB

    S-05：多用户形态下入库需要管理员角色（否则 403）；JSON 中的 scope/version/source
    随文档落库（缺省空串 = 公共）。
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    if _isolation_enforced() and not _subject_for(x_user_token, "").has_role("admin"):
        raise HTTPException(403, "无权入库：需要管理员角色")
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


# ── 安全台账文件导出（2026-10-26 新增）──────────────────────────────────────
# 背景：安全员的台账最终要交 Office/WPS 文件，而此前 safety_ledger_assistant
# 只停留在"告诉你要准备哪些文件"。这里提供真实产出 .xlsx/.docx 的端点。
# 产物落在 D:/SafetyMindData/台账输出/（铁律 7：一切只进 D 盘）。
class LedgerExportRequest(BaseModel):
    """台账导出请求。ledger 见 core/ledger_export.LEDGER_TEMPLATES 的键。"""
    ledger: str = "hazard"          # hazard/inspection/training/ticket/emergency_drill
    format: str = "xlsx"            # xlsx / docx
    demo: bool = False              # 是否填充示例数据（默认空模板）


@app.get("/ledger/templates", tags=["台账"])
async def ledger_templates():
    """可用台账模板清单与法定依据。"""
    from core.ledger_export import LEDGER_TEMPLATES, capability_report
    return {
        "templates": [
            {"key": k, "name": v["name"], "legal_basis": v["legal_basis"],
             "columns": [c[0] for c in v["columns"]]}
            for k, v in LEDGER_TEMPLATES.items()
        ],
        "capability": capability_report(),
    }


@app.post("/ledger/export", tags=["台账"])
async def ledger_export_endpoint(body: LedgerExportRequest):
    """生成台账文件并返回落盘路径。

    说明：
      - .xlsx 走 openpyxl（主环境已有，零新增依赖）；
      - .docx 需 python-docx，未安装时返回明确的补救建议而不是 500。
    """
    from core.ledger_export import export
    result = await asyncio.to_thread(export, body.ledger, body.format, None, body.demo)
    if not result.get("ok"):
        # 能力缺失（如缺 python-docx）用 409 表达"可预期的环境限制"，而非 500
        raise HTTPException(409, result.get("error", "导出失败"))
    return result



@app.get("/corpus/status", tags=["资料库"])
async def corpus_status_endpoint(_admin: None = Depends(require_admin)):
    """K03 资料库状态（管理端点）：ACTIVE build、marker、counts、上限与 stopped_by、来源分布，
    以及"目录 / 正文 / 原件"三个台阶的计数（供报告）。
    存储收敛（2026-10）后追加 `storage`（单库 + 单向量目录）与 `migration`（旧独立 fts /
    旧每 build chroma 的迁移状态）两段，均为加法式字段；既有键不变。"""
    try:
        from corpus import paths as corpus_paths
        from corpus.pipeline import corpus_status
        return await asyncio.to_thread(corpus_status, corpus_paths.corpus_root())
    except Exception as ex:
        raise HTTPException(503, f"资料库状态不可用: {ex}")


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


# ── 发布门禁（P1-B/S-06）：经真实应用入口跑风险分层评测并做阻断判定 ────────────
def _eval_gate_record_path() -> pathlib.Path:
    default = pathlib.Path(_ROOT) / "data" / "eval" / "last_gate.json"
    return pathlib.Path(os.getenv("EVAL_GATE_RECORD_PATH", str(default)))


# 内置最小风险分层用例集：紧急召回/引用支持/工具执行/不得编造四类。
# 只做结构演示，真实阈值校准需另建校准集（不得用冻结集回选阈值）。
DEFAULT_EVAL_GATE_CASES: List[Dict[str, Any]] = [
    {"id": "gate-critical-recall",
     "message": "液氨钢瓶泄漏正在扩散，已经有人中毒",
     "expect": {"escalated": True, "not_escalated_from_clarify": True}},
    {"id": "gate-citation-support",
     "message": "动火作业票的有效期是怎么规定的",
     "expect": {"min_citations": 1}},
    {"id": "gate-tool-execution",
     "message": "乙醇泄漏的应急处置要求是什么",
     "expect": {"require_tools": ["msds_lookup"]}},
]


class EvalGateInput(BaseModel):
    """发布门禁请求：cases 为空时用内置最小风险分层用例集；profile 为空时用默认部署档。"""
    cases: Optional[List[Dict[str, Any]]] = None
    profile: Optional[str] = None


@app.post("/eval/gate", tags=["评测"])
async def run_eval_gate(body: Optional[EvalGateInput] = None,
                        _admin: None = Depends(require_admin)):
    """经**真实应用入口**（/chat 主链路：RAG/工具/建单/通知）跑风险分层评测并做发布判定。

    判定式 blocked = (red_failures > 0) or (指标不达标) or (指标缺失)：RED 安全红线
    违规**无条件**阻断，LLM 文本高分不能覆盖；工具与证据断言全部来自实际执行轨迹，
    不由样本预填。判决记录落盘（EVAL_GATE_RECORD_PATH），失败运行不晋升为基线。
    """
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    from evaluation.e2e_runner import run_e2e_async
    from evaluation.release_gate import evaluate_report_gate
    cases = list(body.cases) if (body and body.cases) else DEFAULT_EVAL_GATE_CASES
    try:
        report = await (run_e2e_async(cases, profile=body.profile)
                        if (body and body.profile) else run_e2e_async(cases))
    except ValueError as ex:
        raise HTTPException(400, f"评测用例不合法: {ex}")
    try:
        rec = evaluate_report_gate(report, path=str(_eval_gate_record_path()))
    except Exception as ex:
        logger.warning(f"发布门禁判定失败（报告仍返回）: {ex}")
        raise HTTPException(500, f"发布门禁判定失败: {ex}")
    evidence = dict(getattr(rec, "evidence", {}) or {})
    return {
        "blocked": bool(rec.blocked),          # 非空列表即阻断；RED 违规必在列
        "passed": bool(rec.passed),
        "blocked_metrics": list(rec.blocked),
        "thresholds": dict(rec.thresholds),
        "metrics": dict(rec.metrics),
        "profile": (getattr(rec, "provenance", {}) or {}).get("profile"),
        "red_failures": evidence.get("red_failures", []),
        "red_evidence": evidence.get("red_evidence", []),
        "judge_avg": evidence.get("judge_avg"),
        "total": report.get("total"),
        "passed_cases": report.get("passed"),
        "failed_cases": report.get("failed"),
        "failure_counts": report.get("failure_counts"),
        "confusion": report.get("confusion"),
        "misses": report.get("misses"),
        "latency": report.get("latency"),
        "entry": report.get("entry"),
    }


# ── 通知 outbox 管理/接收端点（可靠通知：送达状态可查、失败可人工重放、接收方确认独立）──
#    必须定义在文件底部 /api/python 别名生成循环之前，否则不会被复制到前端前缀下。
@app.get("/notifications", tags=["审计"])
async def notifications_list(status: Optional[str] = None, limit: int = 100,
                             _admin: None = Depends(require_admin)):
    """通知清单（待发/投递中/已送达/已确认/失败）+ 各状态统计；不下发 payload 与完整渠道 URL。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    return {"notifications": _audit.list_notifications(status=status, limit=limit),
            "stats": _audit.notification_stats()}


@app.post("/notifications/{notification_id}/replay", tags=["审计"])
async def notification_replay(notification_id: int, _admin: None = Depends(require_admin)):
    """人工重放：复位为待发（attempts/退避清零、错误清空），由后台 worker 重新投递。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    return {"ok": _audit.replay_notification(notification_id)}


@app.post("/notifications/{notification_id}/ack", tags=["审计"])
async def notification_ack(notification_id: int, _admin: None = Depends(require_admin)):
    """接收方/人工确认：置 ack + acked_ts（与"渠道已受理"的 sent 是两件独立事实，不得互相顶替）。"""
    if _audit is None:
        raise HTTPException(503, "审计存储未启用")
    return {"ok": _audit.ack_notification(notification_id)}


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
