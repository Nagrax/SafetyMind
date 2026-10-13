# -*- coding: utf-8 -*-
"""偏好卡片子系统（借鉴 OCTO 的"偏好卡片"资产沉淀，挂接三级记忆）。

从两类信号中沉淀用户偏好卡片，注入对话上下文提升个性化：
  1. 反馈信号：POST /feedback 的 👎/评语及附带对话上下文（message_head/answer_head）；
  2. 画像更新：MemoryManager.update_profile 提炼的人员安全画像
     （{duty, site, entities, safety_history}）。

卡片类型（card_type）：
  - device_type      常问设备类型（设备位号 R-101 / 设备名词 反应釜）
  - sop_category     高频 SOP 类别（八大特殊作业、隐患排查、应急…）
  - answer_style     回答详略偏好（"太长了说重点"→简洁；"太笼统展开讲"→详细）
  - taboo_expression 禁忌表述（"液碱不是火碱，别再叫错了"→多次纠正沉淀成卡）
  - role_site        岗位与常涉区域（来自画像 duty/site）
  - chemical / risk  高频化学品 / 关注风险（来自画像 entities）

抽取策略：规则抽取（实体/关键词统计）为主路径——确定性、零 LLM 依赖、
dead-LLM 模式可跑；LLM 只出现在画像提炼一层（update_profile），
其失败自动降级为规则抽取，本模块自身不发 LLM 请求。

存储：独立 SQLite 单文件（默认 data/preference_cards.db，env PREFERENCE_DB_PATH），
遵循 core/audit_store.py 的 WAL + 线程锁 + 逐写 commit 风格。
独立成库而非并入 data/audit.db 的理由：
  - 生命周期不同：审计是合规留痕（断电不丢、只增不改），偏好卡片是可再生的
    衍生资产（可整库删除重建，不影响审计追责）；
  - 故障隔离：偏好子系统整体可降级（MemoryManager 侧 try/except），独立文件
    损坏/被锁不连坐审计库；两模块各自持连接，同库双写还会引入 SQLITE_BUSY 风险；
  - 测试隔离：测试用 PREFERENCE_DB_PATH 指向临时目录，不碰真实审计数据。

注入纪律：
  - 每卡至少被信号强化 MIN_HITS 次才参与注入（一次偶发不满不沉淀，"屡次"才成卡）；
  - rank_for 按 命中度(当前 query 关键词覆盖) + 强度(hit_count) + 新鲜度(updated_ts)
    排序，最多注入 INJECT_LIMIT(4，3-5 区间) 张；
  - 单用户容量上限 MAX_CARDS_PER_USER(50)，超出剪除排名最低者。
"""
import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 规则抽取词表（确定性纯函数，可单测）────────────────────────────────────────
_DEVICE_NOUNS = (
    "反应釜", "反应器", "储罐", "锅炉", "空压机", "离心机", "压缩机", "泵",
    "阀门", "配电柜", "变压器", "升降机", "叉车", "传送带", "通风橱", "除尘器",
)
# 设备位号：R-101 / P102 / T_404 这类字母前缀+数字编号。
# 用 ASCII 字母数字 lookaround 而非 \b：中文紧贴位号（"泵R-101异常"、"Q-1000检查"）
# 时 \b 因 CJK 也属 \w 而失配。
_DEVICE_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,4}[-_]\d{2,5}|[A-Z]{1,4}\d{3,5})(?!\d)")
# 无分隔符位号（P102 式）须排除标准号前缀：GB50160/API2023/NFPA654 是法规标准，
# 本域语料极高频，误当"常问设备"会污染注入（宁漏不误）。
_STANDARD_PREFIXES = {
    "GB", "GBZ", "AQ", "DL", "JB", "HG", "SH", "SY", "YB", "QB", "JT", "TB",
    "MT", "CJ", "JG", "CECS", "ISO", "API", "ASME", "IEC", "IEEE", "NFPA", "OSHA",
}
_SOP_TOPICS = (
    "动火", "受限空间", "高处作业", "临时用电", "吊装", "动土", "断路", "盲板",
    "隐患排查", "应急预案", "安全培训", "消防", "职业健康", "劳动防护", "变更管理",
)
_CONCISE_CUES = ("太长", "太啰嗦", "太详细", "太繁琐", "精简", "简洁", "简短", "说重点", "抓重点")
_DETAIL_CUES = ("太简单", "太笼统", "太简略", "不够详细", "不够具体", "展开", "详细讲", "细讲", "要步骤")

