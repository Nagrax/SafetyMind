"""
亮点：多轮对话记忆管理

三级记忆架构，模拟人类记忆机制：
  1. 工作记忆（Redis）—— 当前会话的最近 N 条消息，毫秒级读写
  2. 情景记忆（ChromaDB）—— 跨会话的历史对话，按语义相似度检索
  3. 人员安全画像（ChromaDB）—— 从对话中提炼的岗位、常涉装置、
     设备/风险/化学品实体和历史安全行为（上报、培训、整改）

关键设计：
  - 上下文构建时三级记忆融合，按重要性 + 时效性排序
  - 工作记忆超过阈值时自动压缩（LLM 摘要），防止 context 爆炸
  - 所有 Embedding 通过 Anthropic API 生成，无本地模型
"""
import hashlib
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import chromadb
import redis.asyncio as redis
from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content

logger = logging.getLogger(__name__)


class MsgRole(Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"


@dataclass
class Message:
    role:       MsgRole
    content:    str
    timestamp:  datetime = field(default_factory=datetime.now)
    metadata:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryContext:
    """传给 Agent 的完整上下文。"""
    recent_messages:  List[Message]   # 工作记忆：最近对话
    relevant_history: List[str]       # 情景记忆：语义相关的历史片段
    user_profile:     Dict[str, Any]  # 用户画像：偏好、常用实体
    summary:          str             # 当前会话摘要（压缩后）

    @staticmethod
    def _clean(text: str) -> str:
        """移除 Unicode 代理字符，防止编码错误。"""
        return text.encode("utf-8", errors="ignore").decode("utf-8")

    def to_prompt_text(self) -> str:
        """将记忆上下文格式化为 LLM 可用的文本。"""
        parts = []
        if self.summary:
            parts.append(f"[会话摘要]\n{self._clean(self.summary)}")
        if self.relevant_history:
            parts.append("[相关历史]\n" + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        if self.user_profile:
            parts.append(f"[人员安全画像]\n{json.dumps(self.user_profile, ensure_ascii=True)}")
        if self.recent_messages:
            parts.append("[最近对话]")
            for m in self.recent_messages:
                parts.append(f"{m.role.value}: {self._clean(m.content)}")
        return "\n\n".join(parts)


class _MemoryRedis:
    """Redis 不可用时的进程内降级存储（桌面模式）。

    支持可选 JSON 快照持久化：写入即落盘、启动时恢复，
    桌面模式重启后会话与摘要不再丢失。TTL 以惰性过期近似实现
    （访问时检查过期时间戳，过期键立即清除）。
    """

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._lists: Dict[str, List[str]] = {}
        self._kv: Dict[str, str] = {}
        self._expiry: Dict[str, float] = {}
        self._persist_path = persist_path
        if persist_path and os.path.exists(persist_path):
            try:
                snap = json.load(open(persist_path, encoding="utf-8"))
                now = time.time()
                exp_all = snap.get("expiry", {})
                # 已过期的键连同数据一起丢弃（TTL 跨重启生效）
                self._lists = {k: v for k, v in snap.get("lists", {}).items()
                               if k not in exp_all or exp_all[k] > now}
                self._kv = {k: v for k, v in snap.get("kv", {}).items()
                            if k not in exp_all or exp_all[k] > now}
                self._expiry = {k: v for k, v in exp_all.items() if v > now}
            except Exception:
                pass  # 快照损坏则从空状态开始

    def _purge_expired(self, key: str) -> None:
        exp = self._expiry.get(key)
        if exp is not None and exp <= time.time():
            self._lists.pop(key, None)
            self._kv.pop(key, None)
            self._expiry.pop(key, None)

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"lists": self._lists, "kv": self._kv, "expiry": self._expiry},
                          f, ensure_ascii=False)
            os.replace(tmp, self._persist_path)
        except Exception:
            pass  # 持久化失败不阻断对话

    async def lpush(self, key: str, value: str) -> int:
        self._purge_expired(key)
        self._lists.setdefault(key, []).insert(0, value)
        self._save()
        return len(self._lists[key])

    async def lrange(self, key: str, start: int, end: int) -> List[str]:
        self._purge_expired(key)
        values = self._lists.get(key, [])
        if end == -1:
            return list(values[start:])
        return list(values[start:end + 1])

    async def llen(self, key: str) -> int:
        self._purge_expired(key)
        return len(self._lists.get(key, []))

    async def expire(self, key: str, seconds: int) -> bool:
        self._expiry[key] = time.time() + seconds
        self._save()
        return True

    async def get(self, key: str) -> Optional[str]:
        self._purge_expired(key)
        return self._kv.get(key)

    async def setex(self, key: str, seconds: int, value: str) -> bool:
        self._kv[key] = value
        self._expiry[key] = time.time() + seconds
        self._save()
        return True

    async def delete(self, key: str) -> int:
        existed = key in self._lists or key in self._kv
        self._lists.pop(key, None)
        self._kv.pop(key, None)
        self._expiry.pop(key, None)
        self._save()
        return 1 if existed else 0

    async def aclose(self) -> None:
        self._save()
        return None


