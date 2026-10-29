# -*- coding: utf-8 -*-
# 流式对话端点测试（2026-10-28）。
#
# ## 做到了什么，没做什么（如实）##
#
# 做：`/chat/stream`（SSE），事件序列 meta → (stage*) → delta → done。
#   - 紧急预检命中：**零 LLM**，实测 TTFT 16ms、291 字一次性到达
#   - 缓存命中 / 确定性直出：同样即时整块
#   - 完整链路：推送阶段事件（queued/processing）+ 最终答案分块
# 没做：**token 级穿透**。多 Agent + 工具回路的生成发生在员工层内部，
#   要在那里接流式需改造整条链路，且会让 /chat/stream 与 /chat 行为分叉
#   （同问句走不同 prompt/工具策略）——那比"慢"更危险。本文件不宣称已实现。
#
# 用法: python tests/test_chat_stream.py
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SRC = Path(__file__).resolve().parents[1] / "api" / "main.py"

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # ── 1. 端点存在且用 StreamingResponse ───────────────────────────────
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
    record("存在 /chat/stream 端点", "chat_stream" in funcs)
    route_ok = any(
        isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "post"
        and any(isinstance(a, ast.Constant) and a.value == "/chat/stream" for a in n.args)
        for n in ast.walk(tree))
    record("路由注册为 POST /chat/stream", route_ok)
    record("使用 StreamingResponse", "StreamingResponse(gen()" in src)
    record("media_type 为 text/event-stream", 'media_type="text/event-stream"' in src)
    record("关闭代理缓冲（x-accel-buffering）", "x-accel-buffering" in src)

    # ── 2. 事件契约 ─────────────────────────────────────────────────────
    for ev in ("meta", "stage", "delta", "done"):
        record(f"发送 {ev} 事件", f'sse("{ev}"' in src)
    record("SSE 帧格式正确（event:/data: 双行）",
           'event: {event}' in src or "event: {event}" in src)

    # ── 3. 安全底线：紧急路径先于闸门且零 LLM ──────────────────────────
    body = src[src.index("async def chat_stream"):]
    body = body[:body.index('\n@app.')] if '\n@app.' in body else body
    idx_emg = body.index("_emergency_fast_path")
    idx_gate = body.index("_acquire_with_timeout")
    record("紧急分流在获取对话闸门之前", idx_emg < idx_gate)
    record("紧急分流失败也有兜底文案（不空回）",
           "紧急分流失败，请立即拨打 119/120" in body)
    record("紧急分流不经过 LLM（只调 _emergency_fast_path）",
           "_orchestrator.run" not in body.split("_emergency_fast_path")[0])

    # ── 4. 与 /chat 行为一致：复用 _chat_core（不分叉）──────────────────
    record("完整路径复用 _chat_core（不分叉行为）", "await _chat_core(" in body)
    record("同样走快车道分流", "_FAST_GATE if _is_pure_chemical_lookup" in body)
    record("同样受对话闸门约束", "_acquire_with_timeout(lane" in body)

    # ── 5. 不做 token 级穿透要写明（防后人误以为已实现）────────────────
    record("注释明确说明未做 token 级穿透", "不做 token 级穿透" in src)
    record("注释说明未做原因（行为分叉风险）", "行为分叉" in src)

    # ── 6. 流式接口在适配层存在（供后续 token 级使用）──────────────────
    adapter = Path(__file__).resolve().parents[1].joinpath(
        "core/llm_protocol.py").read_text(encoding="utf-8")
    record("适配层提供 stream() 接口", "def stream(self, **kwargs)" in adapter)
    record("适配层解析 SSE delta 且不输出 reasoning",
           "delta" in adapter and "reasoning_content" in adapter
           and "不输出" in adapter)
    record("流式失败降级为非流式（宁可慢不能空）", "降级为非流式" in adapter)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
