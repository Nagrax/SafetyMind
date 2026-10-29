# -*- coding: utf-8 -*-
# /chat/stream 客户端中途断开时的闸门容量泄漏回归测试（2026-10-29 闭环第 42 轮）。
#
# ## 缺陷（读代码发现，结构性问题）
#
# 修前顺序是：
#     await _acquire_with_timeout(lane, ...)      # 拿到容量
#     yield sse("stage", {"stage": "processing"}) # ← 客户端在这一帧断开
#     try: ... finally: lane.release()
# 客户端断开时生成器在 yield 处收到 GeneratorExit，**根本不会进入后面的 try**，
# 容量永久泄漏。_CHAT_GATE 容量 4 —— 泄漏 4 次，整条对话链路就再也不接客了，
# 而 HTTP 层看起来只是"有人取消了请求"。
#
# ## 修法
#
# 把 acquire 之后的所有 yield 都纳入 try/finally，并用 _acquired 标记只释放真正
# 拿到的容量（acquire 超时抛 429 时不误释放）。
#
# ## 本测试锁定
#
# 1. 在 "processing" 帧断开 → 容量恢复（不泄漏）
# 2. 正常读完 → 容量恢复（不重复释放/不少释放）
# 3. 断开后仍可再次拿满容量（用 4 次"断开"验证不会累积泄漏）
#
# 用法: python tests/test_stream_gate_leak.py
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
# 队列等待上限压到 3s：一旦未来又出现容量泄漏，测试会**快速失败**而不是挂在
# 默认 180s 排队上（本轮做反证时正好踩到：泄漏后第 4 次驱动直接卡死）。
os.environ.setdefault("CHAT_QUEUE_TIMEOUT_S", "3")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


async def _drive_stream_until_processing(m, msg):
    """取一个 /chat/stream 响应，推进到 processing 帧，返回 (agen, gate_before)。"""
    req = m.ChatRequest(message=msg, user_id="leak-test")
    resp = await m.chat_stream(req, "")
    agen = resp.body_iterator
    await agen.__anext__()          # meta
    await agen.__anext__()          # stage: queued
    await agen.__anext__()          # stage: processing（此刻容量已拿到）
    return agen


async def main_async():
    import api.main as m

    async with m.app.router.lifespan_context(m.app):
        cap = int(os.getenv("CHAT_MAX_CONCURRENCY", "4"))
        gate = m._CHAT_GATE
        # 用例要走普通对话闸门（不能命中化学品快车道），否则测的是 _FAST_GATE
        MSG = "动火作业票的有效期是怎么规定的？"

        base = gate._value
        agen = await _drive_stream_until_processing(m, MSG)
        held = gate._value
        record("processing 帧时容量确实被占用（前置条件）", held == base - 1,
               f"before={base} held={held}")
        await agen.aclose()             # 模拟客户端断开
        await asyncio.sleep(0)
        record("客户端在 processing 帧断开后容量归还（修复前会泄漏）",
               gate._value == base, f"after={gate._value} base={base}")

        # 连续 4 次"断开"不再泄漏（容量 4 时，旧实现第 4 次就永久卡死）
        leaked = 0
        for _ in range(cap):
            ag = await _drive_stream_until_processing(m, MSG)
            await ag.aclose()
            await asyncio.sleep(0)
            if gate._value != base:
                leaked += 1
        record(f"连续 {cap} 次中途断开都不泄漏容量", leaked == 0,
               f"泄漏次数={leaked} 当前={gate._value} base={base}")

        # 正常读完的路径也不能重复释放
        ag = await _drive_stream_until_processing(m, MSG)
        try:
            while True:
                chunk = await ag.__anext__()
                if "event: done" in chunk:
                    break
        except StopAsyncIteration:
            pass
        await ag.aclose()
        record("正常读完的路径容量也是平的（不重复释放）", gate._value == base,
               f"after={gate._value} base={base}")


def main():
    asyncio.run(main_async())
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
