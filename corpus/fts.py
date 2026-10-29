"""K06 全量词面检索：FTS5 + 中文预分词（jieba）+ BM25 + 标准号/CAS 精确字段。

设计依据：方案 §5.2 —— FTS5 采用中文预分词 + BM25；标准号、CAS 等独立精确字段；
初始各路前 20 候选、RRF 常数 60、去重后至多 32 正文块轻量复排。

为什么必须预分词：**unicode61 不是中文分词器**（它按空白/标点切分，中文连写句会整段
成为一个 token，二字词如"动火"无法命中）；**trigram 也不能独立解决中文二字词的边界**
（trigram 的 3 字窗口对 2 字词只能靠 LIKE 前缀扫，且不支持 BM25 词频排序）。
故写入与查询两端都做 `jieba.cut_for_search` 预分词，词与词之间以空格分隔后交给 unicode61，
使 FTS5 在"词"粒度上建索引与算 BM25。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# 存储收敛（2026-10 存储减法）：FTS 不再有独立文件，直接落 `manifest.sqlite` 同库。
#   与 manifest 六张表（sources/assets/versions/blocks/templates/entry_asset_mapping）
#   同库共存，靠 `corpus_fts_*` 前缀避免同名冲突；多个 build 的 FTS 数据按 build_id 列区分。
#   旧实现遗留的 indexes/<build_id>/fts.sqlite 由 migrate_fts_into_manifest() 一次性并入
#   （旧文件保留为 *.legacy 备份，不删除）。

# WAL + busy_timeout：manifest 与 fts 各自持有连接、并发写同一 SQLite 文件时的必设项。
#   Python sqlite3 默认 timeout=5s（等价 busy_timeout=5000）已作用于 manifest 连接；
#   此处对 FTS 连接显式声明，两端都不会因写锁竞争立即抛 "database is locked"。
BUSY_TIMEOUT_MS = 30000

# 检索预算（方案 §5.2）
CANDIDATES_PER_CHANNEL = 20   # 各路初始候选
RRF_K = 60                    # RRF 常数
MAX_FUSED_BLOCKS = 32         # 去重后至多 32 正文块

# 标准号 / CAS 抽取（精确字段）
_STD_PREFIX = r"(?:GB|GBZ|AQ|JB|HG|SY|DL|TSG|GA|MT|YY|QB|JGJ|CJJ|DB)"
_STD_RE = re.compile(
    _STD_PREFIX + r"\s*/?\s*T?\s*\d{1,7}(?:\.\d+)?\s*[-—]\s*\d{4}", re.IGNORECASE)
_CAS_RE = re.compile(r"(?<![\d-])\d{2,7}-\d{2}-\d(?![\d-])")
_KEEP_RE = re.compile(r"[0-9A-Za-z\u3400-\u9fff]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus_fts_content(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  build_id TEXT NOT NULL DEFAULT '',
  block_id TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  tokenized TEXT NOT NULL DEFAULT '',
  locator TEXT NOT NULL DEFAULT '{}',
  evidence_kind TEXT NOT NULL DEFAULT 'body',
  kind TEXT NOT NULL DEFAULT 'text',
  token_count INTEGER NOT NULL DEFAULT 0,
  standard_no TEXT NOT NULL DEFAULT '',
  cas TEXT NOT NULL DEFAULT '',
  UNIQUE(build_id, block_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts_index USING fts5(
  tokenized, content='corpus_fts_content', content_rowid='id', tokenize='unicode61'
);
CREATE INDEX IF NOT EXISTS idx_corpus_fts_build ON corpus_fts_content(build_id);
CREATE INDEX IF NOT EXISTS idx_corpus_fts_version ON corpus_fts_content(version_id);
CREATE INDEX IF NOT EXISTS idx_corpus_fts_std ON corpus_fts_content(standard_no);
CREATE INDEX IF NOT EXISTS idx_corpus_fts_cas ON corpus_fts_content(cas);
"""

