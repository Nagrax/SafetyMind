# -*- coding: utf-8 -*-
"""知识类问答的精确匹配回复缓存（2026-10-28，闭环"降延迟"设计）。

## 为什么做

实测延迟：RAG 类问题 p50 30-45s（LLM 生成为主）。而安全员的真实工作里，
**同一批问题会被反复问**：动火票有效期、隐患分级口径、某化学品闪点、
台账怎么写。每次重跑一遍检索+生成毫无信息增量。

## 为什么只做"精确匹配"，不做语义匹配

业界常见做法是语义缓存（embedding 相似即命中），但对安全生产领域**风险不可接受**：
"乙醇闪点是多少" 与 "甲醇闪点是多少" 语义高度相似，答案却完全不同；
"动火票有效期" 与 "受限空间作业票有效期" 同理。语义误命中 = 给出错误的法规依据，
在安监检查场景里是事故级错误。

因此这里只做**规范化后的精确匹配**：
  - 消息规范化（去空白、全角转半角、统一大小写）后必须**完全一致**
  - 且意图一致、知识库版本一致、模型一致
语义缓存列为**明确不做**（记录在此，防止后续被当成"没做优化"）。

## 失效策略（防陈旧）

1. `kb_version`：知识库写入即失效（任何文档上传/删除都会 bump），
   避免"库改了还答旧答案"
2. `model`：模型变了不复用（不同模型答案分布不同）
3. TTL：默认 6 小时（`SAFETYMIND_CACHE_TTL_S`），兜住"没人改库但内容过期"
4. LRU 上限：默认 512 条（`SAFETYMIND_CACHE_MAX`），防内存无界

## 只缓存什么

只缓存**知识类**意图（regulation_query / chemical_safety / hazard_inspection /
training_management 等确定性事实问答）。**绝不缓存**：
  - 紧急/事故类（时效性极强，且应急模板本身已 <300ms）
  - 撰写类（写作任务每次都应重新组织，且用户要的就是"新生成"）
  - 反馈/寒暄（无信息量）

## 命中判据（可测）

命中返回的响应必须与未命中时**逐字节一致**（除 cached 标记），
这是测试里锁死的契约。
"""
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

# 可缓存的意图（确定性事实问答）。用字符串比较，避免 import 循环依赖意图枚举。
# 枚举值取自 core/intent_recognizer.IntentCategory（2026-10-28 核对）：
# 知识/事实类意图可缓存；急事、事故、转人工、撰写、寒暄、反馈不缓存。
CACHEABLE_INTENTS = frozenset({
    "work_permit",            # 作业票（动火票有效期等，最常被重复问）
    "regulation_query",       # 法规查询
    "chemical_safety",        # 化学品安全
    "hazard_inspection",      # 隐患排查判定口径
    "training_cert",          # 培训取证
    "inspection_audit",       # 检查审计
    "occupational_health",    # 职业健康
    "ppe_inquiry",            # 劳保用品
    "equipment_maintenance",  # 设备维保
    "safety_document",        # 安全文件（制度/台账模板类问答）
    "general_consult",        # 通用安全咨询
})

# 明确不缓存的意图（安全/时效/创作原因，注释见文件头）
NEVER_CACHE_INTENTS = frozenset({
    "emergency_response", "incident_report", "escalation",
    "feedback", "greeting", "other",
    # equipment_alarm / hazard_report 是此刻发生的具体事件，答案取决于现场，
    # 缓存会答非当次事件 —— 明确排除。
    "equipment_alarm", "hazard_report",
})

_kb_version = 0
_kb_lock = threading.Lock()


def bump_kb_version() -> int:
    """知识库写入后调用：使所有缓存条目失效（版本进 key，天然失效）。"""
    global _kb_version
    with _kb_lock:
        _kb_version += 1
        return _kb_version


def kb_version() -> int:
    with _kb_lock:
        return _kb_version


def normalize(message: str) -> str:
    """规范化：去首尾与连续空白、全角转半角、统一小写。

    只做**不改变语义**的规范化。不做同义词归并、不做分词——那会引入误命中。
    """
    s = (message or "").strip()
    # 全角 → 半角（ASCII 区间）
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    s = "".join(out)
    return " ".join(s.split()).lower()


class ResponseCache:
    """线程安全的 LRU + TTL 精确匹配缓存。"""

    def __init__(self, max_entries: Optional[int] = None, ttl_s: Optional[float] = None):
        self._max = int(max_entries or os.getenv("SAFETYMIND_CACHE_MAX", "512"))
        self._ttl = float(ttl_s if ttl_s is not None
                          else os.getenv("SAFETYMIND_CACHE_TTL_S", "21600"))
        self._data: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(message: str, intent: str, model: str) -> str:
        raw = "\x1f".join([normalize(message), str(intent), str(model),
                           "kb" + str(kb_version())])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def enabled(self) -> bool:
        return (os.getenv("SAFETYMIND_RESPONSE_CACHE", "1") == "1"
                and self._ttl > 0 and self._max > 0)

    def get(self, message: str, intent: str, model: str) -> Optional[Dict[str, Any]]:
        if not self.enabled():
            return None
        key = self.make_key(message, intent, model)
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            ts, payload = item
            if now - ts > self._ttl:
                # 过期即删（不返回陈旧内容）
                self._data.pop(key, None)
                self.misses += 1
                return None
            self._data.move_to_end(key)     # LRU 提升
            self.hits += 1
            return {**payload, "cached": True, "cache_age_s": round(now - ts, 1)}

    def put(self, message: str, intent: str, model: str,
            payload: Dict[str, Any]) -> None:
        if not self.enabled():
            return
        if str(intent) in NEVER_CACHE_INTENTS or str(intent) not in CACHEABLE_INTENTS:
            return
        # 只有真正的成功回答才缓存：空回答/降级文案进去只会放大故障
        resp = str(payload.get("response") or "")
        if len(resp) < 20 or payload.get("degraded"):
            return
        key = self.make_key(message, intent, model)
        with self._lock:
            self._data[key] = (time.time(), dict(payload))
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)   # 淘汰最旧

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._data), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
                "max_entries": self._max, "ttl_s": self._ttl,
                "kb_version": kb_version(),
                "enabled": self.enabled(),
            }

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0


# 进程级单例（/chat 与 /cache/stats 共用）
CACHE = ResponseCache()
