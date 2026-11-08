# -*- coding: utf-8 -*-
# 台账导出主体边界回归（闭环发现 api/main.py::export-subject-scope，severity=medium）。
#
# 发现（台账 evidence：slice3-role-matrix-actual.json 矩阵实测 200 格）：
#   旧 POST /ledger/export 无主体/角色边界——无 Depends(require_admin)、无主体校验，
#   仅 access_gate 共享口令：匿名/普通上报者/撤权主体均 200。
# 修复口径（对齐 SM-42 同范式，api/main.py preview/export 用 require_admin）：
#   导出是管理端动作 → 部署/网络形态必须持 X-Admin-Token（require_admin）；
#   本地单人形态（SAFETYMIND_DEPLOYED 未设 + 回环）逐字节不变（免密自动放行，
#   S-05 既有口径，require_admin 原语义）。
#
# 三态回归（匿名/他人/管理员）+ 两态守卫：
#   外墙（无访问令牌）      → 401（access_gate 既有）
#   匿名（仅访问令牌）      → 401（require_admin 新边界，修复前 200 = 红灯）
#   普通上报者              → 401（同上）
#   撤权主体                → 401（同上）
#   管理员（X-Admin-Token） → 200 且产物真实落盘在夹具目录、xlsx 可读回
#   本地单人形态匿名        → 200（既有行为不变，防止修复误伤本地免密 UX）
#
# 铁律：产物目录钉到一次性临时目录（绝不写 D:/SafetyMindData 生产目录——封闭性守卫，
# 对齐 test_role_matrix_api 动作 3）；存储/会话/向量库全部临时目录；环境不可用退 2。
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TOKEN_A, TOKEN_ADM = "scope-tok-a", "scope-tok-adm"
ACCESS, ADMIN = "scope-access-1", "scope-admin-1"
USERS = [
    {"token": TOKEN_A, "subject_id": "u-A", "roles": ["reporter"], "scopes": ["plant-a"]},
    {"token": TOKEN_ADM, "subject_id": "u-ADM", "roles": ["admin"], "scopes": ["*"]},
]

RESULTS = []
TMP = Path(tempfile.mkdtemp(prefix="sm_export_scope_", dir="D:/claudecode/scratch/sm-loop"))


