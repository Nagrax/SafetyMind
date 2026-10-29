# -*- coding: utf-8 -*-
# 前端"失败可重试"接线的契约测试（2026-10-29 闭环第 44 轮）。
#
# ## 背景
#
# 后端 /chat/stream 已经用 event:error + event:done 收尾（队列超时 / 生成失败），
# 但前端此前把错误文案当普通回答显示，用户不知道"等多久、点哪里"。
# 本轮补：流式错误透传 → 失败气泡 → 重试按钮（复用原问题重发）。
#
# ## 真实浏览器验证时抓到的一个真实回归（写进测试防再犯）
#
# 为了支持"用指定内容重发"，sendMessage 增加了第一个参数；模板里若仍写
# `@click="sendMessage"`，Vue 会把 MouseEvent 当内容传进去，用户消息变成
# "[object PointerEvent]"（截图 scratch/sm-loop/shots/sm_retry_ui.png 的历史版本）。
#
# ## 本测试锁定
#
# 1. 模板显式调用 `sendMessage()`（不能裸传函数引用）
# 2. sendMessage 只接受字符串参数（非字符串回落到输入框）
# 3. streamChat 解析 error 事件并把 error/retryAfter 透传给 UI
# 4. 失败气泡渲染重试按钮，且重试复用上一条用户消息
# 5. dist 产物里确实包含这些实现（防"改了源码没构建"）
#
# 用法: python tests/test_frontend_retry.py
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    app = (REPO / "frontend" / "src" / "App.vue").read_text(encoding="utf-8")
    be = (REPO / "frontend" / "src" / "lib" / "backends.js").read_text(encoding="utf-8")

    record("发送按钮显式调用 sendMessage()（避免把 MouseEvent 当消息）",
           '@click="sendMessage()"' in app, "模板里可能仍是 @click=\"sendMessage\"")
    record("sendMessage 只接受字符串参数",
           "typeof explicitContent === 'string'" in app)
    record("非字符串时回落到输入框内容",
           "? explicitContent : draft.value" in app)

    record("streamChat 解析 error 事件", "evt.event === 'error'" in be)
    record("streamChat 把 error/retryAfter 透传给 UI",
           "normalized.error = err" in be and "normalized.retryAfter" in be)
    record("流式不可用仍回退 /chat（不空回）",
           "return requestChat(type, settings, message)" in be)

    record("失败气泡渲染重试按钮", 'class="m-msg-actions retry-row"' in app
           and "retryMessage(item)" in app)
    record("重试复用上一条用户消息", "function retryMessage(item)" in app
           and "skipUserBubble: true" in app)
    record("失败正文与提示不重复（界面减法）",
           "排队超时，本次没有生成回答。" in app
           and "建议 ${wait} 秒后重试" in app)

    # 构建产物：防止"改源码没构建"
    dist = REPO / "frontend" / "dist" / "assets"
    js = sorted(dist.glob("index-*.js"))
    css = sorted(dist.glob("index-*.css"))
    js_all = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in js)
    record("dist 已包含重试按钮文案", "排队超时，本次没有生成回答" in js_all,
           f"dist={[p.name for p in js]}")
    record("dist 已包含 retry-row 样式",
           any("retry-row" in p.read_text(encoding="utf-8", errors="ignore") for p in css),
           f"dist={[p.name for p in css]}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
