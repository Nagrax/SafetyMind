# -*- coding: utf-8 -*-
# SM-01 第三片 / G4（假模型层可离线部分）：/chat 与 /chat/stream 行为基线冻结。
#
# ## 冻结什么（四维，全部 case ID 定位）##
#   1. 关键事实：正文数值+单位 / 标准号（extract_facts，与 benchmarks/eval_stream_parity.py
#      同口径——直接 import 那份实现，杜绝复制漂移）
#   2. 引用存在性：citations 数量/标题/身份字段（doc_id/chunk_id/catalog_only）
#   3. SSE done 事件：事件序列 meta→(stage*)→delta*→done、done 携带完整响应体
#   4. 安全行为：CRITICAL 模板命中（urgency 与 safety_net 两个确定性升级依据）、
#      紧急快速分流零 LLM 路径、隐患闸门 auto/suggest 分层
# 以及两端点配对一致性（同 payload 先 /chat 后 /chat/stream，结构字段与正文逐字段比对）。
#
# ## 用哪套桩（仓库既有假模型层，不新造）##
#   - 编排器打桩：复用 tests/test_chat_tool_evidence.py::FakeOrchestrator 的注入模式
#     （api.main._orchestrator/_memory/_tool_manager/_audit/_conv_store 全局替换）；
#   - LLM 假模型：死端点（ANTHROPIC_BASE_URL=http://127.0.0.1:9，tests/test_smoke_readonly.py
#     同款）——CRITICAL 模板/安全网/快速分流本就零 LLM 依赖，真实代码路径离线可跑
#     （驱动方式与 tests/test_emergency_safety_net.py §5 一致）；
#   - KB/资源库/审计用真实实现（ngram 嵌入离线确定性），文档种子带 UNIQ 探针。
#
# ## 两个模式 ##
#   回放（默认，进 runner core 清单）: python tests/test_endpoint_baseline_freeze.py
#       读冻结 JSON（默认 D:/claudecode/scratch/sm-loop/handoff/SM-01/slice3-endpoint-baseline.json，
#       可用 SM01_BASELINE_JSON 覆盖）作为断言源重放两端点；任一字段漂移即 [FAIL]（exit 1）。
#   采集（人工触发，门禁不用）: python tests/test_endpoint_baseline_freeze.py --capture <out.json>
#       把当前行为 + 数据/桩指纹 + 运行环境写成冻结基线；拒绝覆盖已有产物（--force 才可）。
#
# ## 明确不冻结（不冒充）##
#   - 真模型轴：真实 LLM 下两端点的行为基线需要 23:00-08:00 窗口口径（docs/BENCHMARK.md 的
#     评测窗），本文件只在假模型层冻结，真实生成正文/耗时不在断言范围；
#   - 计时字段：latency_ms / total_ms / 工具耗时（只记录非冻结，不参与比对）；
#   - 随机标识：request_id / 工单号（冻结前缀与非零性，正文里的编号做归一化）。
#
# 退出码：0 全过 | 1 断言失败（基线漂移）| 2 环境不可用（依赖缺失/app 起不来/基线缺失）。
# 统计行 X/Y PASSED（runner parse_counts 可识别）。
import argparse
import gc
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))    # 共享 _storage_guard
sys.path.insert(0, str(REPO / "benchmarks"))                # eval_stream_parity 同口径函数

# 事实抽取 / SSE 解析 / Jaccard：直接复用 benchmarks/eval_stream_parity.py 的实现
# （"既有口径"本体，杜绝复制漂移；该模块 import 侧无副作用、不联网）
from eval_stream_parity import extract_facts, fact_jaccard, parse_sse  # noqa: E402

SCHEMA = "sm01-endpoint-baseline-v1"
BASELINE_PATH = Path(os.getenv(
    "SM01_BASELINE_JSON",
    "D:/claudecode/scratch/sm-loop/handoff/SM-01/slice3-endpoint-baseline.json"))
SCRATCH = Path("D:/claudecode/scratch/sm-loop")

# ── 环境钉死（冻结口径的一部分；回放时逐项核对）────────────────────────────────
ENV_PINS = {
    "SAFETYMIND_EMBEDDING": "ngram",            # 离线确定性嵌入（不下载模型、不联网）
    "SAFETYMIND_RESPONSE_CACHE": "0",           # 缓存关：两端口独立走全链路（eval_stream_parity 同口径）
    "SAFETYMIND_TEMPERATURE": "0",              # 生成温度 0（假模型层不生成，但钉住口径）
    "ANTHROPIC_API_KEY": "dummy",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",  # 假模型 = 死 LLM 端点（smoke 同款）
    "SAFETYMIND_CRITICAL_TEMPLATE": "1",        # CRITICAL 模板开（安全行为冻结对象）
    "SAFETYMIND_EMERGENCY_SAFETY_NET": "1",     # 紧急安全网开（同上）
    "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
    "SAFETYMIND_OUTBOX_ENABLED": "0",           # outbox worker 不起（行照常落库，投递不跑）
    "PROMETHEUS_PORT": "0",
    "SAFETYMIND_EMERGENCY_FASTPATH": "1",       # case 级覆盖（critical-template 两个 case 关掉）
    "SAFETYMIND_BGE_TAU": "0.80",               # case 级覆盖（clarify case 调低）
}

