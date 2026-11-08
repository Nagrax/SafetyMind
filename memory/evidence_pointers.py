"""
SM-31：受权证据指针与上下文窗口策略（标准库实现，零重依赖）。

问题：长会话下员工层把记忆块全文注入每个专家（最近 20 轮 + 相关历史 + 画像 +
偏好卡），token 随会话长度线性膨胀；而其中多数内容对本次回答并非必需。

本模块提供两条正交机制（SM-31 任务卡 scope）：
  1. 证据指针注册表 EvidencePointerStore —— 把"不进 prompt 的上下文正文"留在
     服务端，只把短引用行交给模型；正文按需经 evidence_expand 工具展开
     （展开走 ToolExecutor 白名单+双预算，无旁路）。访问矩阵（任务卡验收 1）：
       - 同主体同会话才可读（subject_id + session_id 双边界，服务端 contextvar
         绑定，模型无法自报身份）；
       - 过期（TTL）拒绝；内容被篡改（sha256 重算不一致）拒绝——完整性 fail-closed；
       - 存储读取失败如实返回 store_error，绝不编造内容；
       - **自动授权写操作（auto_write）拒绝登记**：库里不存在即永不可引用。
  2. 窗口渲染 window_memory_prompt —— 保留当前请求（装配层既有契约）、最近关键轮
     （SAFETYMIND_CTX_WINDOW_RECENT，默认 4）、否决证据（veto/escalated 标记轮
     无视窗口全文保留）与关键 SOP/条款证据（Skill 冻结快照与 RAG 块不在裁剪范围）；
     其余注册为指针并以短行呈现。

开关：SAFETYMIND_CONTEXT_WINDOW=1 才启用（api/main.py 与 agents 层各自读取），
缺省 off = 现行为逐字节不变（tests/test_context_window_pointers.py t0 钉死）。

指针 id 确定性：ctx- + sha256(f"{subject}|{session}|{kind}|{内容 sha}")[:12]——
同会话同内容幂等（并行专家装配只登记一次），跨主体/跨会话永不碰撞。
"""
import contextvars
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

# 请求级主体/会话绑定（与 mcp/knowledge_base.SCOPES_CONTEXT 同款模式）：
# api 层在 /chat、/chat/stream 处理入口绑定，展开工具在读取时取用——
# 主体与会话是服务端事实，模型只能传 pointer_id，不能自报身份。
_SCOPE: contextvars.ContextVar[Optional[Dict[str, str]]] = contextvars.ContextVar(
    "safetymind_evidence_scope", default=None)

MAX_ENTRIES = 512          # 进程内指针条目上限（FIFO 淘汰最旧；TTL 仍独立生效）
DEFAULT_TTL_S = 86400      # 24h，对齐工作记忆 TTL（请求级短期证据）
_KINDS = ("conversation", "history", "sop_clause", "ticket", "knowledge")


def bind_request_scope(subject_id: str, session_id: str) -> contextvars.Token:
    """绑定本次请求的 {subject_id, session_id}，返回 token 供 finally 重置。"""
    return _SCOPE.set({"subject_id": str(subject_id or ""),
                       "session_id": str(session_id or "")})


def reset_scope(token: contextvars.Token) -> None:
    _SCOPE.reset(token)


def current_scope() -> Dict[str, str]:
    """当前请求 scope（未绑定时返回空字典，调用方回退到请求字段）。"""
    return dict(_SCOPE.get() or {})