class MemoryManager:
    """
    三级记忆管理器。

    工作记忆存 Redis（TTL 24h），情景记忆和用户画像存 ChromaDB（持久化）。
    Redis 连接失败时自动降级为进程内存储（JSON 快照持久化，重启不丢），
    保证桌面单进程模式可用。
    """

    WORKING_MAX   = 20    # 工作记忆最大条数，超过则触发压缩
    COMPRESS_AT   = 15    # 达到此条数时压缩，保留摘要 + 最近 5 条
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        chroma_host:  str = "localhost",
        chroma_port:  int = 8000,
        chroma_path:  str = "./data/chroma",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "claude-3-5-sonnet-20241022",
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self._model  = model

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._redis_local = False

        # ChromaDB：优先连接独立服务（docker compose 模式），连不上则降级为本地嵌入式
        self._use_chroma_server = True
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            chroma = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            chroma.heartbeat()  # 测试连接
            logger.info(f"ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"ChromaDB 服务不可用，使用本地嵌入式模式: {chroma_path}")
            chroma = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._use_chroma_server = False

        # 嵌入式（桌面）模式用本地 n-gram 向量，避免嵌入模型 CDN 下载；服务器模式由服务端嵌入
        embedding_function = None
        if not self._use_chroma_server:
            from core.local_embedding import LocalEmbeddingFunction
            embedding_function = LocalEmbeddingFunction()

        # 情景记忆：存储历史对话片段
        self._episodic = chroma.get_or_create_collection("episodic", embedding_function=embedding_function)
        # 用户画像：存储提炼出的偏好和实体
        self._profile  = chroma.get_or_create_collection("user_profile", embedding_function=embedding_function)

    # ── Redis 访问（失败自动降级进程内存储）────────────────────────────────────

    async def _redis_op(self, op: str, *args, **kwargs):
        """执行 Redis 命令；连接失败时切换为进程内降级存储并重试一次。"""
        try:
            return await getattr(self._redis, op)(*args, **kwargs)
        except Exception as ex:
            if self._redis_local:
                raise
            logger.warning(f"Redis 不可用，切换为进程内记忆降级（JSON 快照持久化，重启不丢）: {ex}")
            snap_path = os.getenv("SAFETYMIND_MEMORY_SNAPSHOT",
                                  os.path.join("data", "memory_snapshot.json"))
            self._redis = _MemoryRedis(persist_path=snap_path)
            self._redis_local = True
            return await getattr(self._redis, op)(*args, **kwargs)

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def add_message(
        self,
        user_id: str,
        conv_id: str,
        role:    MsgRole,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """将一条消息写入工作记忆，超阈值时自动压缩。"""
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        clean_metadata = {
            self._safe_text(k): self._safe_metadata_value(v)
            for k, v in (metadata or {}).items()
        }
        msg = Message(role=role, content=self._safe_text(content), metadata=clean_metadata)
        key = self._wm_key(user_id, conv_id)

        # 追加到 Redis 列表（左推，最新在前）
        await self._redis_op("lpush", key, json.dumps({
            "role":      msg.role.value,
            "content":   msg.content,
            "ts":        msg.timestamp.isoformat(),
            "metadata":  msg.metadata,
        }))
        await self._redis_op("expire", key, 86400)  # 24h TTL

        # 超过压缩阈值时触发压缩
        if await self._redis_op("llen", key) >= self.COMPRESS_AT:
            await self._compress(user_id, conv_id)

    async def update_profile(self, user_id: str, conv_id: str) -> None:
        """
        从当前工作记忆中提炼人员安全画像，更新用户画像。
        用 LLM 提炼画像，然后存入 ChromaDB（ChromaDB 内置 embedding，不依赖外部 API）。
        """
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return

        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in messages[-10:]))
        prompt = f"""从以下安全生产对话中提炼人员安全画像，返回 JSON。
对话:
{text}

返回格式: {{"duty": "岗位/职责（未提及则空字符串）", "site": "常涉装置/车间（未提及则空字符串）", "entities": {{"设备编号": [], "风险类型": [], "化学品": []}}, "safety_history": ["历史隐患上报/培训取证/违章整改等条目，没有则空数组"]}}"""
        prompt = self._safe_text(prompt)

        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=512, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            s, e = raw.find("{"), raw.rfind("}") + 1
            profile_data = json.loads(raw[s:e])

            doc_id = f"{user_id}_profile_{conv_id}"
            doc_text = self._safe_text(json.dumps(profile_data, ensure_ascii=False))

            try:
                await asyncio.to_thread(self._profile.delete, ids=[doc_id])
            except Exception:
                pass

            # 直接传 documents，让 ChromaDB 内置模型生成 embedding（不依赖 Voyage API）
            await asyncio.to_thread(
                self._profile.add,
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[{"user_id": user_id, "conv_id": conv_id,
                            "ts": datetime.now().isoformat()}],
            )
            logger.info(f"用户画像已更新: {user_id}")
        except Exception as ex:
            logger.warning(f"更新用户画像失败: {ex}")

    # ── 读取 ──────────────────────────────────────────────────────────────────

    async def get_context(self, user_id: str, conv_id: str, query: str = "") -> MemoryContext:
        """
        构建完整的记忆上下文。

        query 用于从情景记忆中检索语义相关的历史片段。
        """
        # 1. 工作记忆（当前会话最近消息）
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        query = self._safe_text(query)

        recent = await self._get_working_memory(user_id, conv_id)

        # 2. 情景记忆（跨会话语义检索）
        history = await self._search_episodic(user_id, query or (recent[-1].content if recent else ""))

        # 3. 用户画像
        profile = await self._get_profile(user_id)

        # 4. 会话摘要（如果已压缩过）
        summary = await self._redis_op("get", self._summary_key(user_id, conv_id)) or ""

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            user_profile=profile,
            summary=summary,
        )

    # ── 压缩（防止 context 爆炸）─────────────────────────────────────────────

    async def _compress(self, user_id: str, conv_id: str) -> None:
        """
        工作记忆压缩：
          1. 用 LLM 对旧消息生成摘要
          2. 摘要存 Redis（覆盖旧摘要）
          3. 旧消息存入情景记忆（ChromaDB）供跨会话检索
          4. 工作记忆只保留最近 5 条
        """
        messages = await self._get_working_memory(user_id, conv_id)
        if len(messages) < self.COMPRESS_AT:
            return

        to_compress = messages[:-5]   # 保留最近 5 条
        keep        = messages[-5:]

        # LLM 摘要
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress))
        prompt = self._safe_text(f"用 2-3 句话总结以下对话的关键信息：\n{text}")
        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=256, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = self._safe_text(extract_text_content(resp.content)).strip()
        except Exception:
            summary = f"对话包含 {len(to_compress)} 条消息（摘要生成失败）"

        # 存摘要到 Redis
        skey = self._summary_key(user_id, conv_id)
        old_summary = await self._redis_op("get", skey) or ""
        new_summary = self._safe_text(f"{old_summary}\n{summary}").strip()
        await self._redis_op("setex", skey, 86400, new_summary)

        # 旧消息存入情景记忆
        await self._store_episodic(user_id, conv_id, text, summary)

        # 重置工作记忆为最近 5 条
        key = self._wm_key(user_id, conv_id)
        await self._redis_op("delete", key)
        for m in reversed(keep):
            await self._redis_op("lpush", key, json.dumps({
                "role": m.role.value, "content": m.content,
                "ts": m.timestamp.isoformat(), "metadata": m.metadata,
            }))
        await self._redis_op("expire", key, 86400)
        logger.info(f"工作记忆压缩完成: {user_id}/{conv_id}，摘要 {len(summary)} 字")

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    async def _get_working_memory(self, user_id: str, conv_id: str) -> List[Message]:
        key  = self._wm_key(user_id, conv_id)
        raws = await self._redis_op("lrange", key, 0, self.WORKING_MAX - 1)
        msgs = []
        for raw in reversed(raws):  # Redis lpush 最新在前，reversed 还原时序
            d = json.loads(raw)
            msgs.append(Message(
                role=MsgRole(d["role"]),
                content=d["content"],
                timestamp=datetime.fromisoformat(d["ts"]),
                metadata=d.get("metadata", {}),
            ))
        return msgs

    async def _search_episodic(self, user_id: str, query: str) -> List[str]:
        """语义检索情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        query_text = self._safe_text(query).strip()
        if not query_text:
            return []
        try:
            # 直接传 query_texts，ChromaDB 内置模型自动生成向量做匹配
            results = await asyncio.to_thread(
                self._episodic.query,
                query_texts=[query_text],
                n_results=self.HISTORY_TOP_K,
                where={"user_id": self._safe_text(user_id)},
            )
            docs = results["documents"][0] if results["documents"] else []
            return [self._safe_text(doc) for doc in docs if isinstance(doc, str) and doc.strip()]
        except Exception as ex:
            logger.warning(f"情景记忆检索失败: {ex}")
            return []

    async def _store_episodic(self, user_id: str, conv_id: str, text: str, summary: str) -> None:
        """将压缩后的对话片段存入情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        try:
            user_id = self._safe_text(user_id)
            conv_id = self._safe_text(conv_id)
            text = self._safe_text(text)
            summary = self._safe_text(summary)
            doc_id = hashlib.md5(f"{user_id}{conv_id}{time.time()}".encode()).hexdigest()
            # 直接传 documents，ChromaDB 内置模型自动生成 embedding
            await asyncio.to_thread(
                self._episodic.add,
                ids=[doc_id],
                documents=[summary],
                metadatas=[{"user_id": user_id, "conv_id": conv_id,
                            "ts": datetime.now().isoformat(), "full_text": self._safe_text(text[:500])}],
            )
        except Exception as ex:
            logger.warning(f"存储情景记忆失败: {ex}")

    async def _get_profile(self, user_id: str) -> Dict[str, Any]:
        """获取用户画像（取最新一条）。"""
        try:
            results = await asyncio.to_thread(self._profile.get, where={"user_id": user_id}, limit=1)
            if results["documents"]:
                return json.loads(results["documents"][0])
        except Exception:
            pass
        return {}

    async def close(self) -> None:
        """关闭异步 Redis 连接。"""
        await self._redis.aclose()

    @staticmethod
    def _wm_key(user_id: str, conv_id: str) -> str:
        return f"wm:{user_id}:{conv_id}"

    @staticmethod
    def _summary_key(user_id: str, conv_id: str) -> str:
        return f"summary:{user_id}:{conv_id}"

    @staticmethod
    def _safe_text(value: Any) -> str:
        """转成 ChromaDB 可接受的普通 UTF-8 字符串。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @classmethod
    def _safe_metadata_value(cls, value: Any) -> Any:
        """递归清洗 metadata，避免 Redis/ChromaDB 后续读写遇到非法 UTF-8。"""
        if isinstance(value, str):
            return cls._safe_text(value)
        if isinstance(value, dict):
            return {cls._safe_text(k): cls._safe_metadata_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._safe_metadata_value(v) for v in value]
        return value
