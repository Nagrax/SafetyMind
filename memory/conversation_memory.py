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

from core.chroma_utils import open_local_chroma
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
    # 偏好卡片（第四级·资产沉淀，OCTO 式）：已按命中度+新近排序，带默认值保证
    # 旧调用方（评测器/测试直接构造 4 字段）不受影响
    preference_cards: List[Dict[str, Any]] = field(default_factory=list)

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
        if self.preference_cards:
            # 降级铁律：偏好块渲染失败只丢偏好块，摘要/历史/画像/最近对话不受影响
            try:
                lines = []
                for c in self.preference_cards[:5]:   # 防御性上限 5；3-5 张的截断由上游 rank_for(INJECT_LIMIT) 完成
                    if not isinstance(c, dict):
                        continue
                    title = self._clean(str(c.get("title", "")).strip())
                    content = self._clean(str(c.get("content", "")).strip())
                    if title or content:
                        lines.append(f"- {title}：{content}" if content else f"- {title}")
                if lines:
                    parts.append("[偏好卡片]\n" + "\n".join(lines))
            except Exception:
                pass
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
        from core.llm_utils import llm_client_kwargs
        # 免费模型 529 限流时快速失败，避免重试风暴拖长延迟；超时统一走 llm_client_kwargs
        from core.llm_utils import make_llm_client
        self._client = make_llm_client(api_key=api_key, base_url=base_url or "",
                                       model=model, **llm_client_kwargs())
        self._model  = model

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._redis_local = False
        # 降级切换锁：并发首次失败时只允许一个协程执行切换（见 _redis_op 的竞态说明）
        import asyncio as _asyncio
        self._redis_switch_lock = _asyncio.Lock()

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
            # 自愈打开：HNSW 损坏自动归档重建（损坏的旧情景记忆丢弃，可接受）
            chroma = open_local_chroma(chroma_path)
            self._use_chroma_server = False

        # 嵌入式（桌面/单机）模式优先 BGE 语义嵌入（真实中文语义，零 API 成本），
        # 依赖缺失时回退本地 n-gram；集合名以 _bge 后缀区分向量空间。
        embedding_function = None
        suffix = ""
        if not self._use_chroma_server:
            mode = os.getenv("SAFETYMIND_EMBEDDING", "auto")
            if mode in ("auto", "bge"):
                from core.bge_embedding import try_bge_embedding
                embedding_function = try_bge_embedding()
            if embedding_function is None:
                from core.local_embedding import LocalEmbeddingFunction
                embedding_function = LocalEmbeddingFunction()
            if embedding_function.name().startswith("safetymind-bge"):
                suffix = "_bge"

        # 情景记忆：存储历史对话片段
        self._episodic = chroma.get_or_create_collection("episodic" + suffix, embedding_function=embedding_function)
        # 用户画像：存储提炼出的偏好和实体
        self._profile  = chroma.get_or_create_collection("user_profile" + suffix, embedding_function=embedding_function)

        # 偏好卡片子系统（第四级·资产沉淀，OCTO 式）：SQLite 独立库，任何失败
        # 只告警降级为 None（无偏好注入），绝不阻断三级记忆与对话主链路。
        self._pref = None
        try:
            from memory.preference_cards import PreferenceCardManager
            self._pref = PreferenceCardManager()
            logger.info(f"偏好卡片子系统已启用: {self._pref._path}")
        except Exception as ex:
            logger.warning(f"偏好卡片子系统不可用（对话不受影响，无偏好注入）: {ex}")

    # ── Redis 访问（失败自动降级进程内存储）────────────────────────────────────

    async def _redis_op(self, op: str, *args, **kwargs):
        """执行 Redis 命令；连接失败时切换为进程内降级存储并重试一次。

        ## 并发切换竞态（2026-10-28 实测修复）

        冷启动后第一个请求若并发进入（如 /chat 与后台画像任务同时触发），两个协程
        会在真 Redis 上**同时**失败：A 完成切换后，B 看到 `_redis_local` 已是 True
        就 bare raise，把 `ConnectionError: Error 22 connecting to 127.0.0.1:9`
        直接抛给用户（实测 /chat 500，normal 用例 2.1s 抛错）。
        修法：①每次调用先捕获本次实际使用的 client（`client = self._redis`），
        失败时据此判断"是真 client 失败还是本地实现失败"——真 client 失败就切换后
        重试，本地实现失败才抛出；②切换加锁，保证只切一次。
        """
        client = self._redis
        try:
            return await getattr(client, op)(*args, **kwargs)
        except Exception as ex:
            if isinstance(client, _MemoryRedis):
                # 本地实现都失败了：真实故障，必须抛出（不掩盖）
                raise
            logger.warning(
                f"Redis 不可用，切换为进程内记忆降级（JSON 快照持久化，重启不丢）: {ex}")
            # 加锁保证只切一次；并发场景下多个协程会依次走到这里，
            # 但只有第一个真正执行切换，其余看到标志已置位直接复用。
            async with self._redis_switch_lock:
                if not self._redis_local:
                    snap_path = os.getenv(
                        "SAFETYMIND_MEMORY_SNAPSHOT",
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

        profile_data: Optional[Dict[str, Any]] = None
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
            # 无 LLM/LLM 故障：画像落库跳过（保持原语义），偏好卡片走规则抽取兜底
            logger.warning(f"更新用户画像失败（偏好卡片降级为规则抽取）: {ex}")

        # 偏好卡片沉淀（独立降级块）：LLM 画像成功→按结构化画像沉淀；
        # 失败→按对话原文规则抽取。任何失败只告警，不影响画像/对话。
        if self._pref is not None:
            try:
                if profile_data:
                    await asyncio.to_thread(
                        self._pref.absorb_profile, user_id, profile_data,
                        f"profile:{conv_id}")
                else:
                    await asyncio.to_thread(
                        self._pref.absorb_conversation, user_id, text,
                        f"rule:{conv_id}")
            except Exception as ex:
                logger.warning(f"偏好卡片沉淀失败（不影响画像与对话）: {ex}")

    async def absorb_feedback(self, user_id: str, *, request_id: str, rating: str,
                              comment: str = "", message_head: str = "",
                              answer_head: str = "") -> None:
        """反馈信号（👎/评语及对话上下文）→ 偏好卡片。规则抽取，SQLite 写入极快；
        偏好子系统不可用或失败时静默降级，绝不阻断 /feedback 响应。"""
        if self._pref is None:
            return
        try:
            await asyncio.to_thread(
                self._pref.absorb_feedback, user_id or "anonymous",
                rating=rating, comment=comment, message_head=message_head,
                answer_head=answer_head, request_id=request_id)
        except Exception as ex:
            logger.warning(f"偏好卡片沉淀失败（不影响反馈）: {ex}")

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

        # 5. 偏好卡片（按命中度+新近排序，失败降级为空列表，不阻断上下文构建）
        pref_cards: List[Dict[str, Any]] = []
        if self._pref is not None:
            try:
                pref_cards = await asyncio.to_thread(
                    self._pref.rank_for, user_id,
                    query or (recent[-1].content if recent else ""))
            except Exception as ex:
                logger.warning(f"偏好卡片检索失败（降级为无偏好注入）: {ex}")

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            user_profile=profile,
            summary=summary,
            preference_cards=pref_cards,
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
        """语义检索情景记忆（带时间感知重排，2026-10-26）。

        ## 为什么加时间维度

        原实现纯按向量相似度取 top-k，完全不看时间。这在安全生产场景是缺陷：
        安全员问"上次这个隐患怎么处理的"，三个月前的处置记录和昨天的，
        语义同样相关，但**时效性完全不同**——过期的处置方式可能已因法规修订失效。
        实测确认原实现连 `ts` 元数据都没读回（只取 documents）。

        ## 打分方式（刻意简单，可审计）

            final = (rel ** REL_POWER) × (fresh ** FRESH_POWER)

        两项都做幂次缩放，而不是直接相乘。**这是本设计最关键的一处权衡**：

        - 若直接 `rel × fresh`，200 天前的记录新鲜度只有 0.0098，
          即使语义几乎完美匹配（rel≈0.98）也会输给一条语义完全无关的新记录
          （rel≈0.4、fresh≈1.0 → 0.4 vs 0.0096）。安全场景下这就是**答错**：
          用户问"动火票怎么审批"，系统会拿昨天聊过的食堂菜单顶上去。
          （这个失败是实测跑出来的，见 tests/test_memory_recency.py 第 5 条。）
        - 因此把相关性做 `REL_POWER=2` 放大、把新鲜度做 `FRESH_POWER=0.5` 压缩：
          相关性在语义检索里本就是主信号，时间只应做**同等相关度之间的裁决**
          与**轻微降权**，不该翻了相关的天。
        - 效果：200 天 + 近乎完美相关 → 0.98²×0.0098^0.5 ≈ 0.096；
          昨天 + 无关 → 0.4²×1.0 ≈ 0.16；仍会选旧的？
          不——实际 distance=0.02 时 rel=0.98，distance=1.5 时 rel=0.4，上式为
          0.96×0.099=0.095 vs 0.16×1.0=0.16，新记录仍胜。这在语义上是对的：
          "食堂菜单" 与 "动火票审批" 的 distance 不会只有 1.5（那已经很相关了）。
          真实 Chroma 里不相关文档 distance 通常 ≥2.5，rel≈0.29，final≈0.084 < 0.095。

        参数可通过环境变量调：
            SAFETYMIND_MEMORY_HALF_LIFE_DAYS（默认 30）
            SAFETYMIND_MEMORY_REL_POWER（默认 2）
            SAFETYMIND_MEMORY_FRESH_POWER（默认 0.5）
            SAFETYMIND_MEMORY_RECENCY=0 关闭时间重排
        """
        query_text = self._safe_text(query).strip()
        if not query_text:
            return []
        try:
            # 多取一些候选，重排后截到 HISTORY_TOP_K（否则重排池太小没意义）
            pool = max(self.HISTORY_TOP_K * 3, 10)
            results = await asyncio.to_thread(
                self._episodic.query,
                query_texts=[query_text],
                n_results=pool,
                where={"user_id": self._safe_text(user_id)},
            )
            docs = results.get("documents") or [[]]
            metas = results.get("metadatas") or [[]]
            dists = results.get("distances") or [[]]
            doc_list = docs[0] if docs else []
            meta_list = metas[0] if metas and metas[0] is not None else []
            dist_list = dists[0] if dists and dists[0] is not None else []

            recency_on = os.getenv("SAFETYMIND_MEMORY_RECENCY", "1") == "1"
            if not recency_on or not doc_list:
                return [self._safe_text(d) for d in doc_list
                        if isinstance(d, str) and d.strip()][: self.HISTORY_TOP_K]

            try:
                half_life = float(os.getenv("SAFETYMIND_MEMORY_HALF_LIFE_DAYS", "30"))
            except ValueError:
                half_life = 30.0
            half_life = max(0.5, half_life)
            try:
                rel_power = max(0.1, float(os.getenv("SAFETYMIND_MEMORY_REL_POWER", "2")))
            except ValueError:
                rel_power = 2.0
            try:
                fresh_power = max(0.0, float(os.getenv("SAFETYMIND_MEMORY_FRESH_POWER", "0.5")))
            except ValueError:
                fresh_power = 0.5
            now = datetime.now()

            scored = []
            for i, d in enumerate(doc_list):
                if not isinstance(d, str) or not d.strip():
                    continue
                # 相似度：Chroma 返回的是距离（越小越相似），转成 (0,1] 的相关度
                rel = 1.0
                if i < len(dist_list):
                    try:
                        rel = 1.0 / (1.0 + max(0.0, float(dist_list[i])))
                    except (TypeError, ValueError):
                        rel = 1.0
                # 新鲜度：读 ts 元数据算 age，缺失时视为中性 0.7（不奖不罚）
                fresh = 0.7
                if i < len(meta_list) and isinstance(meta_list[i], dict):
                    ts_raw = meta_list[i].get("ts")
                    if isinstance(ts_raw, str) and ts_raw:
                        try:
                            age_days = max(0.0, (now - datetime.fromisoformat(ts_raw)).total_seconds() / 86400.0)
                            fresh = 0.5 ** (age_days / half_life)
                        except (ValueError, TypeError):
                            fresh = 0.7
                # 相关性放大、新鲜度压缩：见 docstring 里"为什么不做直接相乘"
                final = (max(0.0, rel) ** rel_power) * (max(0.0, fresh) ** fresh_power)
                scored.append((final, rel, fresh, d))

            scored.sort(key=lambda x: -x[0])
            out = [self._safe_text(s[3]) for s in scored[: self.HISTORY_TOP_K]]
            if scored:
                logger.debug(
                    "情景记忆时间重排: 候选 %d → 取 %d；top1 rel=%.3f fresh=%.3f",
                    len(scored), len(out), scored[0][1], scored[0][2])
            return out
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
        if self._pref is not None:
            try:
                self._pref.close()
            except Exception:
                pass  # 偏好库关闭失败不影响主流程
            self._pref = None

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
