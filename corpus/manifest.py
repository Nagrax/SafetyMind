# -*- coding: utf-8 -*-
"""资料库 manifest：来源/资产/版本/正文块/模板契约 + 状态机 + 去重与断点（K02）。

方案 §5「最低契约」与 §9「状态与对账」的代码化落地，全部落在单个
`corpus.paths.manifest_db()` 指向的 `manifest.sqlite`：

  四张主表：
    sources              来源条目层（provider/item_id/link_ref/exported_at/... 含 unknown 保留）
    assets               资产层（内容寻址：asset_id == sha256，原件落 originals/sha256/<前2位>/<sha256>）
    versions             逻辑文档/版本层（logical_doc_id + version_id + status 枚举）
    blocks               正文层（block_id/version_id/content/locator/token_count/evidence_kind）
  两张附着表：
    templates            模板层（template_id/version_id/schema/asset_id）
    entry_asset_mapping  条目→分享→实际文件映射（item_id/share_id/share_file_id/asset_id/mapping_status）

稳定 ID（方案 §5：文档 ID 不能只按 title 生成）：
    logical_doc_id = sha1(provider|item_id)；无 item_id 时 = sha1(provider|title|version_key)
    version_id     = sha1(logical_doc_id|version_key)
    asset_id       = sha256(文件内容)
  → 同名不同源/不同版共存；同内容可共享物理文件，但来源谱系与采集分母不合并抹去
    （同一 asset 可被多条 sources/versions 引用）。

状态机（分开记录，不用一个 done 掩盖缺口）：
    acquisition_status：catalog_registered/pending/acquired_verified/duplicate_confirmed/
                        irrelevant_excluded/invalid_access_blocked/capacity_blocked/
                        parse_failed/parsed
    parse_status      ：not_started/parsed/low_quality/unparseable/unsupported/pending_conversion
    ladder（链路台阶）：link_exported→share_accessible→file_downloaded_verified→body_parsed，
                        只有拿到实际文件并校验才可记 file_downloaded_verified；
                        对账中「已取得文件」只按 file_downloaded_verified 计，杜绝「有链接冒充已取得正文」。
    索引台阶         ：fts_indexed / catalog_card / body_vectorized（时间戳，NULL=未完成）。

写操作全部走 self._lock；枚举非法值一律 ValueError（fail-loud）。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from corpus import paths

# ── 枚举常量化 ────────────────────────────────────────────────────────────────
SOURCES = ("user_imported", "toolbox_catalog", "toolbox_acquired", "other_verified_source")
ACQUISITION_STATUSES = (
    "catalog_registered", "pending", "acquired_verified", "duplicate_confirmed",
    "irrelevant_excluded", "invalid_access_blocked", "capacity_blocked",
    "parse_failed", "parsed",
)
PARSE_STATUSES = (
    "not_started", "parsed", "low_quality", "unparseable", "unsupported", "pending_conversion",
)
LADDER_STEPS = ("link_exported", "share_accessible", "file_downloaded_verified", "body_parsed")
MAPPING_STATUSES = ("mapped", "duplicate", "missing", "invalid", "pending")
EVIDENCE_KINDS = ("body", "catalog")
VERSION_STATUSES = ("active", "soon", "superseded", "repealed", "unknown")
INDEX_FLAGS = ("fts_indexed", "catalog_card", "body_vectorized")
RECON_SCOPES = ("catalog_only", "files")

# 采集分母分桶（方案 §9：预期叶文件 = 已校验 + 重复映射 + 有依据排除 + 待办 + 具体阻塞）
_ACQUISITION_BUCKETS: Dict[str, str] = {
    "catalog_registered": "todo",
    "pending": "todo",
    "acquired_verified": "verified",
    "parsed": "verified",
    "duplicate_confirmed": "duplicate_mapping",
    "irrelevant_excluded": "excluded_with_reason",
    "invalid_access_blocked": "blocked",
    "capacity_blocked": "blocked",
    "parse_failed": "blocked",
}
assert set(_ACQUISITION_BUCKETS) == set(ACQUISITION_STATUSES), "采集分桶必须覆盖全部 acquisition_status"

# 受控引用禁写的凭据特征（不把 cookie/token/提取码写进库）
_SECRET_RE = re.compile(
    r"(?i)(cookie|token|passwd|password|pwd|secret|signature|sign|access[_-]?key|api[_-]?key)"
    r"\s*[:=]|提取码|访问码")

_SOURCE_FIELDS = (
    "title", "category", "updated_at", "summary", "netdisk_provider", "link_ref",
    "exported_at", "raw", "acquisition_status", "parse_status", "ladder",
)
_VERSION_FIELDS = ("category", "industry", "region", "status", "effective_from",
                   "effective_to", "access_scope")

# ── 表结构（建表与非破坏迁移共用的唯一来源）──────────────────────────────────
_TABLES: Dict[str, Dict[str, Any]] = {
    "sources": {
        "cols": [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("provider", "TEXT NOT NULL DEFAULT ''"),
            ("item_id", "TEXT NOT NULL DEFAULT ''"),
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("category", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ("summary", "TEXT NOT NULL DEFAULT ''"),
            ("netdisk_provider", "TEXT NOT NULL DEFAULT ''"),
            ("link_ref", "TEXT NOT NULL DEFAULT ''"),
            ("exported_at", "TEXT NOT NULL DEFAULT ''"),
            ("raw", "TEXT NOT NULL DEFAULT ''"),
            ("unknown_fields", "TEXT NOT NULL DEFAULT '{}'"),
            ("acquisition_status", "TEXT NOT NULL DEFAULT 'catalog_registered'"),
            ("parse_status", "TEXT NOT NULL DEFAULT 'not_started'"),
            ("ladder", "TEXT NOT NULL DEFAULT '[]'"),
            ("fts_indexed", "REAL"),
            ("catalog_card", "REAL"),
            ("body_vectorized", "REAL"),
        ],
        "constraints": [],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_sources_provider ON sources(provider)",
            "CREATE INDEX IF NOT EXISTS idx_sources_item ON sources(item_id)",
            "CREATE INDEX IF NOT EXISTS idx_sources_acq ON sources(acquisition_status)",
        ],
    },
    "assets": {
        "cols": [
            ("asset_id", "TEXT PRIMARY KEY"),
            ("sha256", "TEXT NOT NULL DEFAULT ''"),
            ("mime", "TEXT NOT NULL DEFAULT ''"),
            ("bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("path", "TEXT NOT NULL DEFAULT ''"),
            ("acquisition_status", "TEXT NOT NULL DEFAULT 'acquired_verified'"),
            ("parse_status", "TEXT NOT NULL DEFAULT 'not_started'"),
            ("ladder", "TEXT NOT NULL DEFAULT '[]'"),
            ("fts_indexed", "REAL"),
            ("catalog_card", "REAL"),
            ("body_vectorized", "REAL"),
            ("updated_at", "REAL"),
        ],
        "constraints": [],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_assets_sha ON assets(sha256)",
            "CREATE INDEX IF NOT EXISTS idx_assets_acq ON assets(acquisition_status)",
        ],
    },
    "versions": {
        "cols": [
            ("version_id", "TEXT PRIMARY KEY"),
            ("logical_doc_id", "TEXT NOT NULL DEFAULT ''"),
            ("source_id", "INTEGER"),
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("category", "TEXT NOT NULL DEFAULT ''"),
            ("industry", "TEXT NOT NULL DEFAULT ''"),
            ("region", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("effective_from", "TEXT NOT NULL DEFAULT ''"),
            ("effective_to", "TEXT NOT NULL DEFAULT ''"),
            # K03/K07 访问范围：空串 = 公共；多用户形态下检索按此过滤（fail-closed）
            ("access_scope", "TEXT NOT NULL DEFAULT ''"),
            ("asset_id", "TEXT NOT NULL DEFAULT ''"),
            ("unknown_fields", "TEXT NOT NULL DEFAULT '{}'"),
            ("updated_at", "REAL"),
        ],
        "constraints": [],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_versions_logical ON versions(logical_doc_id)",
            "CREATE INDEX IF NOT EXISTS idx_versions_source ON versions(source_id)",
            "CREATE INDEX IF NOT EXISTS idx_versions_status ON versions(status)",
        ],
    },
    "blocks": {
        "cols": [
            ("block_id", "TEXT PRIMARY KEY"),
            ("version_id", "TEXT NOT NULL DEFAULT ''"),
            ("content", "TEXT NOT NULL DEFAULT ''"),
            ("locator", "TEXT NOT NULL DEFAULT ''"),
            ("token_count", "INTEGER NOT NULL DEFAULT 0"),
            ("evidence_kind", "TEXT NOT NULL DEFAULT 'body'"),
        ],
        "constraints": [],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_blocks_version ON blocks(version_id)",
            "CREATE INDEX IF NOT EXISTS idx_blocks_kind ON blocks(evidence_kind)",
        ],
    },
    "templates": {
        "cols": [
            ("template_id", "TEXT PRIMARY KEY"),
            ("version_id", "TEXT NOT NULL DEFAULT ''"),
            ("schema", "TEXT NOT NULL DEFAULT '{}'"),
            ("asset_id", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "REAL"),
        ],
        "constraints": [],
        "indexes": ["CREATE INDEX IF NOT EXISTS idx_templates_version ON templates(version_id)"],
    },
    "entry_asset_mapping": {
        "cols": [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("item_id", "TEXT NOT NULL DEFAULT ''"),
            ("share_id", "TEXT NOT NULL DEFAULT ''"),
            ("share_file_id", "TEXT NOT NULL DEFAULT ''"),
            ("asset_id", "TEXT NOT NULL DEFAULT ''"),
            ("mapping_status", "TEXT NOT NULL DEFAULT 'mapped'"),
            ("created_at", "REAL"),
        ],
        "constraints": [],
        "indexes": [
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_entry_asset ON entry_asset_mapping"
            "(item_id, share_id, share_file_id, asset_id)",
            "CREATE INDEX IF NOT EXISTS idx_mapping_item ON entry_asset_mapping(item_id)",
        ],
    },
}


def _table_statements() -> List[str]:
    """建表语句（与迁移共用同一份列定义，避免漂移）。"""
    stmts: List[str] = []
    for table, spec in _TABLES.items():
        cols = ",\n    ".join(f"{n} {t}" for n, t in spec["cols"])
        stmts.append(f"CREATE TABLE IF NOT EXISTS {table} (\n    {cols}\n);")
    return stmts


def _index_statements() -> List[str]:
    """建索引语句：必须在迁移补列之后再执行（旧库缺列时索引会失败）。

    逐条补分号：DDL 字面量里可能省略结尾分号，用 "\\n" 拼接会被 SQLite 当成
    两条语句挤在一起（`near "CREATE": syntax error`）。"""
    stmts: List[str] = []
    for spec in _TABLES.values():
        stmts.extend(spec.get("indexes", []))
    return [s if s.rstrip().endswith(";") else s.rstrip() + ";" for s in stmts]


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


class CorpusManifest:
    """资料库 manifest 数据库（SQLite 单文件 + 线程锁 + 非破坏迁移）。"""

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        self._path = Path(db_path) if db_path else paths.manifest_db()
        root = self._path.parent
        # 铁律 7：非 D 盘路径在 ensure_layout 内直接抛错（先校验再连库）
        paths.ensure_layout(root)
        os.makedirs(str(root), exist_ok=True)
        self._root = root
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            # 先建表 → 补列（非破坏迁移）→ 再建索引（旧库缺列时索引才可用）
            self._con.executescript("\n".join(_table_statements()))
            self._migrate_locked()
            self._con.executescript("\n".join(_index_statements()))
            self._con.commit()

    # ── 迁移 / 工具 ─────────────────────────────────────────────────────────
    def _migrate_locked(self) -> None:
        """非破坏迁移：PRAGMA table_info 探测缺列后 ALTER ADD COLUMN，不改写既有数据。
        只加列，不动历史行；主键列无法 ALTER 添加故跳过。"""
        for table, spec in _TABLES.items():
            existing = {r[1] for r in self._con.execute(f"PRAGMA table_info({table})")}
            for name, decl in spec["cols"]:
                if name in existing or "PRIMARY KEY" in decl.upper():
                    continue
                self._con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @staticmethod
    def _validate_enum(value: Any, allowed: Sequence[str], field: str) -> str:
        if value not in allowed:
            raise ValueError(f"非法 {field} 取值 {value!r}，只允许 {tuple(allowed)}")
        return str(value)

    @staticmethod
    def _loads(text: Any, default: Any) -> Any:
        try:
            return json.loads(text) if text else default
        except (ValueError, TypeError):
            return default

    def _guarded_link_ref(self, value: Any) -> str:
        """受控引用：禁止 cookie/token/提取码等凭据入库。"""
        text = str(value or "")
        if _SECRET_RE.search(text):
            raise ValueError("link_ref 不得携带 cookie/token/提取码等凭据，请改存受控引用")
        return text

    def _norm_ladder(self, value: Any) -> List[str]:
        steps = [value] if isinstance(value, str) else list(value or [])
        for s in steps:
            self._validate_enum(s, LADDER_STEPS, "ladder")
        return [str(s) for s in steps]

    @staticmethod
    def _locator_text(locator: Any) -> str:
        if isinstance(locator, (dict, list)):
            return json.dumps(locator, ensure_ascii=False, sort_keys=True)
        return str(locator or "")

    @staticmethod
    def _make_block_id(version_id: str, locator_text: str, content: str) -> str:
        return _sha1(f"{version_id}|{locator_text}|{content}")

    # ── 稳定 ID ────────────────────────────────────────────────────────────
    @staticmethod
    def make_logical_doc_id(provider: str, item_id: str = "",
                            title: str = "", version_key: str = "") -> str:
        """逻辑文档 ID：有 item_id 用 (provider|item_id)，否则用 (provider|title|version_key)。
        刻意不含 title-only 分支，避免「安全检查表」「应急预案」等同名不同源被合并。"""
        if item_id:
            return _sha1(f"{provider}|{item_id}")
        if not title:
            raise ValueError("make_logical_doc_id 需要 item_id 或 title 之一")
        return _sha1(f"{provider}|{title}|{version_key}")

    @staticmethod
    def make_version_id(logical_doc_id: str, version_key: str = "") -> str:
        if not logical_doc_id:
            raise ValueError("make_version_id 需要 logical_doc_id")
        return _sha1(f"{logical_doc_id}|{version_key}")

    # ── 来源条目层 ─────────────────────────────────────────────────────────
    def upsert_source(self, provider: str, item_id: str, **fields) -> int:
        """幂等写入来源条目：同 (provider,item_id) 重复调用不新增行，只更新可变字段。
        未知字段保留进 unknown_fields（方案 §5「未知字段保留unknown」）。"""
        self._validate_enum(provider, SOURCES, "provider")
        item_id = str(item_id or "")
        unknown = dict(fields.pop("unknown_fields", {}) or {})
        values: Dict[str, Any] = {}
        for key, val in fields.items():
            if key not in _SOURCE_FIELDS:
                unknown[key] = val
                continue
            if key == "raw":
                values[key] = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            elif key == "link_ref":
                values[key] = self._guarded_link_ref(val)
            elif key == "acquisition_status":
                values[key] = self._validate_enum(val, ACQUISITION_STATUSES, "acquisition_status")
            elif key == "parse_status":
                values[key] = self._validate_enum(val, PARSE_STATUSES, "parse_status")
            elif key == "ladder":
                values[key] = json.dumps(self._norm_ladder(val), ensure_ascii=False)
            else:
                values[key] = "" if val is None else str(val)
        with self._lock:
            row = None
            if item_id:
                row = self._con.execute(
                    "SELECT id, unknown_fields FROM sources WHERE provider=? AND item_id=?"
                    " ORDER BY id LIMIT 1", (provider, item_id)).fetchone()
            if row is not None:
                if unknown:
                    merged = self._loads(row["unknown_fields"], {})
                    merged.update(unknown)
                    values["unknown_fields"] = json.dumps(merged, ensure_ascii=False)
                if values:
                    sets = ", ".join(f"{k}=?" for k in values)
                    self._con.execute(f"UPDATE sources SET {sets} WHERE id=?",
                                      (*values.values(), row["id"]))
                sid = int(row["id"])
            else:
                cols = ["provider", "item_id", *values.keys(), "unknown_fields"]
                args = [provider, item_id, *values.values(),
                        json.dumps(unknown, ensure_ascii=False)]
                cur = self._con.execute(
                    f"INSERT INTO sources ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    args)
                sid = int(cur.lastrowid)
            self._con.commit()
        return sid

    # ── 资产层（内容寻址）───────────────────────────────────────────────────
    def register_asset(self, path_or_bytes: Union[str, Path, bytes, bytearray],
                       mime: str = "", acquisition_status: str = "acquired_verified") -> str:
        """登记资产：算 sha256，原件落 originals/sha256/<前2位>/<sha256>，返回 asset_id。
        同 sha256 已存在则复用（不重复落盘），仅刷新元数据。asset_id == sha256。"""
        self._validate_enum(acquisition_status, ACQUISITION_STATUSES, "acquisition_status")
        if isinstance(path_or_bytes, (bytes, bytearray)):
            data = bytes(path_or_bytes)
        else:
            src = Path(path_or_bytes)
            data = src.read_bytes()
            if not mime:
                mime = mimetypes.guess_type(str(src))[0] or ""
        sha = hashlib.sha256(data).hexdigest()
        dest = paths.subdir("originals/sha256", self._root) / sha[:2] / sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            tmp = dest.parent / (dest.name + f".tmp{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(str(tmp), str(dest))
        with self._lock:
            row = self._con.execute("SELECT asset_id, mime FROM assets WHERE asset_id=?",
                                    (sha,)).fetchone()
            if row is None:
                self._con.execute(
                    "INSERT INTO assets (asset_id, sha256, mime, bytes, path,"
                    " acquisition_status, parse_status, ladder, updated_at)"
                    " VALUES (?,?,?,?,?,?,'not_started','[]',?)",
                    (sha, sha, mime, len(data), dest.as_posix(), acquisition_status, time.time()))
            else:
                self._con.execute(
                    "UPDATE assets SET bytes=?, path=?, acquisition_status=?,"
                    " mime=CASE WHEN ?='' THEN mime ELSE ? END, updated_at=? WHERE asset_id=?",
                    (len(data), dest.as_posix(), acquisition_status, mime, mime,
                     time.time(), sha))
            self._con.commit()
        return sha

    def link_asset(self, item_id: str, asset_id: str, share_id: str = "",
                   share_file_id: str = "", mapping_status: str = "mapped") -> None:
        """条目→分享→文件映射（断点/幂等：重复调用不新增重复映射行）。"""
        self._validate_enum(mapping_status, MAPPING_STATUSES, "mapping_status")
        item_id, asset_id = str(item_id or ""), str(asset_id or "")
        if not item_id or not asset_id:
            raise ValueError("link_asset 需要 item_id 与 asset_id")
        with self._lock:
            if self._con.execute("SELECT 1 FROM assets WHERE asset_id=?", (asset_id,)).fetchone() is None:
                raise ValueError(f"未登记的 asset_id：{asset_id}")
            self._con.execute(
                "INSERT OR IGNORE INTO entry_asset_mapping (item_id, share_id, share_file_id,"
                " asset_id, mapping_status, created_at) VALUES (?,?,?,?,?,?)",
                (item_id, str(share_id or ""), str(share_file_id or ""), asset_id,
                 mapping_status, time.time()))
            self._con.commit()

    # ── 版本层 ─────────────────────────────────────────────────────────────
    def add_version(self, logical_doc_id: str, version_id: str, source_id: int, title: str,
                    asset_id: str, **fields) -> None:
        """写入/更新一个版本（按 version_id 幂等 upsert，不改变既有其他版本）。"""
        if not logical_doc_id or not version_id:
            raise ValueError("add_version 需要 logical_doc_id 与 version_id")
        unknown = dict(fields.pop("unknown_fields", {}) or {})
        values = {"status": "unknown"}
        for key, val in fields.items():
            if key not in _VERSION_FIELDS:
                unknown[key] = val
                continue
            if key == "status":
                values[key] = self._validate_enum(val, VERSION_STATUSES, "status")
            else:
                values[key] = "" if val is None else str(val)
        payload = {
            "logical_doc_id": str(logical_doc_id), "source_id": int(source_id) if source_id else None,
            "title": str(title or ""), "asset_id": str(asset_id or ""),
            "unknown_fields": json.dumps(unknown, ensure_ascii=False), "updated_at": time.time(),
            **values,
        }
        with self._lock:
            row = self._con.execute("SELECT version_id FROM versions WHERE version_id=?",
                                    (version_id,)).fetchone()
            if row is None:
                cols = ["version_id", *payload.keys()]
                self._con.execute(
                    f"INSERT INTO versions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [version_id, *payload.values()])
            else:
                sets = ", ".join(f"{k}=?" for k in payload)
                self._con.execute(f"UPDATE versions SET {sets} WHERE version_id=?",
                                  (*payload.values(), version_id))
            self._con.commit()

    # ── 正文层 ─────────────────────────────────────────────────────────────
    def add_blocks(self, version_id: str, blocks: Sequence[Mapping]) -> int:
        """批量写入正文块，返回新增数。幂等：同一 version 重复导入不产生重复 block_id
        （按 version_id+locator+内容哈希去重，block_id 作主键 INSERT OR IGNORE）。"""
        if not version_id:
            raise ValueError("add_blocks 需要 version_id")
        rows: List[Tuple[str, str, str, str, int, str]] = []
        seen = set()
        for blk in blocks:
            content = str(blk.get("content", ""))
            locator_text = self._locator_text(blk.get("locator", ""))
            kind = self._validate_enum(blk.get("evidence_kind", "body"), EVIDENCE_KINDS, "evidence_kind")
            bid = str(blk.get("block_id") or self._make_block_id(version_id, locator_text, content))
            if bid in seen:
                continue
            seen.add(bid)
            rows.append((bid, str(version_id), content, locator_text,
                         int(blk.get("token_count") or 0), kind))
        added = 0
        with self._lock:
            for r in rows:
                cur = self._con.execute(
                    "INSERT OR IGNORE INTO blocks (block_id, version_id, content, locator,"
                    " token_count, evidence_kind) VALUES (?,?,?,?,?,?)", r)
                added += max(int(cur.rowcount), 0)
            self._con.commit()
        return added

    # ── 模板层 ─────────────────────────────────────────────────────────────
    def add_template(self, version_id: str, schema: Any, asset_id: str = "",
                     template_id: str = "") -> str:
        """写入模板（字段/单位/必填/表头/说明的 schema JSON）。幂等按 template_id upsert。"""
        if not version_id:
            raise ValueError("add_template 需要 version_id")
        text = schema if isinstance(schema, str) else json.dumps(schema or {}, ensure_ascii=False)
        tid = str(template_id or _sha1(f"{version_id}|{text}"))
        with self._lock:
            row = self._con.execute("SELECT template_id FROM templates WHERE template_id=?",
                                    (tid,)).fetchone()
            if row is None:
                self._con.execute(
                    "INSERT INTO templates (template_id, version_id, schema, asset_id, updated_at)"
                    " VALUES (?,?,?,?,?)", (tid, version_id, text, str(asset_id or ""), time.time()))
            else:
                self._con.execute(
                    "UPDATE templates SET version_id=?, schema=?, asset_id=?, updated_at=?"
                    " WHERE template_id=?",
                    (version_id, text, str(asset_id or ""), time.time(), tid))
            self._con.commit()
        return tid

    # ── 状态机（来源条目或资产）────────────────────────────────────────────
    def set_status(self, item_id_or_asset: str, acquisition_status: Optional[str] = None,
                   parse_status: Optional[str] = None, ladder: Any = None,
                   provider: str = "", **index_flags) -> None:
        """状态流转：目标可为 sources.item_id 或 assets.asset_id（sha256）。
        枚举白名单校验；acquisition_status 与索引台阶互不串用（非法值直接 ValueError）。"""
        target = str(item_id_or_asset or "")
        if not target:
            raise ValueError("set_status 需要 item_id 或 asset_id")
        updates: Dict[str, Any] = {}
        if acquisition_status is not None:
            updates["acquisition_status"] = self._validate_enum(
                acquisition_status, ACQUISITION_STATUSES, "acquisition_status")
        if parse_status is not None:
            updates["parse_status"] = self._validate_enum(parse_status, PARSE_STATUSES, "parse_status")
        for key, val in index_flags.items():
            if key not in INDEX_FLAGS:
                raise ValueError(f"非法索引台阶 {key!r}，只允许 {INDEX_FLAGS}")
            updates[key] = None if val in (None, False, "", 0) else (
                float(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else time.time())
        new_steps: List[str] = []
        if ladder is not None:
            new_steps = self._norm_ladder(ladder)
        if not updates and not new_steps:
            raise ValueError("set_status 未给出任何待更新项")
        with self._lock:
            table, key_col = ("assets", "asset_id") if _is_sha256(target) else ("sources", "item_id")
            row = self._con.execute(f"SELECT * FROM {table} WHERE {key_col}=? LIMIT 1",
                                    (target,)).fetchone()
            if row is None:
                raise ValueError(f"未找到 {table}.{key_col}={target}")
            if new_steps:
                current = self._loads(row["ladder"], [])
                merged = list(current) + [s for s in new_steps if s not in current]
                updates["ladder"] = json.dumps(merged, ensure_ascii=False)
            sets = ", ".join(f"{k}=?" for k in updates)
            self._con.execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?",
                              (*updates.values(), target))
            self._con.commit()

    # ── 对账（方案 §9）────────────────────────────────────────────────────
    def reconciliation(self, provider: Optional[str] = None, scope: str = "catalog_only") -> Dict[str, Any]:
        """分母对账：expected_leaf_files = verified + duplicate_mapping
        + excluded_with_reason + todo + blocked。
        scope 如实声明口径：未取得远端完整清单时只能 catalog_only（只有目录项），
        不得声称文件全量；「已取得文件」只按 ladder 的 file_downloaded_verified 计。"""
        self._validate_enum(scope, RECON_SCOPES, "scope")
        if provider is not None:
            self._validate_enum(provider, SOURCES, "provider")
        with self._lock:
            if provider:
                rows = self._con.execute("SELECT * FROM sources WHERE provider=?",
                                         (provider,)).fetchall()
            else:
                rows = self._con.execute("SELECT * FROM sources").fetchall()
        buckets = {k: 0 for k in ("verified", "duplicate_mapping", "excluded_with_reason", "todo", "blocked")}
        by_acq = {s: 0 for s in ACQUISITION_STATUSES}
        by_parse = {s: 0 for s in PARSE_STATUSES}
        ladder_counts = {s: 0 for s in LADDER_STEPS}
        index_progress = {f: 0 for f in INDEX_FLAGS}
        for r in rows:
            acq = r["acquisition_status"] if r["acquisition_status"] in ACQUISITION_STATUSES else "catalog_registered"
            by_acq[acq] += 1
            buckets[_ACQUISITION_BUCKETS[acq]] += 1
            ps = r["parse_status"] if r["parse_status"] in PARSE_STATUSES else "not_started"
            by_parse[ps] += 1
            for step in self._loads(r["ladder"], []):
                if step in ladder_counts:
                    ladder_counts[step] += 1
            for flag in INDEX_FLAGS:
                if r[flag] is not None:
                    index_progress[flag] += 1
        expected = sum(buckets.values())
        return {
            "scope": scope,
            "provider": provider or "all",
            "expected_leaf_files": expected,
            "verified": buckets["verified"],
            "duplicate_mapping": buckets["duplicate_mapping"],
            "excluded_with_reason": buckets["excluded_with_reason"],
            "todo": buckets["todo"],
            "blocked": buckets["blocked"],
            # 链路台阶逐级计数；「已取得文件」只认 file_downloaded_verified（不许链接冒充）
            "acquired_files": dict(ladder_counts),
            "acquired_files_verified": ladder_counts["file_downloaded_verified"],
            "by_acquisition_status": by_acq,
            "by_parse_status": by_parse,
            "index_progress": index_progress,
            "note": ("仅对账目录项（未取得远端完整清单）" if scope == "catalog_only"
                     else "已核验远端完整清单，可按文件分母对账"),
        }

    # ── 查询 ───────────────────────────────────────────────────────────────
    def get_asset(self, asset_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._con.execute("SELECT * FROM assets WHERE asset_id=?",
                                    (str(asset_id or ""),)).fetchone()
        return self._decode_asset(row) if row else None

    def get_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._con.execute("SELECT * FROM versions WHERE version_id=?",
                                    (str(version_id or ""),)).fetchone()
        return dict(row) if row else None

    def list_sources(self, provider: Optional[str] = None,
                     status: Optional[str] = None) -> List[Dict[str, Any]]:
        """来源清单；status 过滤 acquisition_status（未命中枚举直接 ValueError）。"""
        sql, args = "SELECT * FROM sources", []
        clauses = []
        if provider:
            clauses.append("provider=?")
            args.append(self._validate_enum(provider, SOURCES, "provider"))
        if status:
            clauses.append("acquisition_status=?")
            args.append(self._validate_enum(status, ACQUISITION_STATUSES, "acquisition_status"))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self._con.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["raw"] = self._loads(d.get("raw"), d.get("raw") or "")
            d["unknown_fields"] = self._loads(d.get("unknown_fields"), {})
            d["ladder"] = self._loads(d.get("ladder"), [])
            out.append(d)
        return out

    def list_versions(self, logical_doc_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if logical_doc_id:
                rows = self._con.execute(
                    "SELECT * FROM versions WHERE logical_doc_id=? ORDER BY version_id ASC",
                    (logical_doc_id,)).fetchall()
            else:
                rows = self._con.execute("SELECT * FROM versions ORDER BY version_id ASC").fetchall()
        return [dict(r) for r in rows]

    def list_blocks(self, version_id: str = "", limit: int = 1000, offset: int = 0,
                    evidence_kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """正文块清单。evidence_kind 可选过滤（body 与 catalog 分集合，目录卡不当正文）。"""
        clauses, args = [], []
        if version_id:
            clauses.append("version_id=?")
            args.append(str(version_id))
        if evidence_kind:
            clauses.append("evidence_kind=?")
            args.append(self._validate_enum(evidence_kind, EVIDENCE_KINDS, "evidence_kind"))
        sql = "SELECT * FROM blocks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid ASC LIMIT ? OFFSET ?"
        args.extend([max(int(limit), 0), max(int(offset), 0)])
        with self._lock:
            rows = self._con.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def list_mappings(self, item_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if item_id:
                rows = self._con.execute(
                    "SELECT * FROM entry_asset_mapping WHERE item_id=? ORDER BY id ASC",
                    (item_id,)).fetchall()
            else:
                rows = self._con.execute("SELECT * FROM entry_asset_mapping ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def _decode_asset(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["ladder"] = self._loads(d.get("ladder"), [])
        return d

    def stats(self) -> Dict[str, Any]:
        """各状态计数（供报告）：分维度统计，不用一个 done 掩盖缺口。"""
        q = lambda sql, args=(): self._con.execute(sql, args).fetchall()  # noqa: E731
        with self._lock:
            sources = q("SELECT provider, acquisition_status, parse_status, ladder,"
                        " fts_indexed, catalog_card, body_vectorized FROM sources")
            assets = q("SELECT acquisition_status, parse_status FROM assets")
            by_kind = q("SELECT evidence_kind, COUNT(*) AS n FROM blocks GROUP BY evidence_kind")
            map_status = q("SELECT mapping_status, COUNT(*) AS n FROM entry_asset_mapping"
                           " GROUP BY mapping_status")
            n_versions = self._con.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
            n_blocks = self._con.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
            n_templates = self._con.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        out: Dict[str, Any] = {
            "sources": {"total": len(sources),
                        "by_provider": {p: 0 for p in SOURCES},
                        "by_acquisition_status": {s: 0 for s in ACQUISITION_STATUSES},
                        "by_parse_status": {s: 0 for s in PARSE_STATUSES},
                        "by_ladder": {s: 0 for s in LADDER_STEPS},
                        "index": {f: 0 for f in INDEX_FLAGS}},
            "assets": {"total": len(assets),
                       "by_acquisition_status": {s: 0 for s in ACQUISITION_STATUSES},
                       "by_parse_status": {s: 0 for s in PARSE_STATUSES}},
            "versions": n_versions,
            "blocks": {"total": n_blocks, "by_evidence_kind": {k: 0 for k in EVIDENCE_KINDS}},
            "templates": n_templates,
            "mappings": {"total": sum(int(r["n"]) for r in map_status),
                         "by_status": {s: 0 for s in MAPPING_STATUSES}},
        }
        for r in sources:
            if r["provider"] in out["sources"]["by_provider"]:
                out["sources"]["by_provider"][r["provider"]] += 1
            if r["acquisition_status"] in out["sources"]["by_acquisition_status"]:
                out["sources"]["by_acquisition_status"][r["acquisition_status"]] += 1
            if r["parse_status"] in out["sources"]["by_parse_status"]:
                out["sources"]["by_parse_status"][r["parse_status"]] += 1
            for step in self._loads(r["ladder"], []):
                if step in out["sources"]["by_ladder"]:
                    out["sources"]["by_ladder"][step] += 1
            for flag in INDEX_FLAGS:
                if r[flag] is not None:
                    out["sources"]["index"][flag] += 1
        for r in assets:
            if r["acquisition_status"] in out["assets"]["by_acquisition_status"]:
                out["assets"]["by_acquisition_status"][r["acquisition_status"]] += 1
            if r["parse_status"] in out["assets"]["by_parse_status"]:
                out["assets"]["by_parse_status"][r["parse_status"]] += 1
        for r in by_kind:
            out["blocks"]["by_evidence_kind"][r["evidence_kind"]] = int(r["n"])
        for r in map_status:
            out["mappings"]["by_status"][r["mapping_status"]] = int(r["n"])
        return out

    def close(self) -> None:
        with self._lock:
            self._con.close()
