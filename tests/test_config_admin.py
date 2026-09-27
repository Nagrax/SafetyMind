# .env 可视化配置端点测试：python tests/test_config_admin.py
# 覆盖：鉴权、白名单拒绝、换行注入拒绝、枚举校验、掩码读取、原子写保留注释行、
#       ESCALATION_PHONE 即时生效（公共 /config 端到端反映）、ENV_FILE_PATH 隔离（不碰真实 .env）。
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    tmp_env = "data/_config_test.env"
    Path(tmp_env).write_text(
        "# 测试配置（注释行必须被保留）\nANTHROPIC_API_KEY=sk-original-key-123456\nESCALATION_PHONE=\n",
        encoding="utf-8")
    os.environ["ENV_FILE_PATH"] = tmp_env
    # 0) 首次一键生成管理密码（仅未配置时可用）
    from fastapi.testclient import TestClient
    from api.main import app
    os.environ.pop("ADMIN_TOKEN", None)   # load_dotenv 在 import 时灌入了真实 .env，必须二次清除
    os.environ["API_HOST"] = "127.0.0.1"  # bootstrap 仅限回环形态
    with TestClient(app) as client:
        r = client.post("/config/admin/bootstrap")
        ok = r.status_code == 200 and r.json().get("admin_token", "").startswith("sm-")
        record("首次一键生成管理密码", ok, f"code={r.status_code} body={r.text[:120]}")
        gen_tok = r.json().get("admin_token", "")
        r = client.post("/config/admin/bootstrap")
        record("二次 bootstrap → 409", r.status_code == 409, f"code={r.status_code}")
        r = client.get("/config/admin", headers={"X-Admin-Token": gen_tok})
        record("生成密码立即可用", r.status_code == 200, f"code={r.status_code}")

    os.environ["ADMIN_TOKEN"] = "test-admin-123"
    os.environ["API_HOST"] = "0.0.0.0"  # 部署形态：管理端点强制鉴权

    # 两次 TestClient 会话会重复注册 prometheus 指标，先清空注册表
    from prometheus_client import REGISTRY
    for _c in list(REGISTRY._names_to_collectors.values()):
        try:
            REGISTRY.unregister(_c)
        except Exception:
            pass
    os.environ["ESCALATION_PHONE"] = ""

    from fastapi.testclient import TestClient
    from api.main import app
    H = {"X-Admin-Token": "test-admin-123"}

    with TestClient(app) as client:
        r = client.get("/config/admin")
        record("无令牌访问 → 403/401", r.status_code in (401, 403), f"code={r.status_code}")
        r = client.get("/config/admin", headers={**H, "X-Admin-Token": "wrong"})
        record("错误令牌 → 401", r.status_code == 401, f"code={r.status_code}")

        r = client.get("/config/admin", headers=H)
        cfg = {c["key"]: c for c in r.json()["config"]}
        ok = (r.status_code == 200
              and cfg["ANTHROPIC_API_KEY"]["secret"] is True
              and "****" in cfg["ANTHROPIC_API_KEY"]["value"]
              and "sk-original-key-123456" not in r.text)
        record("密钥掩码（完整值不出端点）", ok, f"body={r.text[:200]}")
        record("白名单字段完整", len(cfg) >= 8 and cfg["SAFETYMIND_BGE"]["restart"] is True)

        # 即时生效键：值班电话
        r = client.post("/config/admin", json={"values": {"ESCALATION_PHONE": "13800138000"}}, headers=H)
        record("写入值班电话", r.status_code == 200 and r.json()["restart_required"] == [],
               f"body={r.text[:200]}")
        r = client.get("/config")
        record("公共 /config 端到端反映", r.json().get("escalation_phone") == "13800138000",
               f"body={r.text[:120]}")

        # 安全边界
        r = client.post("/config/admin", json={"values": {"EVIL_KEY": "x"}}, headers=H)
        record("白名单外键 → 400", r.status_code == 400, f"code={r.status_code}")
        r = client.post("/config/admin", json={"values": {"ESCALATION_PHONE": "a\nEVIL_KEY=x"}}, headers=H)
        record("换行注入 → 400", r.status_code == 400, f"code={r.status_code}")
        r = client.post("/config/admin", json={"values": {"SAFETYMIND_BGE": "2"}}, headers=H)
        record("枚举外取值 → 400", r.status_code == 400, f"code={r.status_code}")

        # 注释行保留 + 密钥更新后仍掩码
        r = client.post("/config/admin", json={"values": {"ANTHROPIC_API_KEY": "sk-new-key-987654"}}, headers=H)
        record("更新 API Key 受理", r.status_code == 200 and "ANTHROPIC_API_KEY" in r.json()["restart_required"])
        text = Path(tmp_env).read_text(encoding="utf-8")
        record("注释行被保留", "# 测试配置（注释行必须被保留）" in text)
        r = client.get("/config/admin", headers=H)
        record("新密钥仍掩码显示", "sk-new-key-987654" not in r.text and "****" in r.text)

        # 本地单人形态：无令牌自动放行
        os.environ["API_HOST"] = "127.0.0.1"
        os.environ.pop("ADMIN_TOKEN", None)
        r = client.get("/config/admin")
        record("本地形态免密自动放行", r.status_code == 200, f"code={r.status_code}")

        # 本地形态即使设了管理密码也免密（锁死"bootstrap 409 + 输入 401"死锁的回归测试）
        os.environ["ADMIN_TOKEN"] = "sm-deadlock-test"
        r = client.get("/config/admin")
        ok = r.status_code == 200 and r.json().get("auth_mode") == "local"
        record("本地形态+已设密码仍免密", ok, f"code={r.status_code} body={r.text[:120]}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
