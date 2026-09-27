"""会话历史持久化存储（跨天可恢复）。

背景：工作记忆（Redis/快照）TTL 仅 24h，且前端刷新即丢——"隔几天回历史会话"
此前不可达。本模块把每个会话的元数据与消息以 JSON 文件落盘（data/conversations/），
供 /conversations 端点组做列表/恢复/删除。

设计：
  - 一个会话一个文件：data/conversations/{user_id}/{conv_id}.json
  - 原子写（tmp + os.replace），进程崩溃不产生半截文件
  - 单会话消息上限 200 条（滚动截断，防磁盘膨胀）
  - user_id / conv_id 做文件名清洗，防路径穿越
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

_SAFE = re.compile(r"[^A-Za-z0-9_-]")
MAX_MESSAGES = 200


def _safe(key: str) -> str:
    return _SAFE.sub("_", str(key or "anonymous"))[:64] or "anonymous"


class ConversationStore:
    """JSON 文件型会话历史存储。目录约定：{root}/{user_id}/{conv_id}.json"""

    def __init__(self, root: Optional[str] = None):
        self._root = root or os.getenv(
            "SAFETYMIND_CONVERSATIONS_DIR", os.path.join("data", "conversations"))
        os.makedirs(self._root, exist_ok=True)

    def _path(self, user_id: str, conv_id: str) -> str:
        d = os.path.join(self._root, _safe(user_id))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{_safe(conv_id)}.json")

    def _load(self, user_id: str, conv_id: str) -> Dict[str, Any]:
        p = self._path(user_id, conv_id)
        if not os.path.exists(p):
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}  # 单文件损坏不影响其它会话

    def _save(self, user_id: str, conv_id: str, data: Dict[str, Any]) -> None:
        p = self._path(user_id, conv_id)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)

    def append_message(self, user_id: str, conv_id: str, role: str,
                       content: str, escalated: bool = False,
                       request_id: str = "") -> None:
        """追加一条消息并落盘；首条用户消息自动成为会话标题。"""
        data = self._load(user_id, conv_id)
        if not data:
            data = {
                "conv_id": conv_id,
                "title": (content[:30] if role == "user" else "新会话"),
                "created_at": time.time(),
                "messages": [],
            }
        msgs: List[Dict[str, Any]] = data.get("messages", [])
        msgs.append({"role": role, "content": content,
                     "escalated": escalated, "request_id": request_id,
                     "ts": time.time()})
        data["messages"] = msgs[-MAX_MESSAGES:]
        data["updated_at"] = time.time()
        self._save(user_id, conv_id, data)

    def list_conversations(self, user_id: str) -> List[Dict[str, Any]]:
        """该用户的会话列表（按最近更新倒序）：[{conv_id,title,updated_at,message_count}]"""
        d = os.path.join(self._root, _safe(user_id))
        if not os.path.isdir(d):
            return []
        items: List[Dict[str, Any]] = []
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            data = self._load(user_id, name[:-5])
            if not data:
                continue
            items.append({
                "conv_id": data.get("conv_id", name[:-5]),
                "title": data.get("title", "未命名会话"),
                "updated_at": data.get("updated_at", 0),
                "message_count": len(data.get("messages", [])),
            })
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return items

    def get_messages(self, user_id: str, conv_id: str) -> List[Dict[str, Any]]:
        data = self._load(user_id, conv_id)
        return data.get("messages", [])

    def delete_conversation(self, user_id: str, conv_id: str) -> bool:
        p = self._path(user_id, conv_id)
        if os.path.exists(p):
            os.remove(p)
            return True
        return False
