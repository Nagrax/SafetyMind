"""K06 短目录卡：由标题、分类、字段与已解析正文的确定性抽取生成，长度受限。

设计依据：方案 §5.2 —— 所有符合检索条件的文档先建一张短目录卡，卡从标题、目录、
字段和已解析正文确定性生成，标记 `evidence_kind=catalog`；**卡与正文分两个集合**；
命中卡后必须回取实际正文，**不拿目录摘要当规范条文**。

本模块三件事：
  1. `make_catalog_card` 生成 ≤ CATALOG_MAX_TOKENS 的短卡（只取标题/章节名/首行/编号，
     绝不含正文全文）；
  2. `is_body_evidence` / `assert_not_body_evidence` 供 K07 引用前做类型断言；
  3. 卡与正文**分离写入** `catalog_cards` / `body_blocks` 两张表，不混存。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from corpus.fts import extract_cas, extract_standard_nos, norm_cas, norm_standard

CATALOG_KIND = "catalog"
BODY_KIND = "body"

CATALOG_MAX_TOKENS = 400   # 短目录卡长度上限
MAX_HEADINGS = 4           # 卡里最多列几个标题/章节名
MAX_LEAD_LINES = 3         # 卡里最多列几行正文首行
MAX_CODES = 8              # 标准号 / CAS 各最多列几个
LINE_MAX_CHARS = 80        # 单行截断长度（防止某行本身过长）

CATALOG_TABLE = "catalog_cards"
BODY_TABLE = "body_blocks"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {CATALOG_TABLE}(
  card_id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  evidence_kind TEXT NOT NULL DEFAULT '{CATALOG_KIND}',
  text TEXT NOT NULL DEFAULT '',
  token_count INTEGER NOT NULL DEFAULT 0,
  standard_nos TEXT NOT NULL DEFAULT '',
  cas TEXT NOT NULL DEFAULT '',
  locators TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS {BODY_TABLE}(
  block_id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL DEFAULT '',
  evidence_kind TEXT NOT NULL DEFAULT '{BODY_KIND}',
  content TEXT NOT NULL DEFAULT '',
  locator TEXT NOT NULL DEFAULT '{{}}',
  kind TEXT NOT NULL DEFAULT 'text',
  token_count INTEGER NOT NULL DEFAULT 0
);
"""


def _evidence_kind(block: Any) -> Optional[str]:
    if isinstance(block, Mapping):
        return block.get("evidence_kind")
    return getattr(block, "evidence_kind", None)


def is_body_evidence(block: Any) -> bool:
    """块是否为正文字段（evidence_kind == "body"）；目录卡一律不是正文证据。"""
    return _evidence_kind(block) == BODY_KIND


def assert_not_body_evidence(block: Any) -> bool:
    """引用前断言：只允许正文块通过；目录卡（或类型缺失）一律抛错。

    K07 在把块送进证据集合前必须调用本函数 —— 目录卡绝不能被当作正文证据引用。
    """
    kind = _evidence_kind(block)
    if kind == CATALOG_KIND:
        raise AssertionError("目录卡（evidence_kind=catalog）不得作为正文证据引用，必须回取实际正文")
    if kind != BODY_KIND:
        raise AssertionError(f"块缺少正文证据类型（evidence_kind={kind!r}），fail-closed 拒绝引用")
    return True


def _block_dict(block: Any) -> Dict[str, Any]:
    if isinstance(block, Mapping):
        return {"content": str(block.get("content", "") or ""),
                "kind": str(block.get("kind") or "text"),
                "locator": dict(block.get("locator") or {}),
                "evidence_kind": block.get("evidence_kind")}
    return {"content": str(getattr(block, "content", "") or ""),
            "kind": str(getattr(block, "kind", "text") or "text"),
            "locator": dict(getattr(block, "locator", {}) or {}),
            "evidence_kind": getattr(block, "evidence_kind", None)}


def _blocks_of(parsed: Any) -> List[Dict[str, Any]]:
    if parsed is None:
        return []
    if isinstance(parsed, Mapping):
        if "blocks" in parsed:
            return [_block_dict(b) for b in (parsed.get("blocks") or [])]
        text = parsed.get("text")
        if text:
            return [{"content": str(text), "kind": "text", "locator": {}, "evidence_kind": None}]
        return []
    blocks = getattr(parsed, "blocks", None)
    if blocks is not None:
        return [_block_dict(b) for b in blocks]
    text = getattr(parsed, "text", None)
    if text:
        return [{"content": str(text), "kind": "text", "locator": {}, "evidence_kind": None}]
    return []


def _clip_line(line: str) -> str:
    line = " ".join((line or "").split())
    return line[:LINE_MAX_CHARS]


