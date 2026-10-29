"""K03 统一 build：唯一 build 标记、小批构建与切换/回滚。

设计依据：docs/资料库建设决策与Agent实施方案_2026-10-19.md §5（装入 agent 的架构）、
§5.2（检索流程与预算）、§7 的 K03/K06。

一条 build 做三件事，产物收敛到 **1 个 SQLite + 1 个向量目录**（存储减法，2026-10）：
  1. 短目录卡向量：写入集合 `corpus_cards__<build_id>`（evidence_kind=catalog）；
  2. 精选正文向量：写入集合 `corpus_body__<build_id>`（evidence_kind=body）；
     —— 两条 collection 共用**单一**向量目录 `indexes/chroma`（所有 build 同目录，
        靠集合名区分；不再每 build 一份 `indexes/<build_id>/chroma/`）；
  3. FTS 词面索引：写入 **manifest 同一个 SQLite**（`corpus.paths.manifest_db()`）的
     `corpus_fts_*` 表，按 build_id 列区分不同 build；不再每 build 一份 `fts.sqlite`。
另落 `build.json`（marker/counts/上限/嵌入配置/工具版本/耗时）。

激活指针在 `indexes/ACTIVE.json`：小批构建 → 切换 → 回滚都只改指针，旧 build 的
`build.json` 与指针记录全部保留（构建/切换/回滚可复演）。退役/回滚回收向量空间用
`drop_build_collections()`：**只删 collection，不删 build.json**（marker/统计仍可追溯，
需要时按同一输入复算重建）。

**汇总上限**（方案 §5.2）：正文向量先 80 份、后至多 300 份，且不超过 30000 块，
两个上限先到哪个就停；返回值如实报告 `stopped_by`。

建库嵌入走 `core.bge_embedding.BgeEmbeddingFunction.embed_documents`（**绕在线查询缓存**），
并记录模型/维度/池化/归一化/查询前缀/分块版本（方案 §5.2「文档与查询必须使用同一配置」）。

**不复用 data/kb_chroma**：corpus 建库失败或写入压力不得威胁用户现网 RAG 目录
（铁律 9 明确并发打开会损坏 HNSW 段），故向量只落资料根目录下的 `indexes/chroma`。
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from corpus import paths
from corpus.catalog import BODY_KIND, CATALOG_KIND, make_catalog_card
from corpus.chunking import Chunk, chunk_blocks
from corpus.fts import CorpusFTS
from corpus.manifest import CorpusManifest

# 分块契约版本（与门禁测试绑定的口径；改动需同步测试与报告）
CHUNKING_VERSION = "k06-chunk-v1"
CHUNK_MAX_TOKENS = 220
CHUNK_TARGET_TOKENS = 180
EMBED_MAX_LENGTH = 256

ACTIVE_NAME = "ACTIVE.json"


# ── 嵌入函数（与 mcp.knowledge_base 同一选择规则）───────────────────────────

def embedding_function() -> Tuple[Any, str, str]:
    """返回 (嵌入函数, 模式, 名称)。auto/bge 优先 BGE，缺失回退 n-gram。

    建库路径经 chroma 写入 → 走嵌入函数的 `__call__` → `embed_documents`
    （见 core/bge_embedding.py：默认不写在线查询缓存）。"""
    mode = os.getenv("SAFETYMIND_EMBEDDING", "auto")  # auto | bge | ngram
    if mode in ("auto", "bge"):
        from core.bge_embedding import try_bge_embedding
        fn = try_bge_embedding()
        if fn is not None:
            return fn, "bge", fn.name()
        if mode == "bge":
            raise RuntimeError("SAFETYMIND_EMBEDDING=bge 但 BGE 嵌入加载失败")
    from core.local_embedding import LocalEmbeddingFunction
    fn = LocalEmbeddingFunction()
    return fn, "ngram", fn.name()


def embedding_for_mode(mode: str) -> Tuple[Any, str]:
    """按 build 记录的模式重建同一嵌入函数（查询端必须与建库端同配置）。"""
    if mode == "bge":
        from core.bge_embedding import try_bge_embedding
        fn = try_bge_embedding()
        if fn is None:
            raise RuntimeError("build 使用 BGE 嵌入，但当前环境加载失败，查询/建库配置不一致")
        return fn, fn.name()
    from core.local_embedding import LocalEmbeddingFunction
    fn = LocalEmbeddingFunction()
    return fn, fn.name()


# ── 稳定 block_id（与 corpus.fts 的写入口径一致：version + 块序号 + 正文）──────

def stable_block_id(version_id: str, index: int, content: str) -> str:
    """跨通道统一的正文档 block_id（FTS 通道按同一公式生成，故 RRF 可直接融合）。

    公式与 `corpus.fts._block_id` 一致：sha256(f"{version_id}|{index}|{content}")[:32]。
    集中在此处，避免检索端与建库端两条口径漂移。
    """
    raw = f"{version_id}|{index}|{content}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def card_collection_name(build_id: str) -> str:
    return f"corpus_cards__{_safe_part(build_id)}"


def body_collection_name(build_id: str) -> str:
    return f"corpus_body__{_safe_part(build_id)}"


def _safe_part(build_id: str) -> str:
    """把 build_id 归一成合法集合名片段（chroma 名须以字母数字起止、3~63 字符）。"""
    keep = [c for c in str(build_id) if c.isalnum() or c in "_-"]
    safe = "".join(keep)[:40].strip("_-")
    return safe or "b"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return obj.as_posix()
    raise TypeError(f"不可序列化类型: {type(obj)!r}")


def _tool_versions() -> Dict[str, str]:
    def _ver(dist: str) -> str:
        try:
            return importlib.metadata.version(dist)
        except Exception:
            return "unknown"
    return {"python": platform.python_version(), "chromadb": _ver("chromadb"),
            "jieba": _ver("jieba")}


# ── 目录：ACTIVE 指针与 build 列表 ──────────────────────────────────────────

def _indexes_dir(root: Optional[Any] = None) -> Path:
    return paths.subdir("indexes", Path(root).resolve() if root else None)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
                   encoding="utf-8")
    os.replace(str(tmp), str(path))


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_meta(build_id: str, root: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """读取单个 build 的 build.json（不存在返回 None）。"""
    p = paths.build_dir(build_id, Path(root).resolve() if root else None) / "build.json"
    return _read_json(p)


def active_build(root: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """当前激活 build 的元数据（ACTIVE.json）；无激活 build 返回 None。"""
    meta = _read_json(_indexes_dir(root) / ACTIVE_NAME)
    if not meta or not meta.get("build_id"):
        return None
    return meta


def list_builds(root: Optional[Any] = None) -> List[Dict[str, Any]]:
    """全部 build 的元数据（按 build_id 升序；读不出 build.json 的目录跳过）。"""
    base = _indexes_dir(root)
    out: List[Dict[str, Any]] = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        meta = _read_json(d / "build.json")
        if meta:
            out.append(meta)
    return out


def activate_build(build_id: str, root: Optional[Any] = None) -> Dict[str, Any]:
    """切换激活指针到指定 build（旧 build 目录与历史指针记录都保留，可回滚）。"""
    base = paths.build_dir(build_id, Path(root).resolve() if root else None)
    meta = _read_json(base / "build.json")
    if meta is None:
        raise ValueError(f"未找到 build 的 build.json：{build_id!r}")
    active = {"build_id": str(build_id), "marker": meta.get("marker", ""),
              "activated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "build_dir": str(base)}
    _write_json(_indexes_dir(root) / ACTIVE_NAME, active)
    history = _indexes_dir(root) / "ACTIVE_history.jsonl"
    with history.open("a", encoding="utf-8") as f:
        f.write(json.dumps(active, ensure_ascii=False) + "\n")
    return active


# ── 退役 / 遗留目录（存储收敛）──────────────────────────────────────────────

def drop_build_collections(build_id: str, root: Optional[Any] = None) -> Dict[str, Any]:
    """删除某 build 的两个 collection（退役/回滚时的向量空间回收）。

    **只删 collection，不删 build.json**：build 的 marker / counts / 上限 / 嵌入配置仍留在
    `indexes/<build_id>/build.json`，可追溯；需要时按同一输入复算（重建）即可。
    向量目录是共享的 `indexes/chroma`，故本函数绝不删除目录本身，只回收该 build 的两条集合。
    """
    base_root = Path(root).resolve() if root else paths.corpus_root()
    from core.chroma_utils import open_local_chroma
    client = open_local_chroma(str(paths.chroma_dir(base_root)))
    dropped: List[str] = []
    missing: List[str] = []
    try:
        for name in (card_collection_name(build_id), body_collection_name(build_id)):
            try:
                client.delete_collection(name)
                dropped.append(name)
            except Exception:
                missing.append(name)
    finally:
        try:
            client.close()
        except Exception:
            pass
    return {"build_id": str(build_id), "dropped": dropped, "missing": missing,
            "chroma_dir": paths.chroma_dir(base_root).as_posix(),
            "build_json_kept": build_meta(build_id, base_root) is not None}


def legacy_chroma_residue(root: Optional[Any] = None) -> List[Dict[str, Any]]:
    """扫描旧实现留下的 indexes/<build_id>/chroma/ 目录（遗留、未迁移、仅占空间）。

    返回每条的 build_id / 路径 / 字节数 / 是否已标记迁移；**不删除**任何残留目录。
    """
    base = _indexes_dir(root)
    out: List[Dict[str, Any]] = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        legacy = d / "chroma"
        if not legacy.exists():
            continue
        nbytes = sum(f.stat().st_size for f in legacy.rglob("*") if f.is_file())
        out.append({"build_id": d.name, "path": legacy.as_posix(), "bytes": nbytes,
                    "migrated_marked": (d / "chroma.migrated.json").exists()})
    return out


def migrate_legacy_chroma(build_id: str, root: Optional[Any] = None) -> Dict[str, Any]:
    """把旧 indexes/<build_id>/chroma/ 的两条 collection 重建进共享 indexes/chroma。

    旧目录**不删除**（仅写 chroma.migrated.json 标记"已迁移"）；返回两条集合的条数。
    嵌入函数按该 build 记录的 embed.mode 重建（与建库同配置）；若 mode 未知则如实标未迁移。
    """
    base_root = Path(root).resolve() if root else paths.corpus_root()
    legacy = paths.build_dir(build_id, base_root) / "chroma"
    meta = build_meta(build_id, base_root) or {}
    result: Dict[str, Any] = {"build_id": str(build_id), "legacy_path": legacy.as_posix(),
                              "migrated": False, "counts": {}, "reason": ""}
    if not legacy.exists():
        result["reason"] = "无遗留 chroma 目录"
        return result
    mode = str((meta.get("embed") or {}).get("mode") or "none")
    if mode not in ("bge", "ngram"):
        result["reason"] = f"build.json 未记录可用嵌入模式（mode={mode}），无法重建向量"
        return result
    ef, _name = embedding_for_mode(mode)
    from core.chroma_utils import open_local_chroma
    src = open_local_chroma(str(legacy))
    dst = open_local_chroma(str(paths.chroma_dir(base_root)))
    counts: Dict[str, int] = {}
    try:
        for key, name in (("cards", card_collection_name(build_id)),
                          ("body", body_collection_name(build_id))):
            try:
                col = src.get_collection(name, embedding_function=ef)
            except Exception:
                counts[key] = 0
                continue
            data = col.get(include=["documents", "metadatas"])
            ids = list(data.get("ids") or [])
            if ids:
                dst.get_or_create_collection(name=name, embedding_function=ef).upsert(
                    ids=ids, documents=list(data.get("documents") or []),
                    metadatas=list(data.get("metadatas") or []))
            counts[key] = len(ids)
    finally:
        for c in (src, dst):
            try:
                c.close()
            except Exception:
                pass
    try:
        (paths.build_dir(build_id, base_root) / "chroma.migrated.json").write_text(
            json.dumps({"build_id": str(build_id), "counts": counts}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass
    result.update({"migrated": True, "counts": counts})
    return result


# ── 主入口：小批构建 ─────────────────────────────────────────────────────────

def _parsed_docs(manifest: CorpusManifest, source_providers: Optional[Sequence[str]]):
    """取"已解析正文"的 versions（parse_status ∈ parsed/low_quality，可含低质量但需标注）。

    行序按 version_id 升序，保证构建确定性（同输入 → 同 build 内容）。
    """
    sources = {s["id"]: s for s in manifest.list_sources()}
    out = []
    for v in sorted(manifest.list_versions(), key=lambda x: str(x.get("version_id", ""))):
        src = sources.get(v.get("source_id"))
        if source_providers and (src is None or src.get("provider") not in source_providers):
            continue
        ps = (src or {}).get("parse_status", "")
        if ps not in ("parsed", "low_quality"):
            continue
        out.append((v, src or {}))
    return out


def _manifest_block_to_parsed(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        locator = json.loads(row.get("locator") or "{}")
    except Exception:
        locator = {}
    return {"content": str(row.get("content") or ""), "locator": locator,
            "kind": "text", "evidence_kind": BODY_KIND}


def build_index(build_id: str, *, root: Optional[Any] = None,
                source_providers: Optional[Sequence[str]] = None,
                max_docs: int = 300, max_blocks: int = 30000, embed: bool = True,
                staging_tag: str = "build") -> Dict[str, Any]:
    """小批构建：目录卡 → 正文向量 → FTS，落 build.json 并返回如实统计。

    两个上限先到哪个就停；`stopped_by` ∈ {"docs","blocks","none"}。
    """
    t0 = time.time()
    base_root = Path(root).resolve() if root else paths.corpus_root()
    bdir = paths.build_dir(build_id, base_root)
    staging = paths.staging_dir(staging_tag, base_root)  # 单批受控临时目录（本轮仅约束）
    manifest = CorpusManifest(paths.manifest_db(base_root))
    # 若该 build 还留有旧实现的独立 fts.sqlite，先一次性并入 manifest 同库（幂等；旧文件留 .legacy）
    try:
        from corpus.fts import legacy_fts_path, migrate_fts_into_manifest
        if legacy_fts_path(build_id, base_root).exists():
            migrate_fts_into_manifest(build_id, base_root)
    except Exception:
        pass
    docs = _parsed_docs(manifest, source_providers)
    n_versions = len(manifest.list_versions())

    ef = None
    mode, ef_name = "none", "none"
    if embed:
        ef, mode, ef_name = embedding_function()

    # 向量目录收敛为单一 indexes/chroma（所有 build 共用；collection 名按 build 区分）
    chroma_dir = paths.chroma_dir(base_root)
    from core.chroma_utils import open_local_chroma
    client = open_local_chroma(str(chroma_dir))
    cards_col = client.get_or_create_collection(
        name=card_collection_name(build_id), metadata={"evidence_kind": CATALOG_KIND},
        embedding_function=ef)
    body_col = client.get_or_create_collection(
        name=body_collection_name(build_id), metadata={"evidence_kind": BODY_KIND},
        embedding_function=ef)

    # FTS 与 manifest 同库（1 个 SQLite）；多个 build 靠 build_id 列区分
    fts = CorpusFTS(db_path=str(paths.manifest_db(base_root)))

    card_ids: List[str] = []
    card_docs: List[str] = []
    card_meta: List[Dict[str, Any]] = []
    body_ids: List[str] = []
    body_docs: List[str] = []
    body_meta: List[Dict[str, Any]] = []

    docs_used = 0
    blocks_used = 0
    low_quality_docs = 0
    docs_without_body = 0
    stopped_by = "none"

    try:
        for v, src in docs:
            if docs_used >= max_docs:
                stopped_by = "docs"
                break
            vid = str(v.get("version_id") or "")
            title = str(v.get("title") or "")
            category = str(v.get("category") or "")
            access_scope = str(v.get("access_scope") or "")
            if src.get("parse_status") == "low_quality":
                low_quality_docs += 1

            body_rows = manifest.list_blocks(vid, evidence_kind=BODY_KIND)
            parsed_blocks = [_manifest_block_to_parsed(r) for r in body_rows if str(r.get("content") or "").strip()]

            # ① 短目录卡（卡与正文分集合；卡绝不含正文全文）
            card = make_catalog_card(
                {"version_id": vid, "title": title, "category": category,
                 "source": src.get("provider", ""), "status": v.get("status", "")},
                {"blocks": parsed_blocks})
            card_ids.append(card["card_id"])
            card_docs.append(card["text"])
            card_meta.append({
                "version_id": vid, "title": title, "category": category,
                "evidence_kind": CATALOG_KIND, "access_scope": access_scope,
                "token_count": int(card.get("token_count") or 0),
                "standard_nos": "|".join(card.get("standard_nos") or []),
                "cas": "|".join(card.get("cas") or []),
                "provider": str(src.get("provider") or ""),
            })

            # ② 正文块（token 计量切块 → 精选正文向量 + FTS 同 build）
            if not parsed_blocks:
                docs_without_body += 1
            chunks: List[Chunk] = chunk_blocks(
                parsed_blocks, max_tokens=CHUNK_MAX_TOKENS,
                target_tokens=CHUNK_TARGET_TOKENS) if parsed_blocks else []
            kept: List[Chunk] = []
            for i, ch in enumerate(chunks):
                if blocks_used >= max_blocks:
                    stopped_by = "blocks"
                    break
                bid = stable_block_id(vid, i, ch.content)
                body_ids.append(bid)
                body_docs.append(ch.content)
                body_meta.append({
                    "block_id": bid, "version_id": vid, "title": title,
                    "category": category, "evidence_kind": BODY_KIND,
                    "access_scope": access_scope, "token_count": int(ch.token_count),
                    "locator": json.dumps(ch.locator, ensure_ascii=False),
                    "kind": ch.kind, "provider": str(src.get("provider") or ""),
                })
                kept.append(ch)
                blocks_used += 1
            if kept or chunks:
                fts.add_blocks(vid, kept, build_id=build_id)

            docs_used += 1
            if stopped_by == "blocks":
                break

        if card_ids:
            cards_col.upsert(ids=card_ids, documents=card_docs, metadatas=card_meta)
        if body_ids:
            body_col.upsert(ids=body_ids, documents=body_docs, metadatas=body_meta)
    finally:
        try:
            fts.close()
        except Exception:
            pass
        try:
            manifest.close()
        except Exception:
            pass

    marker = secrets.token_hex(16)
    dim = 0
    if ef is not None:
        try:
            dim = len(ef.embed_documents(["dimension-probe"])[0])
        except Exception:
            dim = 0
    counts = {"docs": docs_used, "cards": len(card_ids), "blocks": blocks_used,
              "low_quality_docs": low_quality_docs, "docs_without_body": docs_without_body,
              "fts_blocks": len(body_ids), "manifest_versions": n_versions}
    meta: Dict[str, Any] = {
        "build_id": str(build_id),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "marker": marker,
        "embed": {"mode": mode, "name": ef_name, "dim": dim,
                  "max_length": EMBED_MAX_LENGTH, "pooling": "mean", "normalize": True,
                  "query_prefix": "", "build_entry": "BgeEmbeddingFunction.embed_documents"},
        "chunking": {"version": CHUNKING_VERSION, "max_tokens": CHUNK_MAX_TOKENS,
                     "target_tokens": CHUNK_TARGET_TOKENS},
        "counts": counts,
        "limits": {"max_docs": int(max_docs), "max_blocks": int(max_blocks)},
        "stopped_by": stopped_by,
        "source_providers": list(source_providers) if source_providers else "all",
        "collections": {"cards": cards_col.name, "body": body_col.name},
        "chroma_dir": chroma_dir.as_posix(),
        "fts_db": paths.manifest_db(base_root).as_posix(),
        "fts_db_kind": "manifest",
        "fts_build_id": str(build_id),
        "root": base_root.as_posix(),
        "staging": staging.as_posix(),
        "elapsed_s": round(time.time() - t0, 3),
        "tools": _tool_versions(),
    }
    _write_json(bdir / "build.json", meta)
    try:
        client.close()
    except Exception:
        pass
    return meta