_JIEBA_STATE: Dict[str, Any] = {}


def _jieba():
    """惰性加载 jieba；缓存文件强制落 D 盘（铁律 7：一切缓存只进 D 盘）。"""
    if "mod" not in _JIEBA_STATE:
        import tempfile
        import jieba
        if Path(tempfile.gettempdir()).drive.upper() == "C:":
            # jieba 默认把词典缓存写系统临时目录（本机在 C 盘）；显式改到 D 盘
            cache_dir = os.getenv("SAFETYMIND_JIEBA_CACHE_DIR", "D:/SafetyMindData/jieba_cache")
            try:
                os.makedirs(cache_dir, exist_ok=True)
                jieba.dt.cache_file = os.path.join(cache_dir, "jieba.cache")
            except Exception:
                pass
        _JIEBA_STATE["mod"] = jieba
    return _JIEBA_STATE["mod"]


def norm_standard(value: str) -> str:
    """标准号归一：去空白、统一连字符、大写。"""
    return re.sub(r"\s+", "", (value or "").replace("—", "-").replace("－", "-")).upper()


def norm_cas(value: str) -> str:
    """CAS 号归一：去空白与连字符两侧空格。"""
    return re.sub(r"\s+", "", value or "")


def extract_standard_nos(text: str) -> List[str]:
    """抽取 GB/T 12345-2010 形式的标准号（原样匹配串）。"""
    return [m.group(0).strip() for m in _STD_RE.finditer(text or "")]


def extract_cas(text: str) -> List[str]:
    """抽取 64-17-5 形式的 CAS 号。"""
    return _CAS_RE.findall(text or "")


def _std_field(text: str) -> str:
    vals = sorted({norm_standard(v) for v in extract_standard_nos(text)})
    return "|".join(vals)


def _cas_field(text: str) -> str:
    vals = sorted({norm_cas(v) for v in extract_cas(text)})
    return "|".join(vals)


def tokenize_text(text: str) -> str:
    """写入/查询共用的分词：jieba 预分词 + 标准号/CAS 的归一化补充 token。

    标准号与 CAS 的补充 token 同时加入字母/数字片段，使"GB/T 30871-2022"这类
    带标点的编号在查询与索引两端可对齐（unicode61 会把标点当分隔符）。
    """
    parts = [t for t in _jieba().cut_for_search(text or "") if _KEEP_RE.search(t)]
    extra: List[str] = []
    for s in extract_standard_nos(text):
        n = norm_standard(s)
        extra.append(n)
        extra.extend(re.findall(r"[A-Za-z]+|\d+", n))
        extra.extend(re.findall(r"[A-Za-z]|\d+", n))
    for c in extract_cas(text):
        n = norm_cas(c)
        extra.append(n)
        extra.extend(re.findall(r"\d+", n))
    parts.extend(e for e in extra if e)
    return " ".join(parts)


def build_match_query(query: str) -> str:
    """把查询词切成 FTS5 MATCH 表达式（词级 OR，BM25 按命中词加权排序）。"""
    tokens = tokenize_text(query).split()
    if not tokens:
        return ""
    return " OR ".join('"%s"' % t.replace('"', '""') for t in tokens)