# 禁忌/纠正表述（用户评论中的中文纠正句式）
_TABOO_PATTERNS = (
    # "不要叫火碱" / "别再叫X了" / "不要用X这种说法"
    re.compile(r"(?:不要|别)(?:再)?(?:叫|称|说|写|用)(.{1,12}?)(?:这种说法|这个叫法)?(?:了)?(?:[，。；,.!！?？\s]|$)"),
    # "不要把乙炔叫电石气" / "别把X说成Y"
    re.compile(r"(?:不要|别)把(.{1,12}?)(?:叫|称|说|写成|当成)"),
    # "应该叫液碱" / "应该称X"
    re.compile(r"应该(?:叫|称|写|用)(.{1,12}?)(?:[，。；,.!！?？\s]|$)"),
    # "正确名称是液碱" / "规范叫法是X"
    re.compile(r"(?:正确|规范|正式|标准)(?:名称|叫法|说法|术语)(?:是|为|应该叫|应该称)(.{1,12}?)(?:[，。；,.!！?？\s]|$)"),
    # "火碱是错误说法" / "X这个叫法是错的"
    re.compile(r"(.{1,12}?)(?:这个叫法|这种说法|这个说法)(?:是)?(?:错误|不对|不正确|是错的)"),
)
# "液碱不是火碱，是/应该叫…" → 禁忌=group1，正确=group2
_TABOO_DIFF_RE = re.compile(r"(.{1,12}?)不是(.{1,12}?)(?:，|,)?(?:而?是|应该叫|应该称)(.{1,12}?)(?:[，。；,.!！?？\s]|$)")


def _clean_term(term: str) -> str:
    """清洗捕获的术语：去标点与空白，截断。"""
    return re.sub(r"[\s，。；、,.!！?？：:\"'“”‘'()（）]+", "", str(term or ""))[:12]


# 通用词黑名单 + 最短 2 字：修辞句（"安全生产不是负担，而是责任"——安全培训语料
# 高频）与无术语纠正（"别再叫错了"）不得成"用户已多次纠正"卡。
_GENERIC_TERMS = {
    "错", "对", "好", "坏", "是", "非", "真", "假", "了", "的",
    "负担", "责任", "义务", "问题", "成本", "任务", "工作", "安全", "生产",
    "管理", "培训", "教育", "风险", "事故", "隐患", "规定", "要求", "制度",
    "标准", "流程", "内容", "重点", "关键", "基础", "目标", "计划", "结果",
    "原因", "情况", "事情", "东西", "名称", "叫法", "说法", "术语",
    "这个", "那个", "上述", "以下",
}
# 纠正卡必须含"称呼/命名"义动词：区分「纠正说法」与「X不是Y而是Z」式议论修辞
_NAMING_VERB_RE = re.compile(r"[叫称写说]|名称|叫法|说法|术语")


def _valid_term(term: str) -> bool:
    """术语有效性：≥2 字且不在通用词黑名单（宁漏不误）。"""
    return len(term) >= 2 and term not in _GENERIC_TERMS


def _norm_key(text: str) -> str:
    """卡片唯一键的规范化：小写、去空白、截断（保证同名信号稳定命中同一张卡）。"""
    return re.sub(r"\s+", "", str(text or "").strip().lower())[:40]


