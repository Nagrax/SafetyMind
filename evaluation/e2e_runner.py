# -*- coding: utf-8 -*-
# S-06 评测侧：经过真实应用入口的集成评测（evaluation/e2e_runner.py）
#
# 解决的问题（改造前缺陷）：
#   既有 EndToEndEvaluator 直调 orchestrator.run()，绕过 /chat 的 RAG、记忆、工具回路、
#   建单与通知队列；RED 结论只附在 metadata；每次运行还会无条件覆盖评测基线。
#   本模块换成"走真实入口"的集成评测：所有断言（citations / tools_used / knowledge_used /
#   escalated / hazard_action / 紧急快速分流 / 时延 / 工单与通知状态）**全部来自本次实际执行**，
#   并在每个 case 上并联跑分层 Rubric（含 RED 安全红线），产出可被发布门禁消费的报告。
#
# 入口设计：
#   chat_fn 缺省 = 真实应用入口的服务层（api.main.chat 端点协程，含 /chat 的完整主链：
#   紧急预检 → 记忆 → 意图 → RAG → 确定性资源工具 → 生成 → 审计/建单/通知）。
#   为什么不用 TestClient 一次跑多条：TestClient 每个请求新建事件循环，而 /chat 持有模块级
#   asyncio.Semaphore（惰性绑定事件循环），跨循环会抛错；单事件循环内直调端点协程等价于
#   走真实入口且不需要 lifespan（不需要真实模型、数据库与外部服务）。
#   chat_fn 也可注入任意可调用对象（测试打桩 LLM，或 main.py 自行接 HTTP 客户端）；
#   注入项只要返回 ChatResponse / 等价的字段映射均可，字段按名提取。
#
# 零第三方依赖（api.main 仅在使用真实入口时才惰性导入）。
import asyncio
import inspect
import logging
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from evaluation.profiles import resolve_profile
from evaluation.release_gate import per_class_summary
from evaluation.rubric import CLARIFY_PATTERNS, evaluate_funnel

logger = logging.getLogger(__name__)

FAILURE_RED = "RED"
ENTRY_SERVICE_LAYER = "app-service-layer"
ENTRY_INJECTED = "injected-chat-fn"
DEFAULT_USER_ID = "eval-e2e"

# expect 支持的断言键（未知键一律报错，不静默忽略）
EXPECT_KEYS = (
    "intent",
    "escalated",
    "min_citations",
    "require_tools",
    "forbid_substrings",
    "require_substrings",
    "not_escalated_from_clarify",
    "max_total_ms",
)

_CLARIFY_RX = re.compile("|".join(CLARIFY_PATTERNS), re.IGNORECASE)


# ── case 校验 ─────────────────────────────────────────────────────────────────

def validate_cases(cases: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """校验 case 结构；未知 expect 键 / 非法字段直接报错（fail-loud，不静默降级）。"""
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)):
        raise TypeError("cases 必须是序列")
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for idx, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise TypeError(f"第 {idx} 条 case 必须是映射")
        case_id = case.get("id")
        if not case_id or not str(case_id).strip():
            raise ValueError(f"第 {idx} 条 case 缺 id")
        case_id = str(case_id)
        if case_id in seen:
            raise ValueError(f"case id 重复：{case_id}")
        seen.add(case_id)
        message = case.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(f"case {case_id} 缺 message（非空字符串）")
        expect = case.get("expect") or {}
        if not isinstance(expect, Mapping):
            raise TypeError(f"case {case_id} 的 expect 必须是映射")
        unknown = [k for k in expect if k not in EXPECT_KEYS]
        if unknown:
            raise ValueError(f"case {case_id} 出现未知 expect 键 {unknown}，只允许 {list(EXPECT_KEYS)}")
        out.append({"id": case_id, "message": message, "expect": dict(expect)})
    return out


# ── 响应归一化（真实端点对象 / JSON 映射均可）──────────────────────────────────