def rrf_fuse(rankings: Sequence[Sequence[Any]], k: int = RRF_K,
             limit: int = MAX_FUSED_BLOCKS) -> List[str]:
    """RRF 融合多路排名（FTS / 目录卡 / 向量），返回去重后截断的 block_id 列表。

    score(d) = Σ 1/(k + rank_i(d))；同分按首次出现顺序稳定排序。
    """
    scores: Dict[str, float] = {}
    first_seen: Dict[str, int] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking or [], start=1):
            bid = item.get("block_id") if isinstance(item, Mapping) else str(item)
            if not bid:
                continue
            scores[bid] = scores.get(bid, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(bid, len(first_seen))
    fused = sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
    return [bid for bid, _ in fused[:limit]]


def _ch_content(chunk: Any) -> str:
    if isinstance(chunk, Mapping):
        return str(chunk.get("content", "") or "")
    return str(getattr(chunk, "content", "") or "")


def _ch_attr(chunk: Any, name: str, default: Any) -> Any:
    if isinstance(chunk, Mapping):
        return chunk.get(name, default)
    return getattr(chunk, name, default)


def _block_id(version_id: str, index: int, content: str) -> str:
    """稳定 ID：同 version + 同序号 + 同正文 → 同 ID（重复导入幂等）。"""
    raw = f"{version_id}|{index}|{content}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _default_db_path() -> Path:
    """缺省落 manifest 同库（存储收敛：FTS 与 manifest 共用一个 SQLite 文件）。"""
    from corpus.paths import manifest_db
    return manifest_db()


def _row_to_item(row: sqlite3.Row, score: Optional[float] = None) -> Dict[str, Any]:
    try:
        locator = json.loads(row["locator"] or "{}")
    except Exception:
        locator = {}
    item = {
        "block_id": row["block_id"],
        "version_id": row["version_id"],
        "content": row["content"],
        "locator": locator,
        "evidence_kind": row["evidence_kind"],
        "kind": row["kind"],
        "token_count": row["token_count"],
        "standard_no": row["standard_no"],
        "cas": row["cas"],
    }
    if score is not None:
        item["score"] = score
    return item


class CorpusFTS:
    """FTS5 中文词面索引：外部内容表 corpus_fts_content + 虚表 corpus_fts_index（BM25 排序）。

    存储位置：缺省即 `corpus.paths.manifest_db()`（与 manifest 同一个 SQLite 文件）。
    `corpus_fts_content` 带 build_id 列，多个 build 的数据在同一张表里按 build_id 区分；
    唯一键为 (build_id, block_id)，故同一 version 在两个 build 下各留一份、互不覆盖。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：检索可能在线程池中执行（与 Chroma 检索同形）
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                                     timeout=BUSY_TIMEOUT_MS / 1000.0)
        self._conn.row_factory = sqlite3.Row
        # WAL + busy_timeout：与 manifest 连接并发写同一文件时不互锁（短事务即可）
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_MS)}")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    # ── 写入 ────────────────────────────────────────────────────────────────
    def _upsert_locked(self, build_id: str, version_id: str, block_id: str, content: str,
                       tokenized: str, locator_text: str, evidence_kind: str, kind: str,
                       token_count: int, std: str, cas: str) -> bool:
        """在已持锁/已开启的事务内 upsert 一行块，返回是否新增。

        外部内容表：更新前必须先向 FTS 虚表报 delete，否则索引残留旧词。
        """
        old = self._conn.execute(
            "SELECT id, tokenized FROM corpus_fts_content WHERE build_id=? AND block_id=?",
            (build_id, block_id)).fetchone()
        if old is not None:
            self._conn.execute(
                "INSERT INTO corpus_fts_index(corpus_fts_index, rowid, tokenized) VALUES('delete', ?, ?)",
                (old["id"], old["tokenized"]))
            self._conn.execute(
                "UPDATE corpus_fts_content SET version_id=?, content=?, tokenized=?, locator=?, "
                "evidence_kind=?, kind=?, token_count=?, standard_no=?, cas=? "
                "WHERE build_id=? AND block_id=?",
                (version_id, content, tokenized, locator_text, evidence_kind, kind, token_count,
                 std, cas, build_id, block_id))
            rowid = old["id"]
            added = False
        else:
            cur = self._conn.execute(
                "INSERT INTO corpus_fts_content(build_id, block_id, version_id, content, tokenized, "
                "locator, evidence_kind, kind, token_count, standard_no, cas) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (build_id, block_id, version_id, content, tokenized, locator_text, evidence_kind,
                 kind, token_count, std, cas))
            rowid = cur.lastrowid
            added = True
        self._conn.execute(
            "INSERT INTO corpus_fts_index(rowid, tokenized) VALUES(?, ?)", (rowid, tokenized))
        return added

    def add_blocks(self, version_id: str, chunks: Iterable[Any], build_id: str = "") -> int:
        """写入切块（幂等：同 build + 同 version + 同序号 + 同正文 → 同 block_id，覆盖而非新增）。

        build_id 区分同一 SQLite 文件里的多个 build；缺省 "" 表示未按 build 归属（单元测试、
        旧数据迁移前的默认归属）。返回新增（此前不存在）的块数。
        """
        build_id = str(build_id or "")
        added = 0
        with self._lock, self._conn:
            for idx, chunk in enumerate(chunks):
                content = _ch_content(chunk)
                locator = _ch_attr(chunk, "locator", {}) or {}
                evidence_kind = str(_ch_attr(chunk, "evidence_kind", "body") or "body")
                kind = str(_ch_attr(chunk, "kind", "text") or "text")
                token_count = int(_ch_attr(chunk, "token_count", 0) or 0)
                block_id = _block_id(version_id, idx, content)
                tokenized = tokenize_text(content)
                if self._upsert_locked(build_id, version_id, block_id, content, tokenized,
                                       json.dumps(locator, ensure_ascii=False), evidence_kind,
                                       kind, token_count, _std_field(content), _cas_field(content)):
                    added += 1
        return added

    # ── 检索 ────────────────────────────────────────────────────────────────
    def search(self, query: str, limit: int = CANDIDATES_PER_CHANNEL,
               build_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """中文词面检索（BM25 排序；score 越小越相关，与 FTS5 约定一致）。

        build_id 非 None 时只召回该 build 的块（CorpusSearch 按 ACTIVE build 过滤）。
        """
        match = build_match_query(query)
        if not match:
            return []
        extra, extra_args = self._build_filter(build_id)
        sql = ("SELECT c.block_id, c.version_id, c.content, c.locator, c.evidence_kind, "
               "c.kind, c.token_count, c.standard_no, c.cas, bm25(corpus_fts_index) AS score "
               "FROM corpus_fts_index JOIN corpus_fts_content c ON c.id = corpus_fts_index.rowid "
               f"WHERE corpus_fts_index MATCH ?{extra} ORDER BY score LIMIT ?")
        args = [match, *extra_args, int(limit)]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_item(r, score=r["score"]) for r in rows]

    def search_exact(self, kind: str, value: str,
                     build_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """标准号 / CAS 精确等值查询（独立字段，非分词近似）。"""
        key = (kind or "").strip().lower()
        if key in ("standard", "standard_no", "std", "标准号"):
            column, norm = "standard_no", norm_standard(value)
        elif key in ("cas", "cas_no", "cas号"):
            column, norm = "cas", norm_cas(value)
        else:
            raise ValueError(f"search_exact 只支持 kind='standard' 或 'cas'，收到 {kind!r}")
        if not norm:
            return []
        extra, extra_args = self._build_filter(build_id)
        sql = (f"SELECT block_id, version_id, content, locator, evidence_kind, kind, token_count, "
               f"standard_no, cas FROM corpus_fts_content "
               f"WHERE instr('|' || {column} || '|', '|' || ? || '|') > 0{extra} LIMIT 200")
        args = [norm, *extra_args]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_item(r) for r in rows]

    @staticmethod
    def _build_filter(build_id: Optional[str]) -> "tuple[str, List[Any]]":
        """build_id 过滤片段：None → 不过滤（旧调用兼容）；否则限定该 build。"""
        if build_id is None:
            return "", []
        return " AND build_id = ?", [str(build_id)]

    def stats(self, build_id: Optional[str] = None) -> Dict[str, Any]:
        extra, extra_args = self._build_filter(build_id)
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM corpus_fts_content WHERE 1=1{extra}", extra_args).fetchone()[0]
            versions = self._conn.execute(
                f"SELECT COUNT(DISTINCT version_id) FROM corpus_fts_content WHERE 1=1{extra}",
                extra_args).fetchone()[0]
            stds = self._conn.execute(
                f"SELECT COUNT(*) FROM corpus_fts_content WHERE standard_no <> ''{extra}",
                extra_args).fetchone()[0]
            cas = self._conn.execute(
                f"SELECT COUNT(*) FROM corpus_fts_content WHERE cas <> ''{extra}",
                extra_args).fetchone()[0]
        return {"blocks": total, "versions": versions, "standards": stds, "cas": cas,
                "db": str(self.db_path), "build_id": build_id, "rrf_k": RRF_K,
                "candidates_per_channel": CANDIDATES_PER_CHANNEL,
                "max_fused_blocks": MAX_FUSED_BLOCKS}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── 旧独立 FTS 文件的一次性迁移（存储收敛）──────────────────────────────────

def legacy_fts_path(build_id: str, root: Optional[Any] = None) -> Path:
    """旧实现遗留的 indexes/<build_id>/fts.sqlite 路径（迁移前存在，迁移后保留为 .legacy）。"""
    from corpus import paths
    return paths.build_dir(build_id, Path(root).resolve() if root else None) / "fts.sqlite"


def pending_fts_migrations(root: Optional[Any] = None) -> List[str]:
    """列出仍带旧独立 fts.sqlite 的 build_id（供 /corpus/status 报告迁移状态）。"""
    from corpus import paths
    base = paths.subdir("indexes", Path(root).resolve() if root else None)
    out: List[str] = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if d.is_dir() and (d / "fts.sqlite").exists():
            out.append(d.name)
    return out


def migrate_fts_into_manifest(build_id: str, root: Optional[Any] = None) -> Dict[str, Any]:
    """一次性把旧 indexes/<build_id>/fts.sqlite 的内容并入 manifest 同库。

    - 旧的外部内容表 `blocks` 逐行复制到 `corpus_fts_content`（build_id=该 build）；
    - 旧文件**不删除**，改名为 `fts.sqlite.legacy` 作备份（保留可追溯）；
    - 幂等：重复调用不重复导入（按 (build_id, block_id) upsert）。
    """
    import shutil

    legacy = legacy_fts_path(build_id, root)
    result: Dict[str, Any] = {"build_id": str(build_id), "legacy_file": legacy.as_posix(),
                              "migrated": False, "copied": 0, "backup": ""}
    if not legacy.exists():
        result["reason"] = "无旧独立 fts 文件，无需迁移"
        return result

    fts = CorpusFTS()  # 缺省即 manifest 同库（并顺带建好 corpus_fts_* 表）
    copied = 0
    try:
        con = sqlite3.connect(str(legacy))
        con.row_factory = sqlite3.Row
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            rows = []
            if "blocks" in names:
                rows = con.execute(
                    "SELECT block_id, version_id, content, tokenized, locator, evidence_kind, "
                    "kind, token_count, standard_no, cas FROM blocks").fetchall()
        finally:
            con.close()

        with fts._lock, fts._conn:
            for r in rows:
                tokenized = str(r["tokenized"] or tokenize_text(r["content"] or ""))
                if fts._upsert_locked(str(build_id), str(r["version_id"] or ""),
                                      str(r["block_id"] or ""), str(r["content"] or ""),
                                      tokenized, str(r["locator"] or "{}"),
                                      str(r["evidence_kind"] or "body"),
                                      str(r["kind"] or "text"), int(r["token_count"] or 0),
                                      str(r["standard_no"] or ""), str(r["cas"] or "")):
                    copied += 1
    finally:
        fts.close()

    backup = legacy.parent / (legacy.name + ".legacy")
    try:
        if backup.exists():
            backup.unlink()
        shutil.move(str(legacy), str(backup))
    except OSError:
        backup = ""  # 句柄未释放时如实回退：数据已并入，旧文件留在原位
    result.update({"migrated": True, "copied": copied,
                   "backup": backup.as_posix() if backup else "", "reason": ""})
    return result