# ── KB 种子文档（带 UNIQ 探针；夹具文案与 test_chat_tool_evidence.py 同源）──────
TAIL_CLAUSE = "二级动火票有效期不超过72小时UNIQ7788，作业结束后作业人须立即关闭动火票。"
FILLER = ("特殊作业前必须完成风险辨识与安全交底。现场作业人员应确认作业票证齐全有效。"
          "监护人员须全程在岗并保持通讯畅通。")
KB_DOC = {"title": "动火作业管理规定（冻结基线种子）",
          "content": FILLER * 8 + TAIL_CLAUSE,
          "source": "内部制度", "version": "2026-03", "scope": "全厂", "status": "active"}
KB_PROBE = "UNIQ7788"

# ── case 集（顺序即运行顺序；工单号因入库顺序而异但不冻结，只冻结非零性）────────
# intent/result 为桩脚本（假模型层）；run_mode=real-critical 表示桩只接管意图识别，
# run() 委托真实 AgentOrchestrator（死 LLM）走真实 CRITICAL 模板/安全网路径。
CASES = [
    {
        "id": "reg-query-kb-citations",
        "dims": ["关键事实", "引用存在性", "SSE done", "端点配对"],
        "message": "动火作业票的有效期是怎么规定的？UNIQ7788",
        "intent": {"intent": "REGULATION_QUERY", "confidence": 0.92, "urgency": 1,
                   "group": "general", "entities": {}},
        "result": {"response": "动火作业票有效期不超过72小时，依据 GB 15577-2022 第5.1条，"
                               "作业结束后应立即关闭动火票并确认无残留火种。",
                   "agent_type": "GENERAL", "routing_reason": "stub:法规咨询直答",
                   "tool_trace": [{"name": "knowledge_search", "ok": True,
                                   "params": {"query": "动火作业票有效期"}, "preview": "依据命中片段"}]},
    },
    {
        "id": "hazard-report-auto-ticket",
        "dims": ["隐患闸门auto", "端点配对"],
        "message": "3号车间除尘风机异响滤袋破损，现上报隐患，请尽快安排处理",
        "intent": {"intent": "HAZARD_REPORT", "confidence": 0.9, "urgency": 3,
                   "group": "hazard", "entities": {"equipment_id": ["EQ-FAN-003"]}},
        "result": {"response": "已收到设备隐患上报：除尘风机异响且滤袋破损，已按流程自动建单派发，"
                               "整改完成前请停用该设备并设置警示标识。",
                   "agent_type": "GENERAL", "routing_reason": "stub:隐患上报直答", "tool_trace": []},
    },
    {
        "id": "hazard-inspection-suggest",
        "dims": ["隐患闸门suggest", "端点配对"],
        "message": "组织一次受限空间作业隐患排查，重点检查台账和警示标志是否齐全",
        "intent": {"intent": "HAZARD_INSPECTION", "confidence": 0.88, "urgency": 2,
                   "group": "hazard", "entities": {}},
        "result": {"response": "受限空间作业隐患排查要点：先办作业票、气体检测合格后再进入，"
                               "台账与警示标志纳入本次检查清单。",
                   "agent_type": "GENERAL", "routing_reason": "stub:排查咨询直答", "tool_trace": []},
    },
    {
        "id": "clarify-ambiguity",
        "dims": ["SSE done", "端点配对"],
        "message": "车间那边的情况你帮我看看怎么回事",
        "bge_tau": "0.40",      # 澄清带判定需要 tau ≤ conf < 0.55
        "intent": {"intent": "HAZARD_INSPECTION", "confidence": 0.5, "urgency": 1,
                   "group": "hazard", "entities": {},
                   "source_scores": {"laya_fused": 0.51},
                   "alt_intent": "WORK_PERMIT", "alt_confidence": 0.4},
        "result": {"response": "（澄清分支不应走到桩）", "agent_type": "GENERAL",
                   "routing_reason": "stub:不应触达", "tool_trace": []},
    },
    {
        "id": "msds-fastpath-ethanol",
        "dims": ["关键事实", "确定性直出", "端点配对"],
        "message": "乙醇的闪点是多少",
        "intent": {"intent": "CHEMICAL_SAFETY", "confidence": 0.93, "urgency": 1,
                   "group": "general", "entities": {"chemical": ["乙醇"]}},
        "result": {"response": "（确定性直出不应走到桩）", "agent_type": "GENERAL",
                   "routing_reason": "stub:不应触达", "tool_trace": []},
    },
    {
        "id": "critical-template-urgency",
        "dims": ["安全行为(CRITICAL模板)", "SSE done", "端点配对"],
        "fastpath": "0",        # 关掉入口快速分流，让消息走完整链路命中编排器 CRITICAL 模板
        "message": "车间氨气泄漏，已经有人中毒晕倒了，现在该怎么办？",
        "intent": {"intent": "EMERGENCY_RESPONSE", "confidence": 0.85, "urgency": 4,
                   "group": "emergency", "entities": {"chemical": ["氨气"]}},
        "run_mode": "real-critical",
        "result": {"response": "（真实模板路径）", "agent_type": "ESCALATION",
                   "routing_reason": "stub:真实CRITICAL模板", "tool_trace": []},
    },
    {
        "id": "critical-template-safety-net",
        "dims": ["安全行为(紧急安全网)", "端点配对"],
        "fastpath": "0",
        "message": "车间氨气泄漏已经有人晕倒，现在该怎么办？",
        "intent": {"intent": "OTHER", "confidence": 0.3, "urgency": 1,
                   "group": "other", "entities": {}},   # 模拟负载下意图退化
        "run_mode": "real-critical",
        "result": {"response": "（真实安全网路径）", "agent_type": "ESCALATION",
                   "routing_reason": "stub:真实安全网", "tool_trace": []},
    },
    {
        "id": "emergency-fastpath-zero-llm",
        "dims": ["安全行为(快速分流)", "SSE done", "端点配对"],
        "fastpath": "1",        # 入口紧急预检命中：零 LLM/零记忆/零 RAG，桩完全不触达
        "message": "车间氨气泄漏，已经有人中毒晕倒了，现在该怎么办？",
        "intent": {"intent": "EMERGENCY_RESPONSE", "confidence": 0.85, "urgency": 4,
                   "group": "emergency", "entities": {}},
        "result": {"response": "（快速分流不应走到桩）", "agent_type": "ESCALATION",
                   "routing_reason": "stub:不应触达", "tool_trace": []},
    },
]

