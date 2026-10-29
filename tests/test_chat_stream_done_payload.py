# -*- coding: utf-8 -*-
# /chat/stream 的 done 事件契约测试（2026-10-29 闭环第 36 轮）。
#
# 背景：前端此前只调 /chat，/chat/stream 形同虚设——"等待期无反馈"只修了后端一半。
# 前端接入流式后，done 事件必须携带**完整响应体**，否则流式版会丢升级卡/依据/建单卡
# （那会变成"体验好了但功能少了"的隐性回归）。
#
# 本测试锁定：
#   1. done 事件在两个分支（紧急快路径 / 完整链路）都展开完整 body（`**body`）
#   2. 紧急快路径的 done 实际带 response / escalated / citations 字段（运行时验证，零 LLM）
#   3. 事件顺序契约不变：meta 先于 stage/delta/done
#   4. 前端确实调用 /chat/stream（构建产物里能查到），且保留 /chat 作为回退
#
# 用法: python tests/test_chat_stream_done_payload.py
import ast
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    body = src[src.index("async def chat_stream"):]
    body = body[:body.index("\n@app.")] if "\n@app." in body else body

    # ── 1. done 分支：两个正常分支带完整 body；两个异常分支带可读 response ──
    # 2026-10-29 契约扩充：排队超时 / 生成异常也必须发 done（否则客户端看到半截流），
    # 所以这里从 2 个分支变成 4 个；断言同步改为"每个分支都必须有正文来源"。
    done_count = body.count('sse("done"')
    record("done 事件覆盖 4 个分支（紧急/正常/排队超时/异常）", done_count == 4,
           f"count={done_count}")
    record("正常分支 done 展开完整响应体（**body）", body.count("**body") >= 2,
           f"count={body.count('**body')}")
    record("异常分支 done 也带 response（客户端不会看到半截流）",
           body.count('"response": _msg') >= 2, f"count={body.count(chr(34) + 'response' + chr(34) + ': _msg')}")
    record("紧急分支保留 path=emergency 标记", '"path": "emergency"' in body)
    record("完整链路保留 path=pipeline 标记", '"path": "pipeline"' in body)
    record("异常分支带可识别 path（queue_timeout / error）",
           '"path": "queue_timeout"' in body and '"path": "error"' in body)

    # ── 2. 事件顺序：meta 在 stage/delta 之前 ────────────────────────────
    idx_meta = body.index('sse("meta"')
    idx_stage = body.index('sse("stage"')
    idx_delta = body.index('sse("delta"')
    record("meta 先于 stage 与 delta", idx_meta < idx_stage and idx_meta < idx_delta,
           f"meta={idx_meta} stage={idx_stage} delta={idx_delta}")

    # ── 3. 运行时：紧急快路径的 done 带完整字段（零 LLM）─────────────────
    try:
        from fastapi.testclient import TestClient
        import api.main as m
        with TestClient(m.app) as c:
            c.get("/health")
            r = c.post("/chat/stream",
                       json={"message": "车间氨气泄漏，已经有人中毒晕倒了，现在该怎么办？",
                             "user_id": "done-contract"})
            text = r.text
            record("流式响应 content-type 为 text/event-stream",
                   "text/event-stream" in r.headers.get("content-type", ""),
                   r.headers.get("content-type", ""))
            # 解析最后一帧 done
            done = None
            for frame in text.split("\n\n"):
                if frame.startswith("event: done"):
                    data = frame.split("data: ", 1)[1]
                    import json as _json
                    done = _json.loads(data)
            record("done 帧存在且可解析", isinstance(done, dict), f"done={str(done)[:80]}")
            if isinstance(done, dict):
                record("done 带 response 正文且非空", bool(done.get("response")),
                       f"keys={sorted(done)[:8]}")
                record("done 带 escalated 字段（升级信息不丢）", "escalated" in done)
                record("done 带 citations 字段（依据不丢）", "citations" in done)
                record("done 带 path=emergency", done.get("path") == "emergency",
                       f"path={done.get('path')}")
    except Exception as ex:  # pragma: no cover - 依赖本机运行环境
        record("运行时 done 契约（紧急快路径）", False, repr(ex))

    # ── 4. 前端接入：调用 /chat/stream 且保留 /chat 回退 ─────────────────
    fe = (REPO / "frontend" / "src" / "lib" / "backends.js").read_text(encoding="utf-8")
    record("前端存在流式调用函数", "export async function streamChat" in fe)
    record("前端请求 /chat/stream", "/chat/stream" in fe)
    record("流式不可用时回退 /chat（不空回）",
           "return requestChat(type, settings, message)" in fe)
    record("SSE 帧解析处理 event/data 双行",
           "event:" in fe and "data:" in fe)
    app = (REPO / "frontend" / "src" / "App.vue").read_text(encoding="utf-8")
    record("聊天发送走 streamChat", "streamChat(settings.backend" in app)
    record("等待期显示阶段文案", "stageText" in app and "m-stage-note" in app)
    # 构建产物里应有流式实现（避免"改了源码没构建"的假修复）
    dist = REPO / "frontend" / "dist" / "assets"
    js = sorted(dist.glob("index-*.js"))
    record("dist 产物已包含流式实现（已重新构建）",
           bool(js) and any("/chat/stream" in p.read_text(encoding="utf-8", errors="ignore")
                            for p in js),
           f"dist={[p.name for p in js]}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
