"""K03/K07 检索：三路召回（FTS + 短目录卡向量 + 精选正文向量）→ RRF 融合 → 定位回取。

设计依据：方案 §5.2「各通道先过滤再召回 → RRF 融合/去重 → 按定位读取实际正文或复核范围」，
§7 的 K03/K07。

三条硬约束：
  1. **目录卡不得被当作正文证据**：返回的 `blocks` 一律 evidence_kind=body；
     命中目录卡后必须回取 manifest 里的实际正文；无正文则该版本明确标"仅目录"。
  2. **范围过滤 fail-closed**：scopes=None（本地单人形态）不过滤；给了 scopes 时按
     `access_scope` 过滤（空 = 公共），过滤无法生效时返回空，绝不把无权文档交出去
     （与 S-05 的 mcp/knowledge_base 同口径）。
  3. **只给原件与定位**：`open_asset` 返回原件本地路径/字节/mime/解析状态，不把正文当证据。

`CorpusSearch.search` 同步执行；上层（API/工具）按需 asyncio.to_thread 包装。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from corpus import paths
from corpus.build import (active_build, body_collection_name, build_meta,
                          card_collection_name, embedding_for_mode)
from corpus.catalog import BODY_KIND, CATALOG_KIND, assert_not_body_evidence
from corpus.fts import CANDIDATES_PER_CHANNEL, RRF_K, CorpusFTS, rrf_fuse
from corpus.manifest import CorpusManifest

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_TOKENS = 4000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class CorpusSearch:
    """基于某个 build（缺省 ACTIVE）的三路召回检索器。"""

    def __init__(self, build_id: Optional[str] = None, root: Optional[Any] = None):
        self._root = Path(root).resolve() if root else paths.corpus_root()
        if build_id is None:
            active = active_build(self._root)
            if not active or not active.get("build_id"):
                raise RuntimeError(f"无激活 build（{self._root}/indexes/ACTIVE.json），请先 build_index 并 activate_build")
            build_id = str(active["build_id"])
        self._build_id = str(build_id)
        meta = build_meta(self._build_id, self._root)
        if meta is None:
            raise ValueError(f"未找到 build 的 build.json：{self._build_id!r}")
        self._meta = meta
        self._marker = str(meta.get("marker") or "")
        embed = meta.get("embed") or {}
        self._embed_mode = str(embed.get("mode") or "none")
        self._embed_name = str(embed.get("name") or "none")
        self._embed_meta = embed

        self._manifest = CorpusManifest(paths.manifest_db(self._root))
        # 存储收敛：FTS 与 manifest 同库；该 build 若还留有旧独立 fts.sqlite，先一次性并入
        from corpus.fts import legacy_fts_path, migrate_fts_into_manifest
        if legacy_fts_path(self._build_id, self._root).exists():
            try:
                migrate_fts_into_manifest(self._build_id, self._root)
            except Exception:
                pass
        self._fts = CorpusFTS(db_path=str(paths.manifest_db(self._root)))
        self._client = None
        self._cards = None
        self._body = None
        if self._embed_mode in ("bge", "ngram"):
            ef, _name = embedding_for_mode(self._embed_mode)
            from core.chroma_utils import open_local_chroma
            # 向量目录收敛为单一 indexes/chroma（所有 build 共用）；旧 build 的 meta 仍指向其旧目录
            self._client = open_local_chroma(str(meta.get("chroma_dir") or paths.chroma_dir(self._root)))
            cols = meta.get("collections") or {}
            self._cards = self._client.get_or_create_collection(
                name=str(cols.get("cards") or card_collection_name(self._build_id)),
                embedding_function=ef)
            self._body = self._client.get_or_create_collection(
                name=str(cols.get("body") or body_collection_name(self._build_id)),
                embedding_function=ef)

    # ── 生命周期 ─────────────────────────────────────────────────────────────
    def close(self) -> None:
        for obj in (self._fts, self._manifest):
            try:
                obj.close()
            except Exception:
                pass
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass

    @property
    def build_info(self) -> Dict[str, Any]:
        return {"build_id": self._build_id, "marker": self._marker,
                "embed": self._embed_meta}

    # ── 检索 ───────────────────────────────────────────────────────────────
    def search(self, query: str, *, top_k: int = 8,
               scopes: Optional[List[str]] = None, allow_catalog: bool = True,
               budget_tokens: Optional[int] = None,
               limit_blocks: int = 32) -> Dict[str, Any]:
        """三路并行召回 + RRF 融合 + 定位回取，返回 blocks/cards/build/budget。

        blocks：仅正文（evidence_kind=body）；cards：仅目录级（evidence_kind=catalog）。
        """
        budget = int(budget_tokens if budget_tokens is not None
                     else int(_env_budget()))
        build = self.build_info

        # ── 三路召回 ────────────────────────────────────────────────────────
        fts_hits = self._fts.search(query, limit=CANDIDATES_PER_CHANNEL,
                                    build_id=self._build_id)
        fts_by_id: Dict[str, Dict[str, Any]] = {h["block_id"]: h for h in fts_hits}
        rankings: List[List[str]] = [[h["block_id"] for h in fts_hits]]

        card_by_id: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        if allow_catalog and self._cards is not None:
            res = self._query_collection(self._cards, query)
            ids = res["ids"]
            rankings.append(ids)
            for cid, doc, meta in zip(ids, res["documents"], res["metadatas"]):
                card_by_id[cid] = (doc, meta or {})

        body_by_id: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        if self._body is not None:
            res = self._query_collection(self._body, query)
            ids = res["ids"]
            rankings.append(ids)
            for bid, doc, meta in zip(ids, res["documents"], res["metadatas"]):
                body_by_id[bid] = (doc, meta or {})

        fused = rrf_fuse(rankings, k=RRF_K, limit=max(1, int(limit_blocks)))
        scores = _rrf_scores(rankings, RRF_K)

        # ── 定位回取与结果装配 ──────────────────────────────────────────────
        blocks: List[Dict[str, Any]] = []
        cards: List[Dict[str, Any]] = []
        seen_blocks: set = set()
        used_tokens = 0

        for cid in fused:
            if len(blocks) >= max(1, int(top_k)):
                break
            if cid in body_by_id:
                doc, meta = body_by_id[cid]
                blocks.append(self._make_block(cid, doc, meta, scores.get(cid, 0.0)))
                seen_blocks.add(cid)
            elif cid in fts_by_id:
                h = fts_by_id[cid]
                blocks.append(self._make_block(cid, h.get("content", ""), {
                    "version_id": h.get("version_id", ""),
                    "locator": json.dumps(h.get("locator") or {}, ensure_ascii=False),
                    "token_count": h.get("token_count", 0),
                }, scores.get(cid, 0.0)))
                seen_blocks.add(cid)
            elif cid in card_by_id:
                text, meta = card_by_id[cid]
                card, back = self._materialize_card(cid, text, meta, scores.get(cid, 0.0))
                cards.append(card)
                for b in back:
                    if b["block_id"] in seen_blocks:
                        continue
                    seen_blocks.add(b["block_id"])
                    blocks.append(b)

        # ── 去重（同一 version 同一正文只保留一次：卡回取的整块与向量命中块会重叠）──
        uniq: List[Dict[str, Any]] = []
        seen_content: set = set()
        for b in blocks:
            key = (b["version_id"], b.get("content", ""))
            if key in seen_content:
                continue
            seen_content.add(key)
            uniq.append(b)
        blocks = uniq

        # ── 范围过滤（fail-closed）──────────────────────────────────────────
        filter_failed = False
        try:
            blocks = self._filter_scope(blocks, scopes)
            cards = self._filter_scope(cards, scopes)
        except Exception as ex:
            filter_failed = True
            logger.error("corpus 访问范围过滤无法生效，按 fail-closed 返回空结果"
                         "（不降级为不过滤：无权文档不得进入检索上下文）：%s", ex)
            blocks, cards = [], []

        # ── token 预算 + top_k 上限 ─────────────────────────────────────────
        pruned: List[Dict[str, Any]] = []
        for b in blocks:
            t = int(b.get("token_count") or 0)
            if used_tokens + t > budget:
                break
            pruned.append(b)
            used_tokens += t
        blocks = pruned[:max(1, int(top_k))]
        cards = cards[:max(1, int(top_k))]

        # 目录卡不得以正文形式出现：显式断言（防御性，fail-loud）
        for b in blocks:
            assert_not_body_evidence(b)

        return {
            "query": query,
            "blocks": blocks,
            "cards": cards,
            "build": build,
            "budget": {"tokens": used_tokens, "limit_tokens": budget,
                       "blocks": len(blocks), "limit_blocks": int(limit_blocks)},
            "scopes": list(scopes) if scopes else None,
            "filter_failed": filter_failed,
        }

    # ── 内部 ───────────────────────────────────────────────────────────────
    @staticmethod
    def _query_collection(coll, query: str) -> Dict[str, List[Any]]:
        res = coll.query(query_texts=[query], n_results=CANDIDATES_PER_CHANNEL)
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        return {"ids": list(ids), "documents": list(docs), "metadatas": list(metas)}

    def _make_block(self, block_id: str, content: str, meta: Dict[str, Any],
                    score: float) -> Dict[str, Any]:
        try:
            locator = json.loads(meta.get("locator") or "{}")
        except Exception:
            locator = {}
        version_id = str(meta.get("version_id") or "")
        return {
            "block_id": str(block_id),
            "version_id": version_id,
            "evidence_kind": BODY_KIND,
            "locator": locator,
            "content": str(content or ""),
            "token_count": int(meta.get("token_count") or 0),
            "score": round(float(score), 6),
            "title": str(meta.get("title") or ""),
            "category": str(meta.get("category") or ""),
            "status": str(meta.get("status") or ""),
            "access_scope": str(meta.get("access_scope") or ""),
        }

    def _materialize_card(self, card_id: str, text: str, meta: Dict[str, Any],
                          score: float) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """命中目录卡 → 回取该 version 的实际正文（manifest evidence_kind=body）。"""
        version_id = str(meta.get("version_id") or "")
        rows = self._manifest.list_blocks(version_id, evidence_kind=BODY_KIND)
        back: List[Dict[str, Any]] = []
        for i, r in enumerate(rows):
            content = str(r.get("content") or "")
            if not content.strip():
                continue
            back.append(self._make_block(
                str(r.get("block_id") or f"{version_id}#cardback#{i}"), content,
                {"version_id": version_id, "locator": r.get("locator"),
                 "token_count": r.get("token_count") or 0,
                 "title": meta.get("title"), "category": meta.get("category"),
                 "access_scope": meta.get("access_scope")}, score))
        card = {
            "card_id": str(card_id),
            "version_id": version_id,
            "evidence_kind": CATALOG_KIND,
            "title": str(meta.get("title") or ""),
            "category": str(meta.get("category") or ""),
            "text": str(text or ""),
            "token_count": int(meta.get("token_count") or 0),
            "score": round(float(score), 6),
            "access_scope": str(meta.get("access_scope") or ""),
            "catalog_only": not bool(back),
            "backfetched_block_ids": [b["block_id"] for b in back],
            "note": ("命中目录卡，已回取实际正文" if back
                     else "仅目录：该版本无可回取正文，不得当作条款引用"),
        }
        return card, back

    def _scope_of(self, version_id: str) -> str:
        """版本访问范围（空 = 公共）。单独成法，便于上层注入/测试 fail-closed 行为。"""
        v = self._manifest.get_version(version_id)
        return str((v or {}).get("access_scope") or "")

    def _filter_scope(self, items: List[Dict[str, Any]],
                      scopes: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
        if scopes is None or "*" in scopes:
            return items
        kept: List[Dict[str, Any]] = []
        for it in items:
            scope = self._scope_of(it.get("version_id", ""))
            if scope == "" or scope in scopes:
                kept.append(it)
        return kept


def _rrf_scores(rankings: Sequence[Sequence[str]], k: int) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    for ranking in rankings or []:
        for rank, bid in enumerate(ranking or [], start=1):
            scores[bid] = scores.get(bid, 0.0) + 1.0 / (k + rank)
    return scores


def _env_budget() -> int:
    import os
    try:
        return int(os.getenv("SAFETYMIND_CORPUS_BUDGET_TOKENS", str(DEFAULT_BUDGET_TOKENS)))
    except ValueError:
        return DEFAULT_BUDGET_TOKENS


# ── 原件访问（K07：只给原件与定位，不把正文当证据）──────────────────────────

def open_asset(asset_id_or_sha: str, root: Optional[Any] = None) -> Dict[str, Any]:
    """打开原件：返回本地路径/字节/mime/解析状态/is_materialized。

    入参可为 asset_id / sha256（内容寻址）或来源 item_id（经 entry_asset_mapping 反查）。
    **不返回正文内容** —— 正文是证据、原件是文件，两者口径不同。
    未落盘的原件如实回 acquisition_status 与阻塞原因，不得当作已取得。
    """
    base_root = Path(root).resolve() if root else paths.corpus_root()
    m = CorpusManifest(paths.manifest_db(base_root))
    try:
        key = str(asset_id_or_sha or "")
        asset = None
        if _SHA256_RE.fullmatch(key):
            asset = m.get_asset(key)
        if asset is None and key:
            for mp in m.list_mappings(key):
                a = m.get_asset(mp.get("asset_id", ""))
                if a:
                    asset = a
                    break
        if asset is None:
            src = next((s for s in m.list_sources() if s.get("item_id") == key), None)
            return {
                "found": False,
                "asset_id": "",
                "sha256": "",
                "path": "",
                "bytes": 0,
                "mime": "",
                "parse_status": str((src or {}).get("parse_status") or ""),
                "acquisition_status": str((src or {}).get("acquisition_status") or ""),
                "is_materialized": False,
                "blocking_reason": ("无原件：该条目只有转换记录文本，原始文件不在工作区"
                                    if src is not None else "无原件：未找到该资产或条目"),
            }
        path = Path(str(asset.get("path") or ""))
        materialized = bool(path and path.exists())
        return {
            "found": True,
            "asset_id": str(asset.get("asset_id") or ""),
            "sha256": str(asset.get("sha256") or ""),
            "path": path.as_posix() if str(path) else "",
            "bytes": int(asset.get("bytes") or 0),
            "mime": str(asset.get("mime") or ""),
            "parse_status": str(asset.get("parse_status") or ""),
            "acquisition_status": str(asset.get("acquisition_status") or ""),
            "is_materialized": materialized,
            "blocking_reason": "" if materialized else "原件已登记但本地文件缺失（未落盘）",
        }
    finally:
        try:
            m.close()
        except Exception:
            pass