def _as_mapping(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except TypeError:
            return dict(dump(exclude_none=True))
    return dict(getattr(raw, "__dict__", {}) or {})


def _get(raw: Any, name: str, default: Any) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(name, default)
    return getattr(raw, name, default)


# ── 真实入口（缺省）────────────────────────────────────────────────────────────

def _default_chat_fn(user_id: str = DEFAULT_USER_ID) -> Callable[[str], Any]:
    """缺省入口：api.main.chat 端点协程（同一应用服务层，/chat 完整主链）。"""
    from api import main as api_main   # 惰性导入：仅真实入口需要

    async def _call(message: str) -> Any:
        return await api_main.chat(api_main.ChatRequest(message=message, user_id=user_id))

    return _call


# ── 工单 / 通知状态读取（若可读；best-effort，不阻断评测）──────────────────────

def _read_system_state(request_id: str, conv_id: str) -> Dict[str, Any]:
    state: Dict[str, Any] = {"ticket": None, "notifications": None}
    if not request_id:
        return state
    try:
        from api import main as api_main
    except Exception:
        return state
    audit = getattr(api_main, "_audit", None)
    if audit is None:
        return state
    try:
        ticket_id = audit.find_ticket_by_request(request_id)
        if ticket_id:
            t = audit.get_ticket(int(ticket_id)) or {}
            state["ticket"] = {
                "ticket_id": int(ticket_id),
                "status": t.get("status", ""),
                "type": t.get("type", ""),
                "priority": t.get("priority", ""),
            }
        rows = audit.list_notifications(limit=200) or []
        mine = [r for r in rows if str(r.get("idempotency_key", "")).endswith(f":{request_id}:webhook")
                or r.get("request_id") == request_id]
        if mine:
            state["notifications"] = [
                {"idempotency_key": r.get("idempotency_key", ""), "status": r.get("status", ""),
                 "attempts": r.get("attempts", 0)} for r in mine]
    except Exception as ex:                       # 状态读取失败不影响评测结论
        logger.warning("评测读取工单/通知状态失败（忽略）：%s", ex)
    return state


# ── 断言 ──────────────────────────────────────────────────────────────────────

def _check_expect(trace: Mapping[str, Any], expect: Mapping[str, Any]) -> List[Dict[str, str]]:
    """按 expect 逐项断言；返回失败列表 [{rule, detail}]（空 = 全部通过）。"""
    failures: List[Dict[str, str]] = []
    response = str(trace.get("response") or "")
    citations = list(trace.get("citations") or [])
    tools = list(trace.get("tools_used") or [])
    names = {str(t.get("name")) for t in tools if isinstance(t, Mapping)}
    ok_names = {str(t.get("name")) for t in tools
                if isinstance(t, Mapping) and t.get("ok") is not False}

    if "intent" in expect and str(trace.get("intent") or "") != str(expect["intent"]):
        failures.append({"rule": "intent",
                         "detail": f"期望 {expect['intent']}，实际 {trace.get('intent')}"})
    if "escalated" in expect and bool(trace.get("escalated")) != bool(expect["escalated"]):
        failures.append({"rule": "escalated",
                         "detail": f"期望 {bool(expect['escalated'])}，实际 {bool(trace.get('escalated'))}"})
    if "min_citations" in expect and len(citations) < int(expect["min_citations"]):
        failures.append({"rule": "min_citations",
                         "detail": f"期望 ≥{expect['min_citations']} 条，实际 {len(citations)} 条"})
    if "require_tools" in expect:
        want = [str(n) for n in expect["require_tools"]]
        missing = [n for n in want if n not in names or n not in ok_names]
        if missing:
            failures.append({"rule": "require_tools",
                             "detail": f"缺失或未成功的工具 {missing}（本次轨迹 {sorted(names)}）"})
    if "forbid_substrings" in expect:
        hit = [s for s in expect["forbid_substrings"] if str(s) in response]
        if hit:
            failures.append({"rule": "forbid_substrings", "detail": f"响应出现禁用片段 {hit}"})
    if "require_substrings" in expect:
        miss = [s for s in expect["require_substrings"] if str(s) not in response]
        if miss:
            failures.append({"rule": "require_substrings", "detail": f"响应缺少必需片段 {miss}"})
    if expect.get("not_escalated_from_clarify") and _CLARIFY_RX.search(response):
        failures.append({"rule": "not_escalated_from_clarify",
                         "detail": "响应是澄清式追问（本应为处置/答复）"})
    if "max_total_ms" in expect:
        total = float(trace.get("total_ms") or 0.0)
        if total > float(expect["max_total_ms"]):
            failures.append({"rule": "max_total_ms",
                             "detail": f"{total:.1f}ms > 上限 {expect['max_total_ms']}ms"})
    return failures


# ── 主入口 ────────────────────────────────────────────────────────────────────

def _prepare(cases: Sequence[Mapping[str, Any]],
             chat_fn: Optional[Callable[[str], Any]],
             profile: str) -> Dict[str, Any]:
    validated = validate_cases(cases)
    profile_obj = resolve_profile(None if profile in (None, "", "default") else profile)
    use_default = chat_fn is None
    chat = _default_chat_fn() if use_default else chat_fn
    entry = {
        "mode": ENTRY_SERVICE_LAYER if use_default else ENTRY_INJECTED,
        "endpoint": "/chat",
        "detail": ("api.main.chat 端点协程（含紧急预检/记忆/意图/RAG/确定性工具/建单/通知）"
                   if use_default else f"注入 chat_fn={getattr(chat, '__name__', type(chat).__name__)}"),
    }
    return {"validated": validated, "profile_obj": profile_obj, "chat": chat, "entry": entry}


async def _run_cases(validated: Sequence[Mapping[str, Any]],
                     chat: Callable[[str], Any]) -> Dict[str, Any]:
    """逐 case 走真实入口并采集实际产生的证据、工具轨迹、工单/通知状态与耗时。"""
    traces: List[Dict[str, Any]] = []
    red_failures: List[str] = []
    red_evidence: List[Dict[str, Any]] = []
    failure_counts: Dict[str, int] = {}
    latencies: List[float] = []

    async def _call(message: str) -> Any:
        res = chat(message)
        if inspect.isawaitable(res):
            res = await res
        return res

    for case in validated:
        case_id, message, expect = case["id"], case["message"], case["expect"]
        t0 = time.perf_counter()
        raw: Any = None
        error = ""
        try:
            raw = await _call(message)
        except Exception as ex:
            error = f"{type(ex).__name__}: {ex}"
            logger.warning("评测 case %s 调用真实入口失败：%s", case_id, error)
        wall_ms = round((time.perf_counter() - t0) * 1000, 1)

        response_text = str(_get(raw, "response", "") or "")
        total_ms = float(_get(raw, "total_ms", 0.0) or wall_ms)
        request_id = str(_get(raw, "request_id", "") or "")
        conv_id = str(_get(raw, "conv_id", "") or "")
        citations = list(_get(raw, "citations", []) or [])
        tools_used = list(_get(raw, "tools_used", []) or [])
        trace: Dict[str, Any] = {
            "id": case_id,
            "message": message,
            "expect": dict(expect),
            "expect_intent": str(expect.get("intent") or _get(raw, "intent", "") or ""),
            "intent": str(_get(raw, "intent", "") or ""),
            "response": response_text,
            "citations": citations,
            "tools_used": tools_used,
            "knowledge_used": bool(_get(raw, "knowledge_used", False)),
            "escalated": bool(_get(raw, "escalated", False)),
            "hazard_action": str(_get(raw, "hazard_action", "none") or "none"),
            "hazard_ticket_id": int(_get(raw, "hazard_ticket_id", 0) or 0),
            "emergency_fastpath": bool(_get(raw, "emergency_fastpath", False)),
            "total_ms": total_ms,
            "wall_ms": wall_ms,
            "latency_ms": float(_get(raw, "latency_ms", 0.0) or 0.0),
            "request_id": request_id,
            "conv_id": conv_id,
            "judge": None,          # 本轮不真调 LLM；由 main.py 的 judge 侧回填（若有）
            "error": error,
        }
        trace.update(_read_system_state(request_id, conv_id))

        # ── RED / 分层漏斗：复用既有 rubric，不重写；证据取自本次实际执行 ──
        evidence = "\n".join(
            str(c.get("snippet", "")) for c in citations
            if isinstance(c, Mapping) and c.get("snippet")
        ) or None
        verdict = evaluate_funnel(
            message, response_text,
            evidence=evidence,
            tool_expectations=list(expect.get("require_tools") or []) or None,
            tool_trace=[str(t.get("name")) for t in tools_used if isinstance(t, Mapping)],
            domain=trace["expect_intent"] or trace["intent"] or None,
        )
        trace["rubric"] = {
            "overall": verdict.overall,
            "failed_layer": verdict.failed_layer,
            "failed_subitems": list(verdict.failed_subitems),
            "na_layers": list(verdict.na_layers),
            "layers": [{"layer": l.layer, "status": l.status} for l in verdict.layers],
        }
        trace["red"] = {
            "status": verdict.red.status,
            "failed_subitems": [k for k, v in verdict.red.subitems.items() if not v],
            "reason": verdict.red.reason,
        }

        failures = []
        if error:
            failures.append({"rule": "entry_error", "detail": error})
        failures.extend(_check_expect(trace, expect))
        red_fail = verdict.red.status == "fail"
        if red_fail:
            failures.append({"rule": FAILURE_RED, "detail": verdict.red.reason})

        trace["passed"] = not failures
        trace["failures"] = failures
        traces.append(trace)
        latencies.append(total_ms)

        for f in failures:
            failure_counts[f["rule"]] = failure_counts.get(f["rule"], 0) + 1
        if red_fail:
            red_failures.append(case_id)
            red_evidence.append({
                "case_id": case_id,
                "message": message,
                "failed_subitems": trace["red"]["failed_subitems"],
                "reason": verdict.red.reason,
            })

    return {"traces": traces, "red_failures": red_failures, "red_evidence": red_evidence,
            "failure_counts": failure_counts, "latencies": latencies}


def _assemble_report(outcome: Mapping[str, Any],
                     prepared: Mapping[str, Any]) -> Dict[str, Any]:
    """逐 case 轨迹 → 汇总报告（含耗时分布、p95、每类混淆与关键漏检）。"""
    traces = list(outcome["traces"])
    latencies = list(outcome["latencies"])
    profile_obj = prepared["profile_obj"]
    total = len(traces)
    passed = sum(1 for t in traces if t["passed"])
    citation_cases = sum(1 for t in traces if t["citations"])
    sorted_lat = sorted(latencies) if latencies else [0.0]
    report: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "entry": prepared["entry"],
        "profile": {
            "name": profile_obj.name,
            "version": profile_obj.version,
            "model_mode": profile_obj.model_mode,
            "fallback": bool(profile_obj.fallback),
            "thresholds": dict(profile_obj.thresholds),
        },
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "red_failures": list(outcome["red_failures"]),
        "red_failure_count": len(outcome["red_failures"]),
        "red_evidence": list(outcome["red_evidence"]),
        "failure_counts": dict(outcome["failure_counts"]),
        "latency": {
            "n": total,
            "mean": round(sum(sorted_lat) / len(sorted_lat), 1),
            "p50": _percentile(sorted_lat, 50),
            "p95": _percentile(sorted_lat, 95),
            "max": round(max(sorted_lat), 1),
        },
        "citation_coverage": round(citation_cases / total, 4) if total else 0.0,
        "judge_avg": None,
        "cases": traces,
    }
    report["metrics"] = {
        "pass_rate": report["pass_rate"],
        "p95_ms": report["latency"]["p95"],
        "citation_coverage": report["citation_coverage"],
        "red_failure_count": report["red_failure_count"],
    }
    report["confusion"] = per_class_summary(report)
    report["misses"] = [
        {"case_id": t["id"], "expected_intent": t["expect_intent"],
         "failures": t["failures"]}
        for t in traces if not t["passed"]
    ]
    return report