def extract_taboo(comment: str) -> Optional[Dict[str, Any]]:
    """从纠正性评论中提取禁忌表述卡片（无匹配返回 None）。

    宁漏不误三道闸：①"X不是Y(而)是Z"式匹配必须含称呼动词（区分纠正说法与议论
    修辞）；②捕获术语 ≥2 字；③通用词黑名单（负担/责任/标准/流程等抽象词）。
    """
    text = str(comment or "")
    taboo, correct = "", ""
    m = _TABOO_DIFF_RE.search(text)
    if m and _NAMING_VERB_RE.search(m.group(0)):
        # "A不是B，(而)是/应该叫C"：B 为错称（taboo），C 为正称，C 缺失时回退 A
        t2, t3 = _clean_term(m.group(2)), _clean_term(m.group(3))
        if _valid_term(t2):
            taboo = t2
        correct = (t3 if _valid_term(t3) else "") or \
                  (_clean_term(m.group(1)) if _valid_term(_clean_term(m.group(1))) else "")
    if not taboo:
        for pat in _TABOO_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            term = _clean_term(m.group(1))
            if not _valid_term(term):
                continue   # 通用词/单字不入卡，继续试下一句式
            # 否定句式（不要叫X / X是错的）→ X 即禁忌；肯定句式（应该叫X）→ X 即正称
            if re.search(r"不要|别|错|不对", m.group(0)):
                taboo = term
            else:
                correct = correct or term
            break
    if not taboo and not correct:
        return None
    if taboo and correct:
        title, content = "禁忌表述纠正", f"用户已多次纠正：应表述为「{correct}」，避免说「{taboo}」"
    elif taboo:
        title, content = "禁忌表述纠正", f"用户已多次纠正：避免说「{taboo}」"
    else:
        title, content = "禁忌表述纠正", f"用户已多次纠正：应表述为「{correct}」"
    keywords = [t for t in (taboo, correct) if t]
    return {"card_type": "taboo_expression", "card_key": f"taboo:{_norm_key(taboo or correct)}",
            "title": title, "content": content, "keywords": keywords}


def extract_style(comment: str) -> Optional[Dict[str, Any]]:
    """从评论中提取回答详略偏好卡片（无匹配返回 None）。"""
    text = str(comment or "")
    if any(c in text for c in _CONCISE_CUES):
        return {"card_type": "answer_style", "card_key": "style:concise",
                "title": "回答详略偏好", "content": "用户偏好简洁要点式回答，避免大段铺陈",
                "keywords": []}
    if any(c in text for c in _DETAIL_CUES):
        return {"card_type": "answer_style", "card_key": "style:detailed",
                "title": "回答详略偏好", "content": "用户偏好详细分步回答，需展开依据与步骤",
                "keywords": []}
    return None


def extract_devices(text: str) -> List[Dict[str, Any]]:
    """提取常问设备卡片：位号 + 设备名词，去重；标准号前缀（GB/API 等）排除。"""
    text = str(text or "")
    cards, seen = [], set()
    for code in _DEVICE_CODE_RE.findall(text):
        if "-" not in code and "_" not in code:
            prefix_m = re.match(r"[A-Z]+", code)
            if prefix_m and prefix_m.group(0) in _STANDARD_PREFIXES:
                continue   # GB50160/API2023 是标准号，不是设备位号
        key = _norm_key(code)
        if key not in seen:
            seen.add(key)
            cards.append({"card_type": "device_type", "card_key": f"device:{key}",
                          "title": f"常问设备：{code.upper()}",
                          "content": f"用户常问设备 {code.upper()}，回答优先结合该设备的巡检/维保要点",
                          "keywords": [code.upper()]})
    for noun in _DEVICE_NOUNS:
        if noun in text and _norm_key(noun) not in seen:
            seen.add(_norm_key(noun))
            cards.append({"card_type": "device_type", "card_key": f"device:{_norm_key(noun)}",
                          "title": f"常问设备：{noun}",
                          "content": f"用户常问{noun}类设备问题，回答优先结合该类设备的风险与巡检要点",
                          "keywords": [noun]})
    return cards


def extract_sop_topics(text: str) -> List[Dict[str, Any]]:
    """提取高频 SOP 类别卡片。"""
    text = str(text or "")
    cards, seen = [], set()
    for topic in _SOP_TOPICS:
        if topic in text and topic not in seen:
            seen.add(topic)
            cards.append({"card_type": "sop_category", "card_key": f"sop:{_norm_key(topic)}",
                          "title": f"高频主题：{topic}",
                          "content": f"用户高频咨询「{topic}」相关作业/管理要求，回答可直接给出该类 SOP 关键步骤",
                          "keywords": [topic]})
    return cards


_DDL = """
CREATE TABLE IF NOT EXISTS preference_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    card_key TEXT NOT NULL,
    card_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    keywords TEXT DEFAULT '[]',
    hit_count INTEGER DEFAULT 1,
    source TEXT DEFAULT '',
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pref_user_key ON preference_cards(user_id, card_key);
CREATE INDEX IF NOT EXISTS idx_pref_user ON preference_cards(user_id);
"""