def record(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    # 环境必须先于 import 钉死（api.main 导入时 load_dotenv 不覆盖已显式设置的变量）
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_EMERGENCY_FASTPATH": "1",
        "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
        "SAFETYMIND_OUTBOX_ENABLED": "0",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "AUDIT_DB_PATH": str(TMP / "audit.db"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(TMP / "convs"),
        "CHROMA_HOST": "127.0.0.1",
        "CHROMA_PORT": "8973",
        "CHROMA_PERSIST_DIRECTORY": str(TMP / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(TMP / "kb_chroma"),
        "PROMETHEUS_PORT": "0",
        "AUDIT_ENABLED": "1",
        "SAFETYMIND_DEPLOYED": "1",
        "ACCESS_TOKEN": ACCESS,
        "ADMIN_TOKEN": ADMIN,
        "API_HOST": "0.0.0.0",
        "SAFETYMIND_USERS": json.dumps(USERS, ensure_ascii=False),
    })
    try:
        from fastapi.testclient import TestClient
    except Exception as ex:
        print(f"[REJECT] 环境不可用（fastapi TestClient 缺失）: {ex}")
        shutil.rmtree(str(TMP), ignore_errors=True)
        return 2
    try:
        import core.ledger_export as ledger_export
        import api.main as api_main
    except Exception as ex:
        print(f"[REJECT] 环境不可用（产品模块导入失败）: {type(ex).__name__}: {ex}")
        shutil.rmtree(str(TMP), ignore_errors=True)
        return 2

    client = TestClient(api_main.app)   # 不进 lifespan：本端点零存储依赖
    ledger_out = TMP / "ledger_out"
    ledger_out.mkdir(parents=True, exist_ok=True)
    ledger_export.DEFAULT_OUT = ledger_out   # 封闭性守卫：产物钉到夹具目录

    def files_now():
        return sorted(p.name for p in ledger_out.iterdir())

    def reset_out():
        """清空产物目录后再断言"恰一次落盘"：xlsx 文件名秒级时间戳存在同秒覆盖
        （RF-2 立案，另行修复），前置态不清空会让 +1 计数不确定——这是测试骨架
        卫生，不是放松任何断言（三态 401 / 封闭性守卫逐字保留）。"""
        for p in ledger_out.iterdir():
            p.unlink()

    def hdr(user_token="", admin=False):
        h = {"X-Access-Token": ACCESS}
        if user_token:
            h["X-User-Token"] = user_token
        if admin:
            h["X-Admin-Token"] = ADMIN
        return h

    BODY = {"ledger": "hazard", "format": "xlsx"}
    try:
        # ══ 部署/网络多用户形态（SAFETYMIND_DEPLOYED=1 + ADMIN_TOKEN）══════════
        # 外墙：无访问令牌 → 401（access_gate 既有行为，防回归）
        n0 = len(files_now())
        r = client.post("/ledger/export", json=BODY)
        record("外墙|无访问令牌 → 401 且零文件落盘",
               r.status_code == 401 and len(files_now()) == n0,
               f"code={r.status_code} files={files_now()}")

        # 三态之一：匿名（仅访问令牌，无管理令牌）→ 401（修复前 200 = 本回归的红灯格）
        n0 = len(files_now())
        r = client.post("/ledger/export", json=BODY, headers=hdr())
        record("匿名|仅访问令牌 → 401 且零文件落盘（export-subject-scope 新边界）",
               r.status_code == 401 and len(files_now()) == n0,
               f"code={r.status_code} files={files_now()} body={r.text[:120]}")

        # 三态之二：普通上报者（访问+用户令牌，reporter 角色，无管理令牌）→ 401
        n0 = len(files_now())
        r = client.post("/ledger/export", json=BODY, headers=hdr(TOKEN_A))
        record("普通上报者|reporter 主体 → 401 且零文件落盘",
               r.status_code == 401 and len(files_now()) == n0,
               f"code={r.status_code} files={files_now()} body={r.text[:120]}")

        # 撤权主体：token 从主体目录移除（产品逐请求解析 env，即时生效）→ 401
        os.environ["SAFETYMIND_USERS"] = json.dumps(
            [u for u in USERS if u["token"] != TOKEN_A], ensure_ascii=False)
        n0 = len(files_now())
        r = client.post("/ledger/export", json=BODY, headers=hdr(TOKEN_A))
        record("撤权主体|已撤销 token → 401 且零文件落盘",
               r.status_code == 401 and len(files_now()) == n0,
               f"code={r.status_code} files={files_now()}")
        os.environ["SAFETYMIND_USERS"] = json.dumps(USERS, ensure_ascii=False)

        # 三态之三：管理员（X-Admin-Token）→ 200 且产物真实落盘在夹具目录
        reset_out()
        n0 = len(files_now())
        r = client.post("/ledger/export", json=BODY, headers=hdr(admin=True))
        body = r.json() if r.status_code == 200 else {}
        path = str(body.get("path", ""))
        record("管理员|X-Admin-Token → 200 且产物落夹具目录（封闭性守卫）",
               r.status_code == 200 and body.get("ok") is True
               and str(ledger_out) in path and len(files_now()) == n0 + 1,
               f"code={r.status_code} path={path[:120]} files={files_now()}")

        xlsx_ok, xlsx_detail = False, ""
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(ledger_out / Path(path).name))
            xlsx_ok = len(wb.sheetnames) >= 1 and Path(path).stat().st_size > 1000
            xlsx_detail = f"sheets={wb.sheetnames}"
        except Exception as ex:
            xlsx_detail = f"{type(ex).__name__}: {ex}"
        record("管理员|产物为真实可读回的 xlsx（非空壳/非错误页）", xlsx_ok, xlsx_detail)

        # ══ 本地单人形态（S-05 口径：行为逐字节不变）══════════════════════════
        os.environ.pop("SAFETYMIND_DEPLOYED", None)
        os.environ.pop("ACCESS_TOKEN", None)
        os.environ.pop("ADMIN_TOKEN", None)
        os.environ["API_HOST"] = "127.0.0.1"
        reset_out()
        n0 = len(files_now())
        r = client.post("/ledger/export", json=BODY)
        body = r.json() if r.status_code == 200 else {}
        path = str(body.get("path", ""))
        record("本地单人形态|匿名免密 → 200 且产物落夹具目录（既有行为不变）",
               r.status_code == 200 and body.get("ok") is True
               and str(ledger_out) in path and len(files_now()) == n0 + 1,
               f"code={r.status_code} path={path[:120]} body={r.text[:120]}")
    finally:
        api_main._audit = None
        shutil.rmtree(str(TMP), ignore_errors=True)

    failed = RESULTS.count(False)
    total = len(RESULTS)
    print(f"\n{total - failed}/{total} PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
