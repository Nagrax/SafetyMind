"""
亮点：真实工具执行回路（P0-A）——统一工具边界与预算治理

问题：BaseAgent 只把 system/messages 发给 LLM 并直接抽文本，模型无法真正调用
已注册工具（knowledge_search / msds_lookup / resource_search / ledger_checklist），
回答里的"已查询"只是文字，没有证据闭环。

本模块把执行治理收到统一边界（专家只提调用意图，执行器控权限与副作用）：
  1. 只读白名单 —— 只有 allowlist 内的工具可被模型请求；越权请求直接拒绝
     （不执行，回填 is_error 的 tool_result，让模型据此纠正或放弃）。
  2. 双预算 —— max_rounds（模型往返轮数）与 max_calls（工具调用次数），
     超限即终止并如实标记 degraded，不做无限纠正循环。
  3. 统一执行 —— 一律经 MCPToolManager.call 执行：参数 JSON Schema 校验、
     熔断、单工具超时（tool.timeout_s）、缓存、fallback 全部复用既有治理。
  4. 降级可用 —— 服务端不支持 tools 时退回无工具单轮调用；异常/超预算与成功
     终答严格区分，绝不因工具不可用让整次对话失败。

协议（Anthropic）：messages.create(..., tools=[...]) → content 中读取 tool_use
块 → 执行 → 以 {"type":"tool_result","tool_use_id",...} 回填 → 继续调用，
直到没有 tool_use（end_turn）或到预算。
"""
import asyncio
import copy
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from core.llm_utils import extract_text_content
from core.llm_params import (is_thinking_unsupported_error, messages_create_kwargs_base,
                             without_thinking)

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class ToolCallRecord:
    """单次工具调用的可追溯记录（进 tool_trace，不直接回灌给模型）。"""
    tool_use_id:   str
    name:          str
    params:        Dict[str, Any]
    ok:            bool
    error:         Optional[str]
    latency_ms:    float
    result_preview: str        # 截断后的可读预览（不长传）


@dataclass
class ExecutionResult:
    """一次工具回路的结果。terminated 取值：
    end_turn / max_tokens / budget_rounds / budget_calls / tools_unsupported / error。
    成功终答 = terminated=="end_turn" 且未 degraded 且 text 非空。
    """
    text:          str
    tool_trace:    List[ToolCallRecord]
    rounds:        int
    calls:         int
    terminated:    str
    degraded:      bool = False
    error:         Optional[str] = None


# ── 协议小工具（兼容 SDK 对象块与 dict 块）────────────────────────────────────

def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _response_blocks(response: Any) -> List[Any]:
    return list(_field(response, "content") or [])


def _tool_result_block(tool_use_id: str, content: str, is_error: bool) -> Dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_use_id,
            "content": content, "is_error": bool(is_error)}


# ── 工具执行器 ────────────────────────────────────────────────────────────────