# ── 归一化（采集与回放共用同一套，防止"两边都变"的假绿）────────────────────────
_LAT_KEYS = {"latency_ms", "total_ms", "chat_ms", "stream_total_ms", "cache_age_s"}
_VARIED_ID_INLINE = re.compile(r"\b(emg|clr|det|cache)-[0-9a-f]{6,}\b", re.I)
_VARIED_ID_WHOLE = re.compile(r"^(emg|clr|det|cache)-[0-9a-f]{4,}$", re.I)
_HEX_ID = re.compile(r"[0-9a-f]{6,32}", re.I)


def norm_text(s: str) -> str:
    """正文里的运行期编号归一：emg-xxxx 等 → 前缀-<RID>；工单号 #N → #<TID>。"""
    s = _VARIED_ID_INLINE.sub(lambda m: m.group(1) + "-<RID>", s)
    return re.sub(r"#\d+", "#<TID>", s)


def _norm_id(v: str) -> str:
    if _VARIED_ID_WHOLE.match(v or ""):
        return v.split("-")[0] + "-<RID>"
    if _HEX_ID.fullmatch(v or ""):
        return "<RID>"          # 纯 hex 的随机 request_id（uuid4[:8] 等）
    return v


def norm(o, key=None):
    """递归归一：丢计时键、浮点 6 位、正文编号归一。采集/回放必须同构。"""
    if isinstance(o, dict):
        return {k: norm(v, key=k) for k, v in o.items() if k not in _LAT_KEYS}
    if isinstance(o, list):
        return [norm(x, key=key) for x in o]
    if isinstance(o, float):
        return round(o, 6)
    if isinstance(o, str):
        if key == "request_id":
            return _norm_id(o)
        return norm_text(o)
    return o


