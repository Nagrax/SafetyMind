# 前端-后端接线测试：桌面/Web 模式下前端按钮背后的关键端点是否真实可用
# 用法: python tests/test_frontend_endpoints.py（启动完整 app，覆盖 lifespan 初始化）
# 覆盖：/health（连接状态）、GET+POST /skills（已加载能力/重新加载按钮）、
#       /knowledge/stats（知识库页统计）、/api/python/* 前缀别名（桌面模式前端实际路径）、
#       静态首页（frontend/dist 是否随仓库可用）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:  # with 语句触发 lifespan（真实初始化记忆/知识库/Skills）
        r = client.get("/health")
        record("GET /health 连接状态", r.status_code == 200, f"code={r.status_code}")

        r = client.get("/skills")
        ok = r.status_code == 200 and r.json().get("count", 0) >= 1
        record("GET /skills 已加载能力", ok, f"code={r.status_code} body={r.text[:120]}")

        r = client.post("/skills/reload")
        ok = r.status_code == 200 and r.json().get("count", 0) >= 1
        record("POST /skills/reload 重新加载按钮", ok, f"code={r.status_code} body={r.text[:120]}")

        r = client.get("/knowledge/stats")
        ok = r.status_code == 200 and "total_chunks" in r.json()
        record("GET /knowledge/stats 知识库统计", ok, f"code={r.status_code} body={r.text[:120]}")

        # 桌面模式前端走 /api/python 前缀（与 Nginx 反代路径一致），别名必须同权可用
        r = client.get("/api/python/skills")
        record("GET /api/python/skills 别名", r.status_code == 200, f"code={r.status_code}")
        r = client.post("/api/python/skills/reload")
        record("POST /api/python/skills/reload 别名", r.status_code == 200, f"code={r.status_code}")

        # 会话历史：写入（借 /chat 太慢，直接调存储层）→ 列表 → 恢复 → 删除
        from memory.conversation_store import ConversationStore
        store = ConversationStore("data/conversations")
        store.append_message("endpoint_test", "c_hist_1", "user", "动火作业票怎么办理")
        store.append_message("endpoint_test", "c_hist_1", "assistant", "办理流程：...", escalated=False, request_id="r9")
        r = client.get("/conversations?user_id=endpoint_test")
        ok = r.status_code == 200 and any(c["conv_id"] == "c_hist_1" for c in r.json().get("conversations", []))
        record("GET /conversations 历史列表", ok, f"code={r.status_code} body={r.text[:140]}")
        r = client.get("/conversations/c_hist_1/messages?user_id=endpoint_test")
        ok = r.status_code == 200 and len(r.json().get("messages", [])) == 2
        record("GET /conversations/{id}/messages 隔天恢复", ok, f"code={r.status_code}")
        r = client.delete("/conversations/c_hist_1?user_id=endpoint_test")
        ok = r.status_code == 200 and r.json().get("deleted") is True
        record("DELETE /conversations/{id}", ok, f"code={r.status_code}")

        r = client.get("/config")
        record("GET /config 转人工电话配置", r.status_code == 200 and "escalation_phone" in r.json(),
               f"code={r.status_code} body={r.text[:120]}")

        # 评测页加载时自动拉取最近一次评测报告：
        # 200 且含 pass_rate 字段（已有报告），或明确的空态结构（available=false，冷启动）
        r = client.get("/eval/last")
        body = r.json() if r.status_code == 200 else {}
        ok = r.status_code == 200 and ("pass_rate" in body or body.get("available") is False)
        record("GET /eval/last 最近评测报告/空态", ok, f"code={r.status_code} body={r.text[:120]}")
        r = client.get("/api/python/eval/last")
        record("GET /api/python/eval/last 别名", r.status_code == 200, f"code={r.status_code}")

        r = client.get("/")
        record("GET / 静态首页(frontend/dist)", r.status_code == 200 and "<div id=" in r.text,
               f"code={r.status_code} len={len(r.text)}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
