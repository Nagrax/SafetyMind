"""服务端主体解析与数据隔离判定（S-05：服务端身份与数据隔离）。

纯逻辑模块：无 IO（除只读环境变量）、无 LLM、绝不抛出异常。

分工：
  - `Subject`：一次请求的**服务端**主体（id / 角色 / 可检索资料范围 / 来源）。
  - `auth_required()`：是否处于"网络/多用户形态"（复用 main.py 既有 `_admin_auth_required`
    的口径，另加 ACCESS_TOKEN：设了口令即视为多用户访问，不再有本地可信边界）。
  - `load_users()`：解析 SAFETYMIND_USERS 主体目录（token → 主体配置）。
  - `resolve_subject()`：由请求携带的 X-User-Token 解析主体；未命中即匿名（无角色、无范围）。
  - `isolation_enforced()`：**强制主体隔离**的开关。

桌面单人模式（本地形态）保持独立的可信边界：不设 ACCESS_TOKEN、未部署、API_HOST 回环时
直接给出本地可信主体（全角色 + 全范围），既有端点行为逐字节不变。

隔离判定为何还要看"是否配置了主体目录"：未配置 SAFETYMIND_USERS 时服务端无从解析主体，
只能沿用既有 ACCESS_TOKEN / ADMIN_TOKEN 口令门语义（既有部署回归不受影响）；一旦配置了
主体目录，多用户形态下即按主体强制隔离（会话/工单归属、角色边界、资料范围）。
"""
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

ROLE_REPORTER = "reporter"
ROLE_HANDLER = "handler"
ROLE_VERIFIER = "verifier"
ROLE_ADMIN = "admin"
ALL_ROLES = frozenset({ROLE_REPORTER, ROLE_HANDLER, ROLE_VERIFIER, ROLE_ADMIN})

# 多用户形态下的默认角色（配置缺省时上报者仍可上报）
ANONYMOUS_ID = "anonymous"


@dataclass
class Subject:
    """请求主体：一次请求在服务端被认证/推导出的身份。"""
    subject_id: str
    roles: Set[str] = field(default_factory=set)
    scopes: List[str] = field(default_factory=list)
    source: str = "local"          # "local" | "token" | "anonymous"

    def has_role(self, *names: str) -> bool:
        return bool(self.roles.intersection(names))

    @property
    def unrestricted(self) -> bool:
        """资料范围是否不受限（["*"] = 可检索全部资料）。"""
        return "*" in self.scopes


def auth_required() -> bool:
    """是否多用户/网络形态（与 main.py 既有 `_admin_auth_required` 同口径，另加访问令牌）。

    - ACCESS_TOKEN 非空：已启用公网访问门 → 不再存在"本地可信请求"；
    - SAFETYMIND_DEPLOYED=1：服务器反代部署（即使 uvicorn 只绑回环，公网经反代可达）；
    - 否则按 API_HOST：非回环 = 网络暴露。
    """
    if (os.getenv("ACCESS_TOKEN", "") or "").strip():
        return True
    if (os.getenv("SAFETYMIND_DEPLOYED", "") or "").strip() == "1":
        return True
    host = (os.getenv("API_HOST", "0.0.0.0") or "").strip().lower()
    return host not in ("127.0.0.1", "localhost", "::1")


def load_users() -> Dict[str, dict]:
    """解析 SAFETYMIND_USERS（JSON 字符串），返回 {token: 主体配置}。

    接受两种形式：
      - 数组：[{"token": "...", "subject_id": "u-1", "roles": ["reporter"], "scopes": ["plant-a"]}]
      - 对象：{"tok1": {"subject_id": "u-1", "roles": [...], "scopes": [...]}}

    解析失败 → 空表 + 记日志，绝不抛出（启动与请求路径都不得被配置错误拖垮）。
    """
    raw = (os.getenv("SAFETYMIND_USERS", "") or "").strip()
    if not raw:
        return {}
    try:
        data: Any = json.loads(raw)
    except Exception as ex:
        logger.warning(f"SAFETYMIND_USERS 解析失败，主体目录按空处理: {ex}")
        return {}
    table: Dict[str, dict] = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("token"):
                table[str(item["token"])] = item
    elif isinstance(data, dict):
        for token, item in data.items():
            if isinstance(item, dict):
                table[str(token)] = item
    else:
        logger.warning("SAFETYMIND_USERS 不是数组或对象，主体目录按空处理")
    return table


def users_configured() -> bool:
    return bool(load_users())


def isolation_enforced() -> bool:
    """是否强制主体隔离：网络/多用户形态 **且** 已配置主体目录（详见模块文档）。"""
    return auth_required() and users_configured()


def _subject_from_entry(entry: dict) -> Subject:
    """主体目录条目 → Subject（roles 缺省 reporter，scopes 缺省空 = 仅公共资料）。"""
    sid = str(entry.get("subject_id") or "").strip() or ANONYMOUS_ID
    roles_raw = entry.get("roles")
    if isinstance(roles_raw, (list, tuple, set)):
        roles: Set[str] = {str(r).strip() for r in roles_raw if str(r).strip()}
    else:
        roles = set()
    if not roles:
        roles = {ROLE_REPORTER}
    scopes_raw = entry.get("scopes")
    if isinstance(scopes_raw, (list, tuple)):
        scopes: List[str] = [str(s) for s in scopes_raw]
    else:
        scopes = []
    return Subject(sid, roles, scopes, "token")


def anonymous_subject() -> Subject:
    return Subject(ANONYMOUS_ID, set(), [], "anonymous")


def resolve_subject(user_token: str, fallback_user_id: str = "", *,
                    required: Optional[bool] = None) -> Subject:
    """由请求携带的 X-User-Token（+ 本地形态下的客户端 user_id）解析服务端主体。

    * `required=None`（默认）按 `auth_required()` 判定形态；显式传入可覆盖（便于测试与复用）。
    * 本地形态：客户端 user_id 即主体 id（桌面单人可信边界），全角色 + 全范围。
    * 多用户形态：token 命中主体目录才给出角色与资料范围；未命中一律匿名——
      **绝不**把客户端传来的 user_id 当作授权依据（改字段不能越权）。
    """
    effective = auth_required() if required is None else bool(required)
    if not effective:
        return Subject(str(fallback_user_id or "local") or "local",
                       set(ALL_ROLES), ["*"], "local")
    entry = load_users().get(str(user_token or ""))
    if not isinstance(entry, dict):
        return anonymous_subject()
    return _subject_from_entry(entry)