class EvidencePointerStore:
    """进程内证据指针注册表（内容不出服务端，模型只拿短引用行）。

    uses_dir：per-expert 展开使用记录（JSONL）落盘目录；
    缺省 SAFETYMIND_EVIDENCE_DIR（默认 data/evidence_pointers）。落盘失败仅降级，
    绝不阻断对话（仓库降级铁律）。
    """

    def __init__(self, uses_dir: Optional[str] = None):
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._uses_dir = uses_dir or os.getenv(
            "SAFETYMIND_EVIDENCE_DIR", os.path.join("data", "evidence_pointers"))

    # ── 登记 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _pointer_id(subject_id: str, session_id: str, kind: str, content_sha: str) -> str:
        raw = f"{subject_id}|{session_id}|{kind}|{content_sha}"
        return "ctx-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def register(self, *, content: str, subject_id: str, session_id: str,
                 kind: str = "conversation", ttl_s: Optional[int] = None,
                 title: str = "", auto_write: bool = False) -> Optional[Dict[str, Any]]:
        """登记一条证据，返回指针描述；拒绝时返回 None（fail-closed）。

        拒绝条件（任务卡"禁止引用自动授权写操作"+ 防御性校验）：
          auto_write=True、kind="auto_write"、空正文、未知 kind。
        幂等：同主体/同会话/同 kind/同内容 → 返回既有指针（刷新过期时间）。
        """
        if auto_write or str(kind) == "auto_write":
            return None   # 自动授权写操作不可成为可引用证据
        content = str(content or "")
        if not content.strip():
            return None
        if kind not in _KINDS:
            return None
        subject_id = str(subject_id or "")
        session_id = str(session_id or "")
        content_sha = self._sha(content)
        pid = self._pointer_id(subject_id, session_id, kind, content_sha)
        now = time.time()
        ttl = DEFAULT_TTL_S if ttl_s is None else int(ttl_s)
        existing = self._entries.get(pid)
        if existing is not None:
            existing["expires_ts"] = now + ttl
            return self._desc(existing)
        if len(self._entries) >= MAX_ENTRIES:
            oldest = min(self._entries.values(), key=lambda e: e["created_ts"])
            self._entries.pop(oldest["pointer_id"], None)
        entry = {
            "pointer_id": pid, "kind": kind, "subject_id": subject_id,
            "session_id": session_id, "content_sha256": content_sha,
            "content": content, "chars": len(content), "title": str(title or ""),
            "created_ts": now, "expires_ts": now + ttl,
        }
        self._entries[pid] = entry
        return self._desc(entry)

    @staticmethod
    def _desc(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {"pointer_id": entry["pointer_id"], "kind": entry["kind"],
                "chars": entry["chars"], "title": entry["title"]}

    # ── 解析（访问矩阵所在）──────────────────────────────────────────────────

    def _entry(self, pointer_id: str) -> Optional[Dict[str, Any]]:
        """内部取条目缝（测试故障注入点）。"""
        return self._entries.get(pointer_id)

    def resolve(self, pointer_id: str, *, subject_id: Optional[str] = None,
                session_id: Optional[str] = None) -> Dict[str, Any]:
        """按指针取回全文。主体/会话缺省取请求 contextvar（服务端事实）。

        任何失败都返回 {"ok": False, "reason": ...} 且**绝不携带内容**——
        not_found / subject_mismatch / session_mismatch / expired / tampered /
        store_error 六态如实区分（任务卡验收 1：引用权限、篡改、过期、读取失败）。
        """
        pid = str(pointer_id or "")
        scope = current_scope()
        subj = str(subject_id if subject_id is not None else scope.get("subject_id", ""))
        sess = str(session_id if session_id is not None else scope.get("session_id", ""))
        try:
            entry = self._entry(pid)
            if entry is None:
                return {"ok": False, "reason": "not_found", "pointer_id": pid}
            if entry["subject_id"] != subj:
                return {"ok": False, "reason": "subject_mismatch", "pointer_id": pid}
            if entry["session_id"] != sess:
                return {"ok": False, "reason": "session_mismatch", "pointer_id": pid}
            if time.time() >= float(entry["expires_ts"]):
                return {"ok": False, "reason": "expired", "pointer_id": pid}
            if self._sha(entry["content"]) != entry["content_sha256"]:
                return {"ok": False, "reason": "tampered", "pointer_id": pid}
            return {"ok": True, "pointer_id": pid, "kind": entry["kind"],
                    "content": entry["content"], "chars": entry["chars"]}
        except Exception as ex:   # 存储读取失败：如实报错，绝不编造内容
            return {"ok": False, "reason": "store_error", "pointer_id": pid,
                    "error": str(ex)[:200]}

    # ── 窗口渲染（记忆块 → 关键内容全文 + 其余指针短行）───────────────────────

    def pointer_line(self, desc: Dict[str, Any]) -> str:
        title = f"·{desc['title']}" if desc.get("title") else ""
        return f"- {desc['pointer_id']} {desc['kind']}{title} {desc['chars']}字"

    # ── per-expert 展开使用记录（任务卡验收 3）────────────────────────────────

    @property
    def uses_path(self) -> str:
        return os.path.join(self._uses_dir, "pointer_uses.jsonl")

    def record_pointer_uses(self, request_id: str,
                            uses: List[Dict[str, Any]]) -> Optional[str]:
        """落盘一次请求中每个专家实际展开的 pointer_id（追加式 JSONL）。

        uses 为空列表也落盘——"没有展开"与"没有记录"是两个事实，
        回读端按 recorded 区分（诚实缺席，不编造）。
        """
        try:
            os.makedirs(self._uses_dir, exist_ok=True)
            row = {"request_id": str(request_id or ""), "uses": list(uses or []),
                   "recorded_ts": time.time()}
            with open(self.uses_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return self.uses_path
        except Exception:
            return None   # 落盘失败仅降级（诚实缺席），不阻断对话

    def get_pointer_uses(self, request_id: str) -> Dict[str, Any]:
        """读回某请求的 per-expert 使用记录；无记录 → recorded=false（诚实缺席）。"""
        try:
            if not os.path.exists(self.uses_path):
                return {"request_id": request_id, "uses": [], "recorded": False}
            rows: List[Dict[str, Any]] = []
            with open(self.uses_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("request_id") == request_id:
                        rows.append(r)
            if not rows:
                return {"request_id": request_id, "uses": [], "recorded": False}
            return {"request_id": request_id, "uses": rows[-1].get("uses") or [],
                    "recorded": True}
        except Exception:
            return {"request_id": request_id, "uses": [], "recorded": False}


def window_memory_prompt(mem_ctx: Any, *, store: EvidencePointerStore,
                         subject_id: str, session_id: str,
                         recent_keep: Optional[int] = None) -> str:
    """SM-31 窗口渲染：关键内容全文保留，其余注册为指针短行。

    输入是与 to_prompt_text() 同源的 MemoryContext（鸭子类型，避免本模块
    依赖 chromadb/redis 重导入）。分段与 to_prompt_text 对齐：
      [会话摘要] → [人员安全画像] → [偏好卡片] → [最近对话] → [有界引用]
    - 最近 recent_keep 轮全文；更早轮次中带 veto/escalated 标记的**全文保留**
      （否决证据绝不压缩，任务卡 scope），普通轮注册指针；
    - relevant_history（跨会话情景记忆）一律注册指针，不内联正文；
    - 登记失败（存储异常）退回该条全文保留——注入侧不丢本主体自有数据；
      越权/篡改/过期由展开侧 fail-closed 把守；
    - 空内容返回 ""（与旧渲染同语义，装配层按无块处理）。
    """
    if mem_ctx is None:
        return ""
    try:
        keep = recent_keep
        if keep is None:
            try:
                keep = int(os.getenv("SAFETYMIND_CTX_WINDOW_RECENT", "4"))
            except ValueError:
                keep = 4
        keep = max(1, int(keep))

        def _clean(text: Any) -> str:
            return str(text or "").encode("utf-8", errors="ignore").decode("utf-8")

        parts: List[str] = []
        pointer_descs: List[Dict[str, Any]] = []

        def _register(text: str, kind: str, title: str = "") -> str:
            """注册并返回短引用行；注册失败退回全文（不丢数据）。"""
            try:
                desc = store.register(content=text, subject_id=subject_id,
                                      session_id=session_id, kind=kind, title=title)
            except Exception:
                desc = None
            if desc is None:
                return _clean(text)
            pointer_descs.append(desc)
            return store.pointer_line(desc)

        summary = _clean(getattr(mem_ctx, "summary", "") or "")
        if summary:
            parts.append(f"[会话摘要]\n{summary}")

        relevant = list(getattr(mem_ctx, "relevant_history", None) or [])
        if relevant:
            lines = [_register(h, "history", "跨会话相关历史") for h in relevant[:3]]
            parts.append("[有界引用·相关历史]\n" + "\n".join(lines))

        profile = getattr(mem_ctx, "user_profile", None)
        if profile:
            parts.append(f"[人员安全画像]\n{json.dumps(profile, ensure_ascii=True)}")

        cards = list(getattr(mem_ctx, "preference_cards", None) or [])
        if cards:
            lines = []
            for c in cards[:5]:
                if not isinstance(c, dict):
                    continue
                title = _clean(str(c.get("title", "")).strip())
                content = _clean(str(c.get("content", "")).strip())
                if title or content:
                    lines.append(f"- {title}：{content}" if content else f"- {title}")
            if lines:
                parts.append("[偏好卡片]\n" + "\n".join(lines))

        recent = list(getattr(mem_ctx, "recent_messages", None) or [])
        if recent:
            kept, older = recent[-keep:], recent[:-keep]
            lines = []
            for m in older:
                meta = getattr(m, "metadata", None) or {}
                # 否决/升级证据无视窗口：全文保留（任务卡"保留否决"）
                if meta.get("veto") or meta.get("escalated"):
                    kept.append(m)
                    continue
                lines.append(_register(m.content, "conversation", "会话轮次"))
            kept.sort(key=lambda m: recent.index(m))   # 还原时间序
            conv_lines = [f"{getattr(m, 'role').value}: {_clean(m.content)}" for m in kept]
            parts.append("[最近对话]\n" + "\n".join(conv_lines))
            if lines:
                parts.append("[有界引用]\n" + "\n".join(lines))

        text = "\n\n".join(p for p in parts if p.strip())
        return text
    except Exception:
        raise   # 由调用方回退旧渲染（agents 层 try/except），本函数不做半截输出


# 进程级注册表单例（api 层装配与展开工具共用；测试可用 swap_for_test 替换）。
REGISTRY = EvidencePointerStore()


def swap_for_test(store: Optional[EvidencePointerStore]) -> None:
    """测试缝：临时替换进程级单例（None=恢复默认）。生产代码不得调用。"""
    global REGISTRY
    if store is None:
        REGISTRY = EvidencePointerStore()
    else:
        REGISTRY = store