async def run_e2e_async(cases: Sequence[Mapping[str, Any]],
                        *,
                        chat_fn: Optional[Callable[[str], Any]] = None,
                        profile: str = "default") -> Dict[str, Any]:
    """run_e2e 的 async 孪生：在 async 端点（如 /eval/run）内直接 await，避免事件循环冲突。"""
    prepared = _prepare(cases, chat_fn, profile)
    outcome = await _run_cases(prepared["validated"], prepared["chat"])
    return _assemble_report(outcome, prepared)


def run_e2e(cases: Sequence[Mapping[str, Any]],
            *,
            chat_fn: Optional[Callable[[str], Any]] = None,
            profile: str = "default") -> Dict[str, Any]:
    """经过真实应用入口跑一组 case，产出逐 case 轨迹 + 汇总报告（同步版）。

    cases: [{id, message, expect: {...}}]，expect 支持见 EXPECT_KEYS，未知键报错。
    chat_fn: 可注入（测试打桩 LLM / main.py 自接 HTTP 入口）；缺省走真实 /chat 服务层。
    profile: 部署 profile 名（"default" = 按环境/内置默认解析），只记录在报告里，
             实际判定由 evaluation.release_gate.evaluate_report_gate 消费本报告完成。

    返回报告（dict）：
      entry / profile / total / passed / failed / pass_rate / red_failures / red_evidence /
      red_failure_count / failure_counts / latency{p50,p95,max} / citation_coverage /
      metrics{pass_rate,p95_ms,citation_coverage,red_failure_count} / confusion / misses /
      cases[逐 case 轨迹：response/citations/tools_used/knowledge_used/escalated/
      hazard_action/emergency_fastpath/total_ms/工单与通知状态/rubric/red/failures]

    注意：本函数内部用 asyncio.run，不能在运行中的事件循环内调用；async 端点请用 run_e2e_async。
    """
    prepared = _prepare(cases, chat_fn, profile)
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if in_loop:
        raise RuntimeError(
            "run_e2e 不能在运行中的事件循环内调用（内部 asyncio.run 会冲突）："
            "async 端点请改用 run_e2e_async，或把 run_e2e 放到线程里执行")
    outcome = asyncio.run(_run_cases(prepared["validated"], prepared["chat"]))
    return _assemble_report(outcome, prepared)


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """最近秩百分位（与既有评测脚本 p95 取法一致：下标 int(n*q/100)，越界夹到末位）。"""
    if not sorted_vals:
        return 0.0
    idx = int(len(sorted_vals) * q / 100.0)
    idx = min(max(idx, 0), len(sorted_vals) - 1)
    return round(float(sorted_vals[idx]), 1)
