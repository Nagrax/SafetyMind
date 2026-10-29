# -*- coding: utf-8 -*-
# /chat/stream 异常路径回归测试（2026-10-29 闭环第 43 轮）。
#
# ## 缺陷（读代码 + 实测推断）
#
# 排队超时（`_acquire_with_timeout` 抛 429）或生成阶段抛异常时，流式响应头早已发出，
# 旧行为是**直接打断生成器**：客户端只看到"流没了"（没有 done、没有错误事件），
# 日志里也几乎没有痕迹——这既是体验问题（用户不知道发生了什么），也是运维盲区
# （线上只会看到"有人断连"）。
#
# ## 修法
#
# 统一用 `event: error` + `event: done` 收尾：done 带可读正文，前端能像普通回答一样展示；
# CancelledError / GeneratorExit 不在此列（断连仍走 finally 释放容量，见
# test_stream_gate_leak.py）。
#
# ## 本测试锁定
#
# 1. 排队超时 → error(queue_timeout) + done，且容量不被泄漏
# 2. 生成异常 → error(internal_error) + done，且容量被释放
# 3. 正常路径不受影响（done 带 response / path=pipeline）
#
# 用法: python tests/test_stream_error_paths.py
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
# 排队上限压到 0.3s：让"排队超时"这条路径在测试里 1 秒内可复现
os.environ.setdefault("CHAT_QUEUE_TIMEOUT_S", "0.3")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def parse_events(text):
    out = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        ev, data = None, ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        out.append((ev, data))
    return out


async def drain(agen):
    frames = []
    try:
        async for chunk in agen:
            frames.append(chunk)
    except StopAsyncIteration:
        pass
    return parse_events("".join(frames))


async def main_async():
    import api.main as m
    import json as _json

    MSG = "动火作业票的有效期是怎么规定的？"
    orig_core = m._chat_core

    async with m.app.router.lifespan_context(m.app):
        gate = m._CHAT_GATE
        base = gate._value

        # ── 1. 排队超时：占满普通闸门，再发起流式请求 ────────────────────
        held = []
        for _ in range(base):
            await gate.acquire()
            held.append(1)
        try:
            resp = await m.chat_stream(m.ChatRequest(message=MSG, user_id="q"), "")
            events = await drain(resp.body_iterator)
        finally:
            for _ in held:
                gate.release()
        names = [e for e, _ in events]
        record("排队超时时先推 queued 阶段事件", "stage" in names, f"events={names}")
        record("排队超时发 error(queue_timeout) 事件", "error" in names, f"events={names}")
        record("排队超时最终仍发 done（客户端不会看到半截流）", names[-1:] == ["done"],
               f"events={names}")
        err = next((_json.loads(d) for e, d in events if e == "error"), {})
        done = next((_json.loads(d) for e, d in events if e == "done"), {})
        record("error 事件带可读提示与 retry_after",
               err.get("code") == "queue_timeout" and bool(err.get("message"))
               and err.get("retry_after") == 5, f"err={err}")
        record("done 带可读正文（前端可像普通回答展示）",
               bool(done.get("response")) and done.get("path") == "queue_timeout",
               f"done={ {k: done.get(k) for k in ('path', 'response')} }")
        record("排队超时不影响闸门容量", gate._value == base,
               f"after={gate._value} base={base}")

        # ── 2. 生成异常：让 _chat_core 抛错 ─────────────────────────────
        async def boom(*a, **kw):
            raise RuntimeError("模拟上游 502")
        m._chat_core = boom
        try:
            resp = await m.chat_stream(m.ChatRequest(message=MSG, user_id="q"), "")
            events = await drain(resp.body_iterator)
        finally:
            m._chat_core = orig_core
        names = [e for e, _ in events]
        record("生成异常发 error(internal_error) 事件", "error" in names, f"events={names}")
        record("生成异常最终仍发 done", names[-1:] == ["done"], f"events={names}")
        err = next((_json.loads(d) for e, d in events if e == "error"), {})
        record("异常提示不暴露内部细节（只给可执行建议）",
               err.get("code") == "internal_error" and "502" not in (err.get("message") or ""),
               f"err={err}")
        record("生成异常后闸门容量被释放", gate._value == base,
               f"after={gate._value} base={base}")

        # ── 3. 正常路径不受影响 ─────────────────────────────────────────
        class _Fake:
            def model_dump(self):
                return {"response": "正常回答", "citations": [], "intent": "work_permit"}

        async def ok_core(*a, **kw):
            return _Fake()
        m._chat_core = ok_core
        try:
            resp = await m.chat_stream(m.ChatRequest(message=MSG, user_id="q"), "")
            events = await drain(resp.body_iterator)
        finally:
            m._chat_core = orig_core
        names = [e for e, _ in events]
        done = next((_json.loads(d) for e, d in events if e == "done"), {})
        record("正常路径：delta + done，且无 error 事件",
               "delta" in names and "done" in names and "error" not in names,
               f"events={names}")
        record("正常路径 done 带正文与 path=pipeline",
               done.get("response") == "正常回答" and done.get("path") == "pipeline",
               f"done={ {k: done.get(k) for k in ('path', 'response')} }")
        record("正常路径闸门容量平衡", gate._value == base, f"after={gate._value}")


def main():
    asyncio.run(main_async())
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