_UPSERT_SQL = """
INSERT INTO preference_cards
    (user_id, card_key, card_type, title, content, keywords, hit_count, source,
     created_ts, updated_ts)
VALUES (?,?,?,?,?,?,1,?,?,?)
ON CONFLICT(user_id, card_key) DO UPDATE SET
    card_type=excluded.card_type,
    title=excluded.title,
    content=excluded.content,
    keywords=excluded.keywords,
    hit_count=preference_cards.hit_count+1,
    source=excluded.source,
    updated_ts=excluded.updated_ts
"""


class PreferenceCardManager:
    """SQLite 偏好卡片存储（audit_store 同款：WAL + 线程锁 + 逐批原子提交）。"""

    MIN_HITS = 2             # 注入门槛：信号至少强化 2 次（"屡次"才成卡，单次偶发不注入）
    MAX_CARDS_PER_USER = 50  # 单用户容量上限，超出剪除排名最低（弱且旧）的卡
    INJECT_LIMIT = 4         # 注入上限（3-5 区间内取 4）

    def __init__(self, db_path: Optional[str] = None):
        self._path = db_path or os.getenv(
            "PREFERENCE_DB_PATH", os.path.join("data", "preference_cards.db"))
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(self._path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.executescript(_DDL)
            self._con.commit()

    # ── 写入（一批卡片单事务，原子落盘）──────────────────────────────────────
    def upsert_cards(self, user_id: str, cards: List[Dict[str, Any]], source: str = "") -> int:
        """插入或强化一批卡片：新卡 hit_count=1，已有卡 hit_count+1 并刷新内容。
        同批内同键去重（一次信号对同一张卡只计一次强化）。返回实际落库卡片数。"""
        if not cards:
            return 0
        merged: Dict[str, Dict[str, Any]] = {}
        for c in cards:
            key = f"{c.get('card_type', '')}:{c.get('card_key', '')}"
            merged.setdefault(key, c)
        now = time.time()
        with self._lock:
            for c in merged.values():
                self._con.execute(_UPSERT_SQL, (
                    user_id, str(c.get("card_key", "")), str(c.get("card_type", "")),
                    str(c.get("title", ""))[:60], str(c.get("content", ""))[:200],
                    json.dumps([str(k)[:20] for k in (c.get("keywords") or [])], ensure_ascii=False),
                    source[:80], now, now))
            self._prune_locked(user_id)
            self._con.commit()
        return len(merged)

    def absorb_feedback(self, user_id: str, *, rating: str, comment: str = "",
                        message_head: str = "", answer_head: str = "",
                        request_id: str = "") -> int:
        """反馈信号 → 偏好卡片（规则抽取，无 LLM）。
        禁忌/详略来自评语句式；设备/SOP 统计来自用户消息（👎👍 都算"常问"证据）。"""
        user_id = str(user_id or "anonymous")
        source = f"feedback:{request_id}"[:80]
        cards: List[Dict[str, Any]] = []
        taboo = extract_taboo(comment)
        if taboo:
            cards.append(taboo)
        style = extract_style(comment)
        if style:
            cards.append(style)
        cards.extend(extract_devices(message_head))
        cards.extend(extract_sop_topics(message_head))
        return self.upsert_cards(user_id, cards, source)

    def absorb_conversation(self, user_id: str, dialog_text: str, source: str = "rule:dialog") -> int:
        """对话原文 → 规则抽取（实体/关键词统计）。LLM 画像失败时的降级路径。"""
        cards = extract_devices(dialog_text) + extract_sop_topics(dialog_text)
        taboo = extract_taboo(dialog_text)
        if taboo:
            cards.append(taboo)
        return self.upsert_cards(user_id, cards, source)

    def absorb_profile(self, user_id: str, profile: Dict[str, Any], source: str = "profile") -> int:
        """画像更新 → 偏好卡片：岗位/区域 + 实体统计（设备编号/化学品/风险类型）。"""
        profile = profile or {}
        cards: List[Dict[str, Any]] = []
        duty, site = str(profile.get("duty", "")).strip(), str(profile.get("site", "")).strip()
        if duty or site:
            label = "·".join(x for x in (duty, site) if x)
            cards.append({"card_type": "role_site", "card_key": f"role:{_norm_key(label)}",
                          "title": "岗位与常涉区域", "content": f"用户岗位/区域：{label}，回答贴合该岗位视角",
                          "keywords": [x for x in (duty, site) if x][:4]})
        entities = profile.get("entities") or {}
        # key 前缀与 extract_devices 等规则抽取保持一致：同一设备/化学品无论来自
        # 反馈还是画像，都强化同一张卡（device:r-101 而非 device_type:r-101）
        type_map = {"设备编号": ("device", "device_type", "常问设备"),
                    "化学品": ("chemical", "chemical", "高频化学品"),
                    "风险类型": ("risk", "risk", "关注风险")}
        for group, (key_prefix, ctype, label) in type_map.items():
            for item in (entities.get(group) or [])[:8]:
                item = str(item).strip()
                if not item:
                    continue
                cards.append({"card_type": ctype, "card_key": f"{key_prefix}:{_norm_key(item)}",
                              "title": f"{label}：{item}",
                              "content": f"画像显示用户常涉{label}「{item}」，回答优先关联",
                              "keywords": [item]})
        return self.upsert_cards(user_id, cards, source)

    # ── 读取 ─────────────────────────────────────────────────────────────────
    def rank_for(self, user_id: str, query: str = "",
                 limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """按 命中度+强度+新鲜度 排序返回待注入卡片（默认 INJECT_LIMIT，硬上限 5）。
        只返回 hit_count >= MIN_HITS 的卡（屡次信号才注入）。"""
        limit = min(limit or self.INJECT_LIMIT, 5)
        try:
            kws = re.sub(r"\s+", "", str(query or ""))
        except Exception:
            kws = ""
        now = time.time()
        cards = self.list_cards(user_id)
        eligible = [c for c in cards if c["hit_count"] >= self.MIN_HITS]
        for c in eligible:
            c["score"] = self._score(c, kws, now)
        eligible.sort(key=lambda c: (-c["score"], -c["updated_ts"]))
        return eligible[:limit]

    def list_cards(self, user_id: str) -> List[Dict[str, Any]]:
        """该用户全部卡片（按强度、新近排序），供管理/测试/容量校验。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM preference_cards WHERE user_id=?"
                " ORDER BY hit_count DESC, updated_ts DESC", (user_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["keywords"] = json.loads(d.get("keywords") or "[]")
            except (ValueError, TypeError):
                d["keywords"] = []
            out.append(d)
        return out

    def stats(self) -> Dict[str, Any]:
        """全库概览（管理端/自检用）。"""
        with self._lock:
            q = lambda sql: self._con.execute(sql).fetchone()[0]  # noqa: E731
            return {"cards_total": q("SELECT COUNT(*) FROM preference_cards"),
                    "users_total": q("SELECT COUNT(DISTINCT user_id) FROM preference_cards")}

    @classmethod
    def _score(cls, card: Dict[str, Any], query_compact: str, now: float) -> float:
        """命中度(2.0) + 强度(1.0) + 新鲜度(0.5)，两周线性衰减。确定性纯函数。"""
        kws = card.get("keywords") or []
        hit = sum(1 for k in kws if k and k in query_compact) if query_compact else 0
        hit_ratio = hit / len(kws) if kws else 0.0
        strength = min(card.get("hit_count", 1), 10) / 10.0
        age_days = max(0.0, (now - card.get("updated_ts", now)) / 86400)
        recency = max(0.0, 1.0 - age_days / 14.0)
        return round(2.0 * hit_ratio + 1.0 * strength + 0.5 * recency, 4)

    def _prune_locked(self, user_id: str) -> int:
        """容量上限：按 (hit_count, updated_ts) 保留最强的 MAX_CARDS_PER_USER 张。
        调用方必须持有 self._lock 且随后 commit。"""
        count = self._con.execute(
            "SELECT COUNT(*) FROM preference_cards WHERE user_id=?", (user_id,)).fetchone()[0]
        if count <= self.MAX_CARDS_PER_USER:
            return 0
        rows = self._con.execute(
            "SELECT id FROM preference_cards WHERE user_id=?"
            " ORDER BY hit_count DESC, updated_ts DESC LIMIT -1 OFFSET ?",
            (user_id, self.MAX_CARDS_PER_USER)).fetchall()
        self._con.executemany("DELETE FROM preference_cards WHERE id=?", [(r[0],) for r in rows])
        return len(rows)

    def close(self) -> None:
        with self._lock:
            self._con.close()