def make_catalog_card(version: Mapping, parsed: Mapping) -> Dict[str, Any]:
    """由版本元数据 + 已解析正文的确定性抽取生成短目录卡（≤ 400 token）。

    仅取：标题、分类、来源/版本字段、正文中的标题/章节名、正文首行、标准号与 CAS。
    **不含正文全文**；卡生成是确定性的（同输入 → 同卡），便于重建与比对。
    """
    from corpus.chunking import estimate_tokens  # 轻量估算，避免加载模型

    version = version or {}
    title = str(version.get("title") or "")
    category = str(version.get("category") or version.get("class") or "")
    version_id = str(version.get("version_id") or version.get("logical_doc_id") or version.get("id") or "")
    source = str(version.get("source") or version.get("source_id") or "")
    status = str(version.get("status") or "")

    blocks = _blocks_of(parsed)
    headings: List[str] = []
    lead_lines: List[str] = []
    standards: List[str] = []
    cas_list: List[str] = []
    locators: List[Dict[str, Any]] = []

    for blk in blocks:
        content = blk["content"]
        if blk["kind"] == "heading":
            line = _clip_line(content)
            if line and line not in headings and len(headings) < MAX_HEADINGS:
                headings.append(line)
        if len(lead_lines) < MAX_LEAD_LINES:
            for raw in content.splitlines():
                line = _clip_line(raw)
                if line and line not in lead_lines:
                    lead_lines.append(line)
                    locators.append(blk["locator"])
                    break
        for std in extract_standard_nos(content):
            n = norm_standard(std)
            if n not in standards and len(standards) < MAX_CODES:
                standards.append(n)
        for c in extract_cas(content):
            n = norm_cas(c)
            if n not in cas_list and len(cas_list) < MAX_CODES:
                cas_list.append(n)

    lines: List[str] = []
    if title:
        lines.append(f"标题：{title}")
    if category:
        lines.append(f"分类：{category}")
    if source or version_id or status:
        lines.append(f"来源：{source} 版本：{version_id} 状态：{status}".strip())
    if headings:
        lines.append("章节：" + " / ".join(headings))
    if lead_lines:
        lines.append("摘要：" + " ".join(lead_lines))
    if standards:
        lines.append("标准号：" + " ".join(standards))
    if cas_list:
        lines.append("CAS：" + " ".join(cas_list))

    # 长度受限：逐行累加，超上限即停（末行按字符位置硬截）
    kept: List[str] = []
    used = 0
    for line in lines:
        t = estimate_tokens(line)
        if used + t > CATALOG_MAX_TOKENS:
            remaining = CATALOG_MAX_TOKENS - used
            if remaining > 0:
                kept.append(line[: max(1, remaining)])
            break
        kept.append(line)
        used += t
    text = "\n".join(kept)

    card_id = hashlib.sha256(f"{version_id}|{title}".encode("utf-8")).hexdigest()[:24]
    return {
        "card_id": card_id,
        "version_id": version_id,
        "title": title,
        "category": category,
        "evidence_kind": CATALOG_KIND,
        "text": text,
        "token_count": estimate_tokens(text),
        "headings": headings,
        "lead_lines": lead_lines,
        "standard_nos": standards,
        "cas": cas_list,
        "locators": locators,
    }


# ── 卡与正文分离存储 ──────────────────────────────────────────────────────────

def catalog_db_path() -> Path:
    """目录卡库路径（默认与 manifest 同目录的 catalog.sqlite，可由环境变量覆盖）。"""
    env = os.getenv("SAFETYMIND_CATALOG_DB")
    if env:
        return Path(env)
    from corpus.paths import manifest_db
    return manifest_db().parent / "catalog.sqlite"


def _connect(db_path: Any) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def write_catalog_card(db_path: Any, card: Mapping) -> str:
    """写入目录卡表（只接受 evidence_kind=catalog；不接受正文块）。"""
    if _evidence_kind(card) != CATALOG_KIND:
        raise AssertionError(
            f"write_catalog_card 只接受 evidence_kind={CATALOG_KIND!r} 的卡，收到 {_evidence_kind(card)!r}")
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {CATALOG_TABLE}"
            "(card_id, version_id, title, category, evidence_kind, text, token_count, "
            "standard_nos, cas, locators) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(card.get("card_id", "")), str(card.get("version_id", "")),
             str(card.get("title", "")), str(card.get("category", "")), CATALOG_KIND,
             str(card.get("text", "")), int(card.get("token_count", 0) or 0),
             json.dumps(list(card.get("standard_nos") or []), ensure_ascii=False),
             json.dumps(list(card.get("cas") or []), ensure_ascii=False),
             json.dumps(list(card.get("locators") or []), ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return str(card.get("card_id", ""))


def write_body_blocks(db_path: Any, version_id: str, blocks: Sequence[Any]) -> int:
    """写入正文块表（只接受 evidence_kind=body；遇到目录卡抛错，杜绝混存）。"""
    conn = _connect(db_path)
    n = 0
    try:
        for blk in blocks:
            assert_not_body_evidence(blk)  # 目录卡 / 类型缺失一律拒绝
            b = _block_dict(blk)
            block_id = hashlib.sha256(
                f"{version_id}|{n}|{b['content']}".encode("utf-8")).hexdigest()[:32]
            conn.execute(
                f"INSERT OR REPLACE INTO {BODY_TABLE}"
                "(block_id, version_id, evidence_kind, content, locator, kind, token_count) "
                "VALUES(?,?,?,?,?,?,?)",
                (block_id, version_id, BODY_KIND, b["content"],
                 json.dumps(b["locator"], ensure_ascii=False), b["kind"],
                 int(getattr(blk, "token_count", 0) or 0)))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def read_catalog_cards(db_path: Any) -> List[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(f"SELECT * FROM {CATALOG_TABLE}").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def read_body_blocks(db_path: Any) -> List[Dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(f"SELECT * FROM {BODY_TABLE}").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
