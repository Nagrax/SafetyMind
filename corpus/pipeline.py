"""K03 自有资料可复跑管线：import_user_owned → 解析（有原件才解析）→ FTS/目录卡/正文向量。

设计依据：docs/资料库建设决策与Agent实施方案_2026-10-19.md §3（1698 条自有资料归属）、
§5（装入 agent 的架构）、§7 的 K03。

来源边界（方案 §3 / §5「来源应至少分成 user_imported / toolbox_catalog / ...」）：
  - `data/knowledge_seed/xlsx_docs.json` 里的记录是**用户自行加入的既有资料**，
    不是百宝箱采集成果 —— 它们以 provider=`user_imported` 独立登记，采集分母与
    `toolbox_acquired` 分开统计，绝不合并抹去；
  - 百宝箱目录层以 provider=`toolbox_catalog`、acquisition_status=`catalog_registered`
    登记占位（**只有目录、没有正文**），用来体现"目录已有但正文未取得"的分母，
    不得把目录算成正文；
  - 转换记录文本可复现（`acquisition_status=acquired_verified` 只对这份"转换记录文本"成立），
    但原始 XLSX **不在工作区** → `open_asset` 必须如实回"无原件"（不伪造原件落盘）。

整条管线幂等：重复运行不产生重复来源行 / 版本行 / 正文块（manifest upsert 语义）。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from corpus import paths
from corpus.build import (activate_build, active_build, build_index, build_meta,
                          legacy_chroma_residue, list_builds)
from corpus.chunking import estimate_tokens
from corpus.fts import pending_fts_migrations
from corpus.manifest import CorpusManifest
from corpus.search import open_asset  # noqa: F401  （对外统一入口：管线交付的原件访问）

_REPO = Path(__file__).resolve().parents[1]
DEFAULT_SEED = _REPO / "data" / "knowledge_seed" / "xlsx_docs.json"

USER_NOTE = ("用户自行加入的既有资料（XLSX 转换记录文本），不是百宝箱采集成果；"
             "来源谱系与采集分母独立统计，原始 XLSX 不在工作区。")
TOOLBOX_NOTE = ("百宝箱资料目录层占位：只有目录/链接，未取得正文，不得当作已入库正文。")
TOOLBOX_PLACEHOLDER_ID = "toolbox-catalog-placeholder"


def _seed_path(seed_path: Optional[Any]) -> Path:
    import os
    if seed_path:
        return Path(seed_path)
    env = os.getenv("SAFETYMIND_KNOWLEDGE_SEED")
    if env:
        return Path(env)
    return DEFAULT_SEED


def _stable_item_id(index: int, content: str) -> str:
    """稳定 item_id：文件内序号 + 内容哈希（同文件重跑 → 同 ID，幂等）。"""
    h = hashlib.sha1((content or "").encode("utf-8")).hexdigest()[:12]
    return f"xlsx:{int(index)}:{h}"


def import_user_owned(root: Optional[Any] = None, *, seed_path: Optional[Any] = None) -> Dict[str, Any]:
    """把自有资料（转换记录文本）灌进 manifest，并登记百宝箱目录层占位。"""
    t0 = time.time()
    base_root = Path(root).resolve() if root else paths.corpus_root()
    seed = _seed_path(seed_path)
    records = json.loads(Path(seed).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"seed 必须是 JSON 数组：{seed}")

    m = CorpusManifest(paths.manifest_db(base_root))
    added_blocks = 0
    try:
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
            title = str(rec.get("title") or "")
            content = str(rec.get("content") or "")
            meta = rec.get("meta") or {}
            item_id = _stable_item_id(i, content)
            sid = m.upsert_source(
                "user_imported", item_id, title=title,
                category=str(meta.get("big") or ""),
                acquisition_status="acquired_verified", parse_status="parsed",
                raw={"note": USER_NOTE, "index": i, "meta": meta, "origin_present": False})
            logical = m.make_logical_doc_id("user_imported", item_id=item_id)
            vid = m.make_version_id(logical, "xlsx-v1")
            m.add_version(logical, vid, sid, title, "",
                          category=str(meta.get("big") or ""), access_scope="",
                          status="unknown")
            added_blocks += m.add_blocks(vid, [{
                "content": content, "locator": {"index": i},
                "token_count": estimate_tokens(content), "evidence_kind": "body"}])

        m.upsert_source("toolbox_catalog", TOOLBOX_PLACEHOLDER_ID,
                        title="百宝箱资料目录（占位）", category="",
                        acquisition_status="catalog_registered", parse_status="not_started",
                        raw={"note": TOOLBOX_NOTE, "origin_present": False})

        stats = m.stats()
        sources_by_provider = stats["sources"]["by_provider"]
    finally:
        m.close()

    return {
        "provider": "user_imported",
        "seed": seed.as_posix(),
        "records": len(records),
        "user_imported_sources": sources_by_provider.get("user_imported", 0),
        "toolbox_catalog_sources": sources_by_provider.get("toolbox_catalog", 0),
        "versions": stats["versions"],
        "blocks": stats["blocks"]["total"],
        "added_blocks_this_run": added_blocks,
        "note": USER_NOTE,
        "elapsed_s": round(time.time() - t0, 3),
    }


def parse_acquired(root: Optional[Any] = None) -> Dict[str, Any]:
    """解析阶段：**只在有原件落盘时才解析**，把完整段落写入 manifest 正文块。

    本轮自有资料只有转换记录文本（无原件），因此本阶段通常为空 —— 不伪造原件。
    """
    from corpus.parsers import parse_file
    t0 = time.time()
    base_root = Path(root).resolve() if root else paths.corpus_root()
    m = CorpusManifest(paths.manifest_db(base_root))
    parsed = skipped = added = 0
    try:
        for v in m.list_versions():
            asset_id = str(v.get("asset_id") or "")
            if not asset_id:
                continue
            asset = m.get_asset(asset_id)
            if not asset:
                continue
            path = Path(str(asset.get("path") or ""))
            if not path.exists():
                skipped += 1
                continue
            res = parse_file(str(path))
            m.add_blocks(str(v["version_id"]), [
                {"content": b.content, "locator": b.locator, "kind": b.kind,
                 "evidence_kind": "body"} for b in res.blocks if b.content.strip()])
            m.set_status(asset_id, parse_status=res.status)
            parsed += 1
            added += len([b for b in res.blocks if b.content.strip()])
    finally:
        m.close()
    return {"parsed": parsed, "skipped_no_original": skipped, "blocks": added,
            "elapsed_s": round(time.time() - t0, 3)}


def run_full_pipeline(root: Optional[Any] = None, *, build_id: Optional[str] = None,
                      seed_path: Optional[Any] = None, max_docs: int = 300,
                      max_blocks: int = 30000,
                      source_providers: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """自有资料可复跑管线：import → 解析 → build（FTS/目录卡/正文向量同 build）。

    幂等：重复运行（同 build_id）不产生重复来源/版本/正文块，也不重复向量。
    """
    t0 = time.time()
    base_root = Path(root).resolve() if root else paths.corpus_root()
    bid = build_id or f"build-{time.strftime('%Y%m%d-%H%M%S')}"
    imp = import_user_owned(base_root, seed_path=seed_path)
    par = parse_acquired(base_root)
    meta = build_index(bid, root=base_root, source_providers=source_providers,
                       max_docs=max_docs, max_blocks=max_blocks)
    active = activate_build(bid, base_root)
    return {"import": imp, "parse": par, "build": meta, "active": active,
            "build_id": bid, "marker": meta.get("marker", ""),
            "elapsed_s": round(time.time() - t0, 3)}


def corpus_status(root: Optional[Any] = None) -> Dict[str, Any]:
    """三台阶计数与来源分布（供 /corpus/status 报告）：目录 / 正文 / 原件分开报。"""
    base_root = Path(root).resolve() if root else paths.corpus_root()
    m = CorpusManifest(paths.manifest_db(base_root))
    active = active_build(base_root)
    try:
        stats = m.stats()
        recon = m.reconciliation()
        assets_total = int((stats.get("assets") or {}).get("total") or 0)
        # 正文台阶：manifest 正文块（evidence_kind=body）与其覆盖的版本数
        body_versions = 0
        try:
            with m._lock:
                body_versions = int(m._con.execute(
                    "SELECT COUNT(DISTINCT version_id) FROM blocks WHERE evidence_kind='body'"
                ).fetchone()[0])
        except Exception:
            body_versions = 0
        # 原件台阶：已登记资产中本地文件真实存在的数量
        materialized = 0
        try:
            with m._lock:
                rows = m._con.execute("SELECT path FROM assets").fetchall()
            for r in rows:
                p = str(r[0] or "")
                if p and Path(p).exists():
                    materialized += 1
        except Exception:
            pass
    finally:
        m.close()
    build_counts = (build_meta(active["build_id"], base_root) or {}).get("counts") if active else {}
    build_counts = build_counts or {}
    # 存储收敛状态（如实报告）：FTS 与 manifest 同库；旧独立 fts / 旧每 build chroma 的迁移进度
    pending = pending_fts_migrations(base_root)
    residue = legacy_chroma_residue(base_root)
    migration = {
        "fts_db_kind": "manifest",
        "fts_db": str(paths.manifest_db(base_root)),
        "pending_legacy_fts_builds": pending,
        "legacy_chroma_dirs": residue,
        "legacy_chroma_bytes": sum(int(r.get("bytes") or 0) for r in residue),
        "note": ("FTS 已并入 manifest 单库；无待迁移旧独立 fts 文件" if not pending
                 else f"仍有个 build 的旧独立 fts.sqlite 待迁移：{pending}"),
    }
    return {
        "active_build": active,
        "builds": [{"build_id": b.get("build_id"), "marker": b.get("marker"),
                    "created_at": b.get("created_at"), "stopped_by": b.get("stopped_by"),
                    "counts": b.get("counts"), "limits": b.get("limits")}
                   for b in list_builds(base_root)],
        "storage": {"manifest_db": str(paths.manifest_db(base_root)),
                    "chroma_dir": str(paths.chroma_dir(base_root)),
                    "fts_db": str(paths.manifest_db(base_root)),
                    "fts_db_kind": "manifest"},
        "migration": migration,
        "sources": {"total": stats["sources"]["total"],
                    "by_provider": stats["sources"]["by_provider"],
                    "by_acquisition_status": stats["sources"]["by_acquisition_status"],
                    "by_parse_status": stats["sources"]["by_parse_status"]},
        "ladder": {
            "catalog_entries": stats["sources"]["by_acquisition_status"].get("catalog_registered", 0),
            "catalog_cards": int(build_counts.get("cards") or 0),
            "body_versions": body_versions,
            "body_blocks": int(stats["blocks"]["total"]),
            "originals": assets_total,
            "originals_materialized": materialized,
        },
        "reconciliation": recon,
    }