def deep_diff(a, b, path=""):
    """冻结值 a vs 观察值 b 的逐路径差异（可读输出，不截断结构）。"""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                diffs.append(f"{path}.{k}: 观察多出（冻结缺失）")
            elif k not in b:
                diffs.append(f"{path}.{k}: 冻结多出（观察缺失）")
            else:
                diffs += deep_diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path}: 列表长度 冻结={len(a)} 观察={len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diffs += deep_diff(x, y, f"{path}[{i}]")
    elif isinstance(a, bool) != isinstance(b, bool):
        diffs.append(f"{path}: 类型漂移 冻结={type(a).__name__} 观察={type(b).__name__}")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if abs(float(a) - float(b)) > 1e-9:
            diffs.append(f"{path}: 数值 冻结={a} 观察={b}")
    elif a != b:
        diffs.append(f"{path}: 冻结={str(a)[:90]!r} 观察={str(b)[:90]!r}")
    return diffs


# ── 桩层（复用 test_chat_tool_evidence.FakeOrchestrator 的既有模式）────────────
class FakeMemCtx:
    recent_messages = None

    def to_prompt_text(self):
        return ""


class FakeMemory:
    async def get_context(self, *a, **k):
        return FakeMemCtx()

    async def add_message(self, *a, **k):
        return None

    async def update_profile(self, *a, **k):
        return None

    async def absorb_feedback(self, *a, **k):
        return None

    async def close(self):
        """lifespan 关停钩子（api.main 在 shutdown 时 await _memory.close()）。"""
        return None


class FreezeOrchestrator:
    """假模型层编排器：意图按 case 脚本直出；run() 支持 scripted / real-critical 两种模式。

    real-critical：委托真实 AgentOrchestrator（死 LLM 端点）跑真实 EmployeeAgent.handle——
    CRITICAL 模板与紧急安全网零 LLM 依赖，离线可复现（test_emergency_safety_net 同款驱动）。
    真实编排器实例惰性建一次并复用（模板路径无状态；req.intent 已由桩给出 → 不再走
    死端点意图识别）。
    """

    def __init__(self):
        self.spec = None
        self.last_req = None
        self._real_orch = None

    def set_spec(self, spec):
        self.spec = spec

    def _real(self):
        if self._real_orch is None:
            from agents.agent_orchestrator import AgentOrchestrator
            self._real_orch = AgentOrchestrator(api_key="dummy",
                                                base_url="http://127.0.0.1:9", model="dummy")
        return self._real_orch

    async def recognize_intent(self, message, history=None):
        from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
        s = self.spec["intent"]
        alt = s.get("alt_intent")
        # IntentResult.alt_intent 的契约是**枚举值字符串**（如 "work_permit"），
        # case 脚本里写枚举名，这里统一转换，避免澄清带判定拿 NAME 比对 VALUE 漏判。
        alt_value = IntentCategory[alt].value if alt else None
        return IntentResult(
            intent=IntentCategory[s["intent"]], confidence=s["confidence"],
            urgency=UrgencyLevel(s["urgency"]), intent_group=s["group"],
            entities={k: list(v) for k, v in (s.get("entities") or {}).items()},
            reasoning="stub(freeze-baseline)", latency_ms=1.0,
            source_scores=dict(s.get("source_scores") or {}),
            alt_intent=alt_value, alt_confidence=s.get("alt_confidence", 0.0),
        )

    async def run(self, req):
        from agents.agent_orchestrator import AgentType, OrchestratorResult
        from core.tool_execution import ToolCallRecord
        self.last_req = req
        if self.spec.get("run_mode") == "real-critical":
            return await self._real().run(req)
        s = self.spec["result"]
        trace = [ToolCallRecord(tool_use_id=f"tu_{i}", name=t["name"],
                                params=dict(t.get("params") or {}), ok=bool(t.get("ok", True)),
                                error=None, latency_ms=6.0,
                                result_preview=t.get("preview", "依据命中片段"))
                 for i, t in enumerate(s.get("tool_trace") or [])]
        return OrchestratorResult(
            request_id=req.request_id, response=s["response"],
            agent_type=AgentType[s["agent_type"]], intent=req.intent,
            escalated=bool(s.get("escalated", False)),
            escalation_basis=s.get("escalation_basis", ""),
            latency_ms=5.0,
            agent_types=[AgentType[s["agent_type"]]],
            primary_agent=AgentType[s["agent_type"]],
            routing_reason=s["routing_reason"],
            routing_confidence=1.0,
            tool_trace=trace,
        )


# ── 栈搭建（真实 KB/资源库/审计 + 假编排器/记忆；存储全部进 scratch 临时目录）──
def build_stack(tmp_root: Path):
    import _storage_guard as guard
    from core.audit_store import AuditStore
    from mcp.knowledge_base import KnowledgeBase
    from mcp.resource_library import ResourceLibrary
    from mcp.tool_manager import MCPToolManager, Tool

    kb, kb_dir = guard.build_kb_checked(
        tmp_root,
        lambda d: KnowledgeBase(chroma_host="127.0.0.1", chroma_port=1, chroma_path=str(d)),
        lambda k: k.add_documents([KB_DOC]), KB_PROBE, name="freeze_kb")
    lib_dir = guard.fresh_dir(tmp_root, "freeze_reslib")
    lib = ResourceLibrary(base_dir=str(lib_dir))
    tm = MCPToolManager(api_key="test-key")
    tm.register(Tool(name="knowledge_search", description="t", handler=kb.search_handler,
                     schema={"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]}, cache_ttl=0.0))
    tm.register(Tool(name="msds_lookup", description="t", handler=lib.msds_handler,
                     schema={"type": "object",
                             "properties": {"action": {"type": "string"},
                                            "name": {"type": "string"},
                                            "cas": {"type": "string"},
                                            "section": {"type": "string"}},
                             "required": []}, cache_ttl=0.0))
    audit_dir = guard.fresh_dir(tmp_root, "freeze_audit")
    _audit_seq = [0]

    def audit_factory():
        """每次端点调用一个**全新审计库**：配对实验必须同起点——审计副作用（建单/
        升级/建议）带 24h 去重，若 /chat 与 /chat/stream 共用一个库，第二个请求会
        被去重语义改写（auto_ticket→merged），那是会话级状态，不是端点分叉。"""
        _audit_seq[0] += 1
        return AuditStore(db_path=str(Path(audit_dir) / f"audit_freeze_{_audit_seq[0]}.db"))

    import api.main as api_main
    api_main._tool_manager = tm
    api_main._memory = FakeMemory()
    fake = FreezeOrchestrator()
    api_main._orchestrator = fake
    api_main._audit = audit_factory()
    api_main._conv_store = None
    return api_main, fake, guard, kb, audit_factory, [kb_dir, lib_dir, audit_dir]


def release_stack(guard, kb, api_main, dirs):
    guard.release_kb(kb)
    try:                       # 最后一次端点调用注入的审计库还没关（Windows 句柄）
        api_main._audit.close()
    except Exception:
        pass
    gc.collect()
    for d in dirs:
        if not guard.rmtree_retry(d):
            print(f"[WARN] 临时目录残留未清干净（句柄未释放？）: {d}", flush=True)


# ── 单 case 观测：同 payload 先 /chat 再 /chat/stream（eval_stream_parity 同序）──
def collect(client, fake, api_main, audit_factory, case):
    os.environ["SAFETYMIND_EMERGENCY_FASTPATH"] = case.get("fastpath", "1")
    os.environ["SAFETYMIND_BGE_TAU"] = case.get("bge_tau", "0.80")
    fake.set_spec(case)
    payload = {"message": case["message"], "user_id": "freeze-baseline",
               "conv_id": f"frz-{case['id']}"}

    def fresh_audit():
        old = api_main._audit
        try:
            if old is not None:
                old.close()
        except Exception:
            pass
        api_main._audit = audit_factory()

    fresh_audit()
    r1 = client.post("/chat", json=payload)
    b1 = r1.json() if r1.status_code == 200 else {}
    body_n = norm(b1)                    # 归一后的完整 /chat 响应体（done 覆盖性核对用）

    fresh_audit()                        # 第二个端点同起点（见 audit_factory 注释）

    r2 = client.post("/chat/stream", json=payload)
    frames = parse_sse(r2.text) if r2.status_code == 200 else []
    events, metas, stages, deltas, done = [], [], [], [], {}
    for ev, data in frames:
        events.append(ev)
        if ev == "meta":
            metas.append(data)
        elif ev == "stage":
            stages.append(data)
        elif ev == "delta":
            deltas.append(str(data.get("text") or ""))
        elif ev == "done":
            done = data

    cites = [{
        "index": c.get("index"), "title": c.get("title"),
        "doc_id": c.get("doc_id"), "chunk_id": c.get("chunk_id"),
        "content_hash": c.get("content_hash"), "section": c.get("section"),
        "score": c.get("score"), "source": c.get("source"), "version": c.get("version"),
        "status": c.get("status"), "chunk_index": c.get("chunk_index"),
        "total_chunks": c.get("total_chunks"), "snippet": c.get("snippet"),
        "catalog_only": c.get("catalog_only"),
    } for c in (b1.get("citations") or [])]

    chat = {
        "status": r1.status_code,
        "intent": body_n.get("intent"), "intent_group": body_n.get("intent_group"),
        "agent_type": body_n.get("agent_type"), "agent_types": body_n.get("agent_types"),
        "primary_agent": body_n.get("primary_agent"),
        "supporting_agents": body_n.get("supporting_agents"),
        "routing_reason": body_n.get("routing_reason"),
        "routing_confidence": body_n.get("routing_confidence"),
        "escalated": bool(body_n.get("escalated")),
        "escalation_basis": body_n.get("escalation_basis") or "",
        "knowledge_used": bool(body_n.get("knowledge_used")),
        "entities": body_n.get("entities") or {},
        "hazard_action": body_n.get("hazard_action") or "none",
        "hazard_ticket_id_nonzero": bool(body_n.get("hazard_ticket_id")),
        "hazard_suggestion": body_n.get("hazard_suggestion"),
        "emergency_fastpath": bool(body_n.get("emergency_fastpath")),
        "urgency": body_n.get("urgency") or "",
        "emergency_status": body_n.get("emergency_status") or {},
        "response": body_n.get("response") or "",
        "request_id": body_n.get("request_id") or "",
        "conv_id": body_n.get("conv_id") or "",
        "subject_id": body_n.get("subject_id") or "",
        "citations": norm(cites),
        "tools_used": [{"name": t.get("name"), "ok": bool(t.get("ok")),
                        "error": t.get("error"),
                        **({"deterministic": bool(t["deterministic"])}
                           if "deterministic" in t else {})}
                       for t in (b1.get("tools_used") or [])],
        "facts": sorted(extract_facts(b1.get("response") or "")),
    }

    done_n = norm(done or {})
    delta_text = "".join(deltas)
    stream = {
        "status": r2.status_code,
        "events": events,
        "meta": norm(metas),
        "stages": norm(stages),
        "delta_text": norm_text(delta_text),
        "done": done_n,
        "facts": sorted(extract_facts(delta_text)),
    }

    parity = {
        "body_equal": (stream["delta_text"] == chat["response"]
                       == stream["done"].get("response")),
        # done 事件 = 完整 /chat 响应体（归一后逐字段相等）+ path 标记（additive 契约）
        "done_covers_chat_body": (
            {k: v for k, v in stream["done"].items() if k != "path"} == body_n),
        "intent_equal": chat["intent"] == stream["done"].get("intent"),
        "escalated_equal": chat["escalated"] == bool(stream["done"].get("escalated")),
        "basis_equal": chat["escalation_basis"] == (stream["done"].get("escalation_basis") or ""),
        "hazard_equal": chat["hazard_action"] == (stream["done"].get("hazard_action") or "none"),
        "citations_equal": body_n.get("citations") == stream["done"].get("citations"),
        "knowledge_equal": chat["knowledge_used"] == bool(stream["done"].get("knowledge_used")),
        "facts_jaccard": fact_jaccard(set(chat["facts"]), set(stream["facts"])),
    }
    return {"id": case["id"], "dims": case.get("dims") or [],
            "message": case["message"], "chat": chat, "stream": stream,
            "parity": parity}


# ── 指纹与环境 ────────────────────────────────────────────────────────────────
def _sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def fingerprints() -> dict:
    fp = {
        "stub_file_sha256": _sha256_file(Path(__file__)),
        "kb_seed_doc_sha256": hashlib.sha256(
            json.dumps(KB_DOC, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "msds_seed_sha256": _sha256_file(REPO / "mcp" / "msds_seed.json"),
        "embedding": os.environ.get("SAFETYMIND_EMBEDDING", ""),
        "llm_endpoint": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "case_ids": [c["id"] for c in CASES],
    }
    try:
        fp["api_main_sha256"] = _sha256_file(REPO / "api" / "main.py")
        fp["orchestrator_sha256"] = _sha256_file(REPO / "agents" / "agent_orchestrator.py")
    except OSError:
        pass
    return fp


def environment() -> dict:
    env = {
        "python": sys.version.split()[0],
        "python_full": sys.version,
        "platform": platform.platform(),
        "env_pins": {k: os.environ.get(k, "") for k in ENV_PINS},
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                              capture_output=True, timeout=30)
        env["repo_head_sha"] = head.stdout.decode().strip() if head.returncode == 0 else "unknown"
        st = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO),
                            capture_output=True, timeout=30)
        env["repo_dirty_files"] = [l.strip() for l in st.stdout.decode().split("\n") if l.strip()]
    except Exception:
        env["repo_head_sha"] = "unknown"
        env["repo_dirty_files"] = []
    return env


# ── 运行前钉环境（必须在 import api.main 之前）────────────────────────────────
def pin_env(tmp_root: Path):
    for k, v in ENV_PINS.items():
        os.environ[k] = v
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    # 存储旋钮：runner 已提供的沿用（setdefault），standalone 运行落到本进程临时目录
    for key, name in (("AUDIT_DB_PATH", "audit.db"),
                      ("CHROMA_PERSIST_DIRECTORY", "chroma"),
                      ("CHROMA_KB_PERSIST_DIRECTORY", "kb_chroma"),
                      ("SAFETYMIND_CONVERSATIONS_DIR", "conversations"),
                      ("PREFERENCE_DB_PATH", "preference_cards.db"),
                      ("SAFETYMIND_MEMORY_SNAPSHOT", "memory_snapshot.json"),
                      ("SAFETYMIND_SKILL_EVO_DIR", "skill_evo"),
                      ("SAFETYMIND_CORPUS_ROOT", "corpus")):
        os.environ.setdefault(key, str(tmp_root / name))
    # 评测产物路径产品默认会写 /app/data/eval 或仓库 data/eval，强制钉到临时目录
    # （与 test_smoke_readonly.py 同款处置）
    for key in ("EVAL_BASELINE_PATH", "EVAL_LAST_REPORT_PATH"):
        os.environ[key] = str(tmp_root / "eval" / f"{key.lower()}.json")
    os.environ.setdefault("EVAL_GATE_RECORD_PATH", str(tmp_root / "eval" / "eval_gate_record.json"))


# ── 采集 ─────────────────────────────────────────────────────────────────────
def run_capture(out_path: Path, force: bool) -> int:
    if out_path.exists() and not force:
        print(f"[REJECT] 产物已存在，拒绝覆盖（--force 才可重冻）: {out_path}")
        return 2
    try:
        from fastapi.testclient import TestClient
    except Exception as ex:
        print(f"[REJECT] 环境不可用（fastapi TestClient 缺失）: {ex}")
        return 2
    try:
        import api.main as m
    except Exception as ex:
        print(f"[REJECT] 环境不可用（产品 app 导入失败）: {type(ex).__name__}: {ex}")
        return 2

    SCRATCH.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="sm01_freeze_", dir=str(SCRATCH)))
    rows = []
    try:
        with TestClient(m.app) as client:
            api_main, fake, guard, kb, audit_factory, dirs = build_stack(tmp_root)
            try:
                for case in CASES:
                    t0 = time.time()
                    row = collect(client, fake, api_main, audit_factory, case)
                    rows.append(row)
                    print(f"[CAPTURE] {case['id']} chat={row['chat']['status']} "
                          f"stream={row['stream']['status']} "
                          f"cites={len(row['chat']['citations'])} "
                          f"escalated={row['chat']['escalated']}/{row['chat']['escalation_basis']!r} "
                          f"hazard={row['chat']['hazard_action']} "
                          f"({time.time()-t0:.1f}s)", flush=True)
            finally:
                release_stack(guard, kb, api_main, dirs)
    finally:
        gc.collect()
        shutil.rmtree(str(tmp_root), ignore_errors=True)

    doc = {
        "schema": SCHEMA,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "captured_by": "python tests/test_endpoint_baseline_freeze.py --capture",
        "note": ("假模型层（stub orchestrator + 死 LLM 端点）行为基线；回放见本文件 "
                 "tests/test_endpoint_baseline_freeze.py 默认模式。基线漂移即红。"),
        "scope": {
            "endpoints": ["/chat", "/chat/stream"],
            "layer": "fake-model（仓库既有桩层，见 tests/ 头注）",
            "dimensions": ["关键事实", "引用存在性", "SSE done 事件",
                           "安全行为(CRITICAL 模板/安全网/快速分流)", "端点配对一致性"],
        },
        "not_frozen": {
            "real_model_axis": {
                "status": "未冻结",
                "window": "23:00-08:00 窗口口径（真模型轴）",
                "reason": ("真模型两端点基线需要真实 LLM 端点与评测窗口（docs/BENCHMARK.md 口径）；"
                           "本片只冻结可离线复现的假模型层，不冒充真模型结论"),
                "pointer": ["docs/BENCHMARK.md", "benchmarks/eval_stream_parity.py"],
            },
            "timing_fields": sorted(_LAT_KEYS),
            "volatile_fields": ["request_id（冻结前缀/归一形）",
                                "hazard/emergency 工单号（冻结非零性）"],
        },
        "environment": environment(),
        "fingerprints": fingerprints(),
        "cases": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[CAPTURE] written {out_path}（cases={len(rows)}）")
    return 0


# ── 回放（默认模式）：冻结 JSON 为断言源，漂移即红 ─────────────────────────────
def run_replay() -> int:
    try:
        from fastapi.testclient import TestClient
    except Exception as ex:
        print(f"[REJECT] 环境不可用（fastapi TestClient 缺失）: {ex}")
        return 2
    if not BASELINE_PATH.is_file():
        print(f"[REJECT] 环境不可用（冻结基线缺失）: {BASELINE_PATH}")
        return 2
    try:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"[REJECT] 环境不可用（冻结基线不可解析）: {ex}")
        return 2
    try:
        import api.main as m
    except Exception as ex:
        print(f"[REJECT] 环境不可用（产品 app 导入失败）: {type(ex).__name__}: {ex}")
        return 2

    RESULTS = []

    def record(name, ok, detail=""):
        RESULTS.append(bool(ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))

    # ── 契约头核对：schema / case 清单 / 环境钉 / 数据指纹 ────────────────────
    record("基线 schema 一致", baseline.get("schema") == SCHEMA,
           f"frozen={baseline.get('schema')!r}")
    frozen_ids = [c.get("id") for c in (baseline.get("cases") or [])]
    record("case 清单一致（不缺不多不换序）", frozen_ids == [c["id"] for c in CASES],
           f"frozen={frozen_ids}")

    pins_now = {k: os.environ.get(k, "") for k in ENV_PINS}
    for k, v in ENV_PINS.items():     # 回放自己重新钉一遍，保证口径与冻结时一致
        os.environ[k] = v
    pins_frozen = (baseline.get("environment") or {}).get("env_pins") or {}
    record("环境钉与冻结一致（冻结口径可复现的前提）",
           all(pins_frozen.get(k) == v for k, v in ENV_PINS.items()),
           f"diff={{ {', '.join(f'{k}: frozen={pins_frozen.get(k)!r} now={v!r}' for k, v in ENV_PINS.items() if pins_frozen.get(k) != v)} }}")

    fp_frozen = baseline.get("fingerprints") or {}
    fp_now = fingerprints()
    for key in ("kb_seed_doc_sha256", "msds_seed_sha256"):
        record(f"数据指纹一致: {key}", fp_frozen.get(key) == fp_now.get(key),
               f"frozen={str(fp_frozen.get(key))[:16]} now={str(fp_now.get(key))[:16]}")
    record("数据指纹一致: embedding", fp_frozen.get("embedding") == fp_now.get("embedding"),
           f"frozen={fp_frozen.get('embedding')!r} now={fp_now.get('embedding')!r}")
    if fp_frozen.get("python") and fp_frozen.get("python") != sys.version.split()[0]:
        print(f"[WARN] 解释器版本与冻结时不同（记录差异，不算漂移）: "
              f"frozen={fp_frozen.get('python')} now={sys.version.split()[0]}")

    # ── 逐 case 回放 ────────────────────────────────────────────────────────
    SCRATCH.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="sm01_freeze_", dir=str(SCRATCH)))
    try:
        with TestClient(m.app) as client:
            api_main, fake, guard, kb, audit_factory, dirs = build_stack(tmp_root)
            try:
                for case, frozen in zip(CASES, baseline.get("cases") or []):
                    try:
                        obs = collect(client, fake, api_main, audit_factory, case)
                    except Exception as ex:
                        record(f"case {case['id']}", False,
                               f"回放异常 {type(ex).__name__}: {str(ex)[:200]}")
                        continue
                    diffs = []
                    for section in ("chat", "stream", "parity"):
                        diffs += deep_diff(frozen.get(section) or {}, obs[section], section)
                    # 不变性哨兵：观察里不允许混入计时字段
                    leak = [k for k in ("latency_ms", "total_ms") if k in obs["chat"]]
                    diffs += [f"chat.{k}: 非冻结字段混入观测" for k in leak]
                    record(f"case {case['id']}", not diffs, "; ".join(diffs[:6])[:500])
                    if diffs:
                        for d in diffs[6:12]:
                            print(f"    … {d}")
            finally:
                release_stack(guard, kb, api_main, dirs)
    finally:
        gc.collect()
        shutil.rmtree(str(tmp_root), ignore_errors=True)

    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} PASSED")
    return 0 if passed == total else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="SM-01 G4：/chat 与 /chat/stream 假模型层基线冻结")
    ap.add_argument("--capture", metavar="PATH", default="",
                    help="采集模式：把当前行为冻结为基线 JSON（默认回放模式）")
    ap.add_argument("--force", action="store_true", help="采集模式允许覆盖已存在产物")
    args = ap.parse_args()
    # 环境钉在进入任何产品导入之前（capture/replay 都要先钉，存储旋钮用独立临时根）
    SCRATCH.mkdir(parents=True, exist_ok=True)
    pre_tmp = Path(tempfile.mkdtemp(prefix="sm01_freeze_env_", dir=str(SCRATCH)))
    pin_env(pre_tmp)
    try:
        if args.capture:
            return run_capture(Path(args.capture), args.force)
        return run_replay()
    finally:
        shutil.rmtree(str(pre_tmp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
