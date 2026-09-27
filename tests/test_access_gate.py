# 公网访问门（ACCESS_TOKEN 中间件）测试：python tests/test_access_gate.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    os.environ["ACCESS_TOKEN"] = "gate-secret-42"
    os.environ["API_HOST"] = "127.0.0.1"
    os.environ["AUDIT_DB_PATH"] = "data/_gate_audit.db"
    try:
        Path("data/_gate_audit.db").unlink()  # 句柄可能未即时释放
    except OSError:
        pass

    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        r = client.get("/health")
        record("健康检查保持公开", r.status_code == 200)
        r = client.get("/config")
        record("/config 保持公开", r.status_code == 200)
        r = client.get("/")
        record("前端静态首页公开", r.status_code == 200)
        r = client.post("/chat", json={"message": "你好", "user_id": "g", "conv_id": "g"})
        record("无令牌 /chat → 401", r.status_code == 401, f"code={r.status_code}")
        r = client.get("/conversations?user_id=g")
        record("无令牌 /conversations → 401", r.status_code == 401, f"code={r.status_code}")
        r = client.post("/chat", json={"message": "你好", "user_id": "g", "conv_id": "g2"},
                        headers={"X-Access-Token": "wrong"})
        record("错误令牌 → 401", r.status_code == 401, f"code={r.status_code}")
        r = client.post("/chat", json={"message": "你好", "user_id": "g", "conv_id": "g3"},
                        headers={"X-Access-Token": "gate-secret-42"})
        record("正确令牌 → 通过门", r.status_code == 200, f"code={r.status_code}")

        # 关闭开关：中间件每请求读环境，删除令牌即门不启用（同一会话内验证）
        del os.environ["ACCESS_TOKEN"]
        r = client.post("/chat", json={"message": "你好", "user_id": "g", "conv_id": "g4"})
        record("未设 ACCESS_TOKEN → 门不启用", r.status_code == 200, f"code={r.status_code}")

    try:
        Path("data/_gate_audit.db").unlink()  # 句柄可能未即时释放
    except OSError:
        pass
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