class ToolExecutor:
    """把"专家提出工具请求 → 经 ToolManager 执行 → 回填结果 → 续答"封成一条回路。"""

    def __init__(
        self,
        client: Any,
        model: str,
        tool_manager: Any,
        *,
        allowlist: Sequence[str],
        max_rounds: int = 3,
        max_calls: int = 4,
        max_tokens: Optional[int] = None,
        result_preview_chars: int = 800,
        exec_context: Optional[Dict[str, Any]] = None,
    ):
        # 输出预算（2026-10-29）：默认 1024 保持原行为；SAFETYMIND_MAX_TOKENS 可在不改代码
        # 的前提下测"调大预算能否消掉截断"——判定/分级类问题实测 15%（3/20）被截断，
        # 且被截断时正文里会留下模型自己的作答提纲（见 eval_truncation.py 的产物）。
        if max_tokens is None:
            try:
                max_tokens = int(os.getenv("SAFETYMIND_MAX_TOKENS", "1024"))
            except ValueError:
                max_tokens = 1024
        self._client = client
        self._model = model
        self._tool_manager = tool_manager
        self._allowlist = list(dict.fromkeys(allowlist or []))
        self._max_rounds = max(1, int(max_rounds))
        self._max_calls = max(0, int(max_calls))
        self._max_tokens = int(max_tokens)
        self._result_preview_chars = max(1, int(result_preview_chars))
        self._exec_context = exec_context

    # ── schema 暴露 ───────────────────────────────────────────────────────────

    def tool_schemas(self) -> List[Dict[str, Any]]:
        """把 allowlist 内、tool_manager 已注册的工具转成 Anthropic tools 格式。

        顺序稳定（按 allowlist 顺序）；未注册的 allowlist 名直接跳过。
        """
        registered: Dict[str, Any] = getattr(self._tool_manager, "_tools", {}) or {}
        schemas: List[Dict[str, Any]] = []
        for name in self._allowlist:
            tool = registered.get(name)
            if tool is None:
                continue
            schemas.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": copy.deepcopy(tool.schema),
            })
        return schemas

    # ── 主回路 ────────────────────────────────────────────────────────────────

    async def run(self, *, system: str, messages: List[Dict[str, Any]]) -> ExecutionResult:
        """
        执行工具回路：模型往返 → 执行 tool_use（经 ToolManager）→ 回填 tool_result
        → 继续，直到 end_turn 或预算耗尽。不缓存 LLM 响应（工具结果缓存由
        ToolManager 负责）。
        """
        schemas = self.tool_schemas()
        msgs: List[Dict[str, Any]] = [dict(m) for m in (messages or [])]
        trace: List[ToolCallRecord] = []
        calls = 0
        rounds = 0
        degraded = False
        last_text = ""

        for round_idx in range(self._max_rounds):
            rounds += 1
            kwargs: Dict[str, Any] = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "system": system,
                "messages": msgs,
                **messages_create_kwargs_base(),
            }
            if schemas:
                kwargs["tools"] = schemas

            try:
                response = await self._create(**kwargs)
            except Exception as ex:
                if round_idx == 0:
                    # 首轮失败：可能是服务端不支持 tools，也可能端点故障。
                    # 统一尝试一次无工具单轮调用——保证对话不因工具不可用而失败。
                    return await self._degrade_without_tools(system, messages, ex, rounds)
                logger.warning(f"ToolExecutor 第 {rounds} 轮 LLM 调用失败，终止工具回路: {ex}")
                return ExecutionResult(
                    text=self._ensure_text(last_text), tool_trace=trace, rounds=rounds,
                    calls=calls, terminated="error", degraded=True,
                    error=f"LLM 调用失败: {ex}",
                )

            blocks = _response_blocks(response)
            text = extract_text_content(blocks)
            if text:
                last_text = text
            tool_uses = [block for block in blocks if _field(block, "type") == "tool_use"]
            stop_reason = _field(response, "stop_reason")
            # 与工具请求同时出现的那段文字是**过程话术**（"我再查一下…"），不是终答。
            # 实测（2026-10-29）：预算恰好在"说了要做、还没做"时耗尽，这句话会被当成
            # 最终答复交给用户（真实 /chat 20 条里 2 条只有 21/25 字）。
            last_text_is_interim = bool(text and tool_uses)

            if not tool_uses:
                if stop_reason == "max_tokens":
                    # 截断不是成功终答：如实标注 degraded 并在正文里说明。
                    return ExecutionResult(
                        text=self._notify_truncated(text), tool_trace=trace, rounds=rounds,
                        calls=calls, terminated="max_tokens", degraded=True,
                        error="stop_reason=max_tokens：输出被长度上限截断",
                    )
                return ExecutionResult(
                    text=text, tool_trace=trace, rounds=rounds, calls=calls,
                    terminated="end_turn", degraded=degraded,
                )

            msgs.append({"role": "assistant", "content": blocks})
            tool_results: List[Dict[str, Any]] = []
            budget_exceeded = False

            # 同一轮内的多个 tool_use 并行执行（2026-10-26 延迟优化）。
            #
            # 为什么：实测工具类 /chat 比无工具类多 ~15.5s，而其中一部分是
            # 「模型在同一轮里同时请求 2~3 个工具，执行器却串行跑」——
            # msds_lookup×2 + knowledge_search 三个只读调用本可并发。
            # 这些工具都是只读、无副作用、彼此不依赖（没有工具参数引用另一个工具结果），
            # 因此并发安全；预算仍按 calls 逐个计数，先占先得。
            #
            # 保守边界：只对**白名单内且预算允许**的调用并发；越权/超预算的立刻
            # 就地拒绝（不并发、不等待），保持原有语义。
            async def _exec_one(block: Any) -> tuple:
                """执行单个 tool_use；返回 (tool_use_id, name, params, result|None, error|None, latency_ms, denied_reason|None)"""
                tid = _field(block, "id") or ""
                nm = _field(block, "name") or ""
                pr = _field(block, "input")
                if not isinstance(pr, dict):
                    pr = {}
                if nm not in self._allowlist:
                    return (tid, nm, pr, None, None, 0.0, "whitelist")
                started = time.monotonic()
                try:
                    res = await self._tool_manager.call(
                        nm, pr, self._exec_context, use_cache=True)
                    return (tid, nm, pr, res, None, (time.monotonic() - started) * 1000, None)
                except Exception as ex:   # ToolManager 内部已消化常规错误，这里兜底
                    return (tid, nm, pr, None, ex, (time.monotonic() - started) * 1000, None)

            # 先按预算切分：能执行的并发，不能执行的就地拒绝
            #
            # 重要：trace 与 tool_results 的顺序必须**与模型给出的 tool_use 顺序一致**
            # （可追溯性约定，tests/test_tool_execution.py t4 断言 trace[1] 是超预算项）。
            # 并发执行会打乱完成顺序，所以这里用「占位槽」：先按原顺序建好列表，
            # 并发结果按 tool_use_id 回填到对应槽位。
            runnable: List[Any] = []
            slots: List[Any] = []   # 每项：{"denied": trace_record|None, "run": block|None}
            for block in tool_uses:
                tid = _field(block, "id") or ""
                nm = _field(block, "name") or ""
                pr = _field(block, "input")
                if not isinstance(pr, dict):
                    pr = {}
                if nm not in self._allowlist:
                    logger.warning(f"ToolExecutor 拒绝白名单外工具请求: {nm}")
                    rec = ToolCallRecord(tool_use_id=tid, name=nm, params=pr,
                                         ok=False, error="工具不在白名单", latency_ms=0.0,
                                         result_preview="")
                    trace.append(rec)
                    tool_results.append(_tool_result_block(
                        tid, f"工具 {nm} 不在本次允许调用的白名单内，已拒绝执行。", True))
                    slots.append({"rec": rec, "block": None, "kind": "whitelist"})
                    continue
                if calls + len(runnable) >= self._max_calls:
                    rec = ToolCallRecord(tool_use_id=tid, name=nm, params=pr,
                                         ok=False, error="超出调用预算（未执行）", latency_ms=0.0,
                                         result_preview="")
                    trace.append(rec)
                    budget_exceeded = True
                    slots.append({"rec": rec, "block": None, "kind": "budget"})
                    continue
                runnable.append(block)
                slots.append({"rec": None, "block": block, "kind": "run"})

            # 并发执行本轮可运行的工具
            outcomes = await asyncio.gather(*[_exec_one(b) for b in runnable]) if runnable else []
            by_id = {o[0]: o for o in outcomes}

            # 按原顺序回填：并发结果按 tool_use_id 归位，trace 顺序因此与模型请求一致
            filled: List[int] = []   # 记录哪些槽位已补齐（用于重建 trace 顺序）
            for slot in slots:
                if slot["kind"] != "run":
                    continue
                block = slot["block"]
                tid = _field(block, "id") or ""
                nm = _field(block, "name") or ""
                pr = _field(block, "input")
                if not isinstance(pr, dict):
                    pr = {}
                (_, _n, _p, res, exc, latency, _denied) = by_id.get(
                    tid, (tid, nm, pr, None, RuntimeError("并发结果缺失"), 0.0, None))
                calls += 1
                if exc is not None:
                    degraded = True
                    logger.error(f"ToolExecutor 工具调用异常: {nm} — {exc}")
                    rec = ToolCallRecord(tool_use_id=tid, name=nm, params=pr,
                                         ok=False, error=f"工具调用异常: {exc}",
                                         latency_ms=latency, result_preview="")
                    trace.append(rec)
                    tool_results.append(_tool_result_block(
                        tid, self._clean(f"工具 {nm} 调用异常，未获得结果: {exc}"), True))
                    slot["rec"] = rec
                    continue

                result = res
                success = bool(_field(result, "success", False))
                result_error = _field(result, "error")
                payload = self._serialize(_field(result, "data"))
                rec = ToolCallRecord(tool_use_id=tid, name=nm, params=pr,
                                     ok=success, error=result_error, latency_ms=latency,
                                     result_preview=self._truncate(payload))
                trace.append(rec)
                slot["rec"] = rec

                # fallback（success=True 但带 error）同样标记 is_error：
                # 降级说明不是检索证据，模型不得据此断言事实。
                is_error = (not success) or bool(result_error)
                if is_error:
                    degraded = True
                if not success:
                    body = f"{result_error}\n{payload}".strip() if result_error else payload
                elif result_error:
                    body = f"[降级结果，非检索证据] {result_error}\n{payload}".strip()
                else:
                    body = payload
                tool_results.append(_tool_result_block(
                    tid, self._truncate(body), is_error))

            # 修正顺序：trace 与 tool_results 均按 slots 原顺序重排
            ordered_recs = [s["rec"] for s in slots if s["rec"] is not None]
            if ordered_recs:
                trace[:] = ordered_recs

            # ── 关于"跳过第二轮以省延迟"：实测后决定**不做**（保留记录防重犯）──
            #
            # 设想：单工具确定性查询时直接模板化工具结果返回，省掉第二次 LLM 往返。
            # 实测（benchmarks/compare_tool_roundtrip.py --live）：
            #   A 两轮 11609ms → 222 字，含"仅供参考/储存建议/作业环境控制"等安全提示
            #   B 一轮  5304ms → 154 字，但输出是**原始 JSON**（2/2 带 JSON 味）
            #   即：省 54% 延迟，代价是回答不可读、且丢失安全提示。
            #
            # 更关键：一旦启用会破坏 5 条既有契约（本轮真跑出来才发现的）：
            #   - t1 端到端工具回路期望"工具执行后由模型给终答"
            #   - max_rounds=1 时应 terminated=budget_rounds + degraded，而非 end_turn
            #   - BaseAgent 接 executor 时期望走 2 轮
            #   - 门面 run() 的 tool_trace 与终答来源语义
            # 结论：**6.3s 的收益不值得破坏轮次语义与终答契约**。已回退。
            #
            # 真正可行的方向（未做，列为后续）：在 /chat 层做"确定性查询前置分流"——
            # 对 CAS 号/台账编号这类强格式查询，由 API 层决定是否需要模型参与，
            # 而不是在 ToolExecutor 内部偷工减轮。
            msgs.append({"role": "user", "content": tool_results})

            if budget_exceeded:
                return ExecutionResult(
                    text=self._finalize_budget_text(last_text, last_text_is_interim,
                                                    tool_results),
                    tool_trace=trace, rounds=rounds, calls=calls,
                    terminated="budget_calls", degraded=True)
            if round_idx == self._max_rounds - 1:
                return ExecutionResult(
                    text=self._finalize_budget_text(last_text, last_text_is_interim,
                                                    tool_results),
                    tool_trace=trace, rounds=rounds, calls=calls,
                    terminated="budget_rounds", degraded=True)

        # 理论上每轮都会 return；兜底不留悬挂状态。
        return ExecutionResult(text=self._ensure_text(last_text), tool_trace=trace,
                               rounds=rounds, calls=calls,
                               terminated="budget_rounds", degraded=True)

    # ── LLM 调用（思考字段兼容层）─────────────────────────────────────────────

    async def _create(self, **kwargs: Any) -> Any:
        """统一的 messages.create：端点不接受 thinking 字段时自动去掉重试一次。

        为什么要这一层（2026-10-25 闭环实测）：某些 Anthropic 兼容端点会把多余的
        thinking 字段当非法入参 400；而官方模型会忽略该字段。若不做兜底，
        一个纯兼容性问题就会让整次对话失败——那是比"思考吃光预算"更严重的问题。
        """
        try:
            return await self._client.messages.create(**kwargs)
        except Exception as ex:
            if not is_thinking_unsupported_error(ex):
                raise
            logger.warning(f"端点不接受 thinking 字段，去掉后重试一次: {ex}")
            return await self._client.messages.create(**without_thinking(kwargs))

    # ── 降级路径 ──────────────────────────────────────────────────────────────

    async def _degrade_without_tools(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        exc: Exception,
        rounds: int,
    ) -> ExecutionResult:
        """服务端不支持 tools（或其他首轮失败）：退回无工具单轮调用，保证可用文本。"""
        try:
            response = await self._create(
                model=self._model, max_tokens=self._max_tokens, system=system,
                messages=[dict(m) for m in (messages or [])],
                **messages_create_kwargs_base(),
            )
            text = extract_text_content(_response_blocks(response))
        except Exception as retry_ex:
            logger.error(f"ToolExecutor 无工具降级调用仍失败: {retry_ex}")
            return ExecutionResult(
                text="", tool_trace=[], rounds=rounds, calls=0,
                terminated="error", degraded=True,
                error=f"工具调用失败: {exc}; 无工具降级失败: {retry_ex}",
            )
        logger.warning(f"ToolExecutor 工具不可用，降级为无工具单轮调用: {exc}")
        return ExecutionResult(
            text=self._clean(text) or "（本次未能启用工具调用，已按无工具模式回答。）",
            tool_trace=[], rounds=rounds, calls=0,
            terminated="tools_unsupported", degraded=True, error=str(exc),
        )

    # ── 文本与结果处理 ────────────────────────────────────────────────────────

    @staticmethod
    def _clean(value: Any) -> str:
        """移除 Unicode 代理字符，避免 LLM 请求编码失败（与 ToolManager 同款）。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    def _serialize(self, data: Any) -> str:
        if data is None:
            return ""
        if isinstance(data, str):
            return self._clean(data)
        try:
            return self._clean(json.dumps(data, ensure_ascii=False, default=str))
        except Exception:
            return self._clean(str(data))

    def _truncate(self, text: str) -> str:
        if len(text) <= self._result_preview_chars:
            return text
        return text[: self._result_preview_chars] + "…（已截断）"

    @staticmethod
    def _ensure_text(text: str) -> str:
        return text or "工具调用未在预算内完成，未获得可用的最终回答。"

    def _finalize_budget_text(self, last_text: str, last_text_is_interim: bool,
                              tool_results: List[Dict[str, Any]]) -> str:
        """工具预算耗尽时的收尾文本。

        规则（2026-10-29 实测驱动）：
          - 最后一段文字与工具请求同时出现 → 它是**过程话术**（"我再做一次确认"），
            不能当答案交付；有工具证据就交证据摘要，没有就如实说没答出来；
          - 真正的终答（没有同时请求工具）→ 原样返回（不做任何改写）；
          - 没有文字但有证据 → 交证据摘要（既有行为，见 _partial_evidence_answer）。
        不额外发起 LLM 调用：轮次预算为 1 的契约（"不再请求模型"）必须保持不变。
        """
        if last_text_is_interim:
            return self._partial_evidence_answer(tool_results) or self._ensure_text("")
        return self._ensure_text(last_text) if last_text else (
            self._partial_evidence_answer(tool_results) or self._ensure_text(""))

    @staticmethod
    def _partial_evidence_answer(tool_results: List[Dict[str, Any]]) -> str:
        """预算耗尽但已取到工具结果时，交出**未整理但有用**的证据，而不是交白卷。

        为什么（2026-10-28 并发/负载实测）：模型慢（不是失败）时工具轮次预算会被
        烧光，此时 last_text 为空，旧版直接回"工具调用未在预算内完成，未获得可用的
        最终回答。"——用户等了 44s 什么也没拿到。而实际上工具已经返回了真实资料，
        交给用户自己看远好过交白卷。

        三条纪律：
          1. **如实标注未整理**：明说"未能完成最终整理"，不假装这是成稿；
          2. 只摘录成功结果（is_error 的降级说明不是证据，不得混入）；
          3. 长度封顶，避免把原始 JSON 灌给用户。
        返回空串则调用方回落到 _ensure_text 的固定文案（无证据时仍如实说没有）。
        """
        if not tool_results:
            return ""
        chunks: List[str] = []
        for blk in tool_results:
            if not isinstance(blk, dict) or blk.get("is_error"):
                continue
            raw = blk.get("content")
            if isinstance(raw, list):
                raw = "\n".join(x.get("text", "") for x in raw if isinstance(x, dict))
            text = str(raw or "").strip()
            if not text:
                continue
            chunks.append(text[:600])
        if not chunks:
            return ""
        body = "\n\n".join(chunks)[:1500]
        return ("（说明：本次未能完成最终整理——模型在工具轮次预算内没有产出终答；"
                "以下是已从系统内取到的原始资料，可直接参考，未经过改写与归纳。）\n\n"
                + body)

    @staticmethod
    def _notify_truncated(text: str) -> str:
        """截断时的对外文案。

        关键改进（2026-10-25 闭环实测）：思考块可能吃光整个输出预算，此时 text 为空，
        旧版只会回一句「（注意：本次回答因达到长度上限被截断，未完整生成。）」——
        用户拿不到任何答案，等于服务不可用。现在区分为两种情况：
          - 有部分正文：附一句截断提示；
          - 完全没有正文：明确告知「未能生成答案」并给出可执行的下一步，而不是假装有回答。
        """
        note = "（注意：本次回答因达到长度上限被截断，未完整生成。）"
        if text:
            return f"{text}\n\n{note}".strip()
        return (
            "抱歉，本次未能生成完整回答（模型输出预算在推理阶段耗尽，正文未产出）。\n\n"
            "建议：把问题拆小一些再问，或稍后重试；紧急情况请直接拨打应急电话或转人工。"
        )
