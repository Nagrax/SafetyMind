# -*- coding: utf-8 -*-
"""评测脚本的凭据读取（**唯一入口**，禁止在脚本里写死 key）。

## 为什么单独一个模块（安全）

2026-10-29 提交前扫描发现：15 个评测脚本把中继 key 直接写在 `os.getenv(..., "<key>")`
的默认值里——一旦仓库公开，key 就泄露了。现在统一改为：
依次读 `JUDGE_API_KEY` → `ANTHROPIC_API_KEY` → 仓库 `.env` 的 `ANTHROPIC_API_KEY`，
全部缺失时返回空串，并在调用处显式报错（**绝不回退到写死的值**）。

`.env` 已在 `.gitignore` 里；`.env.bak*` 也已忽略。脚本自身不再含任何密钥字面量。
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _from_dotenv(name: str) -> str:
    try:
        for line in (REPO / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def judge_key() -> str:
    """裁判/评测用的模型 key：环境变量优先，其次仓库 .env。"""
    for env in ("JUDGE_API_KEY", "DSH_GATEWAY_KEY", "ANTHROPIC_API_KEY"):
        v = (os.getenv(env) or "").strip()
        if v:
            return v
    return _from_dotenv("ANTHROPIC_API_KEY")


def gateway(base_default: str = "http://127.0.0.1:8326/v1") -> str:
    """评测网关地址：环境变量优先，其次仓库 .env 的 ANTHROPIC_BASE_URL。"""
    for env in ("JUDGE_GATEWAY", "DSH_GATEWAY_URL"):
        v = (os.getenv(env) or "").strip()
        if v:
            return v
    return _from_dotenv("ANTHROPIC_BASE_URL") or base_default


def require_key() -> str:
    key = judge_key()
    if not key:
        raise SystemExit("缺少评测凭据：请设置 JUDGE_API_KEY 或在仓库 .env 填 ANTHROPIC_API_KEY")
    return key
