# -*- coding: utf-8 -*-
"""知识库/资料库测试的"存储健康自检 + 一次重建重试"（范式来自 tests/test_identity_isolation.py）。

背景：本机 ChromaDB 1.5.9（Rust）在 Windows 上偶发持久 HNSW 段不可读
（`Error creating hnsw segment reader: Nothing found on disk`）：全新唯一目录、
单进程单客户端、无任何人为破坏时约 1/70 会出现。这是**环境级抖动**，不是被测代码的
缺陷——只有先确认"存储确实可用"，知识库用例度量到的才是产品行为而不是磁盘抖动。

范式：
  1) 播种后立刻自检（doc_count 等于预期 + 一次不带 scopes 的检索能命中探针串）；
  2) 自检不过 → 释放旧句柄 → **换全新唯一目录重建一次**（只重建一次）；
  3) 重建后仍不过 → 如实把不可用状态交给调用方（断言照常执行、照常 FAIL）；
  4) 结束显式释放句柄（删集合/关客户端/清共享系统缓存/gc）并清理目录。

本模块只做"度量前的环境体检"，不改产品代码、不改任何断言语义、不跳过任何断言。
"""
import gc
import os
import shutil
import time
from pathlib import Path

TAG_PRIMARY = "primary"
TAG_REBUILD = "rebuild"

_SEQ = [0]   # 进程内序号：保证本次运行内每个目录都是全新唯一的（不与仍被其它实例占用的目录重名）


def fresh_dir(base, name, tag=None):
    """取一个全新唯一目录：进程内序号 + 进程号保证"本进程内绝不复用"。

    同名残留（上次崩溃留下）先清理并断言为空后再建；残留在本进程内不可能出现
    （名字唯一），因此清理只针对历史运行的遗留目录。
    复用/残留目录会放大上面的偶发故障，故每次（含重建）都用全新目录。返回 Path。
    """
    _SEQ[0] += 1
    suffix = f"_{tag}" if tag else ""
    d = Path(base) / f"{name}_{os.getpid()}_{_SEQ[0]}{suffix}"
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)
    if d.exists():
        raise RuntimeError(f"临时目录残留未清理干净: {d}")
    d.mkdir(parents=True)
    return d


def _clear_system_cache():
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
        SharedSystemClient.clear_system_cache()
    except Exception:
        pass


def rmtree_retry(path, attempts=4, delay=0.25):
    """删除目录：Windows 上 chroma 句柄延迟释放会拖住删除，失败则重试数次。

    返回是否删干净。注意：chroma 1.5 的 Rust 侧会一直持有**本进程最近打开**的持久库
    目录句柄（close() 也不放），该目录往往只剩空壳且直到进程退出才可删；调用方据此
    打印 WARN 即可（下次运行的清理会扫掉它），这不是用例失败。
    """
    p = Path(path)
    for _ in range(max(1, int(attempts))):
        shutil.rmtree(str(p), ignore_errors=True)
        if not p.exists():
            return True
        gc.collect()
        time.sleep(delay)
    return not p.exists()


def release_kb(kb, drop_collections=True):
    """显式释放 Chroma 客户端句柄：删集合 → 关客户端 → 清共享系统缓存 → gc。

    Windows 下句柄未释放时目录删不掉（已复现 WinError 32/5），也会让后续打开同一
    目录读到不一致状态；异常路径同样要走到这里。
    drop_collections=False 时保留集合数据（供同一进程后续实例复用已自检过的存储）。
    """
    if kb is None:
        return
    try:
        if drop_collections:
            for name in (getattr(kb._collection, "name", None), getattr(kb._parents, "name", None)):
                if not name:
                    continue
                try:
                    kb._client.delete_collection(name)
                except Exception:
                    pass
        kb._client.close()
    except Exception:
        pass
    _clear_system_cache()
    gc.collect()


def kb_storage_healthy(kb, base, expected, probe):
    """播种后自检存储真的可用 —— 度量的是产品行为，而不是磁盘抖动。

    1) 走本机（非外部服务）且真实子块数 == 播种前 + 本次导入片段数；
    2) 不带 scopes 的检索必须命中探针串（段文件不可读时 query 必失败）。
    返回 (ok, why)。
    """
    if getattr(kb, "_use_server", False):
        return False, f"意外连上外部 ChromaDB 服务（{kb._client}），度量对象不是本地临时目录"
    want = int(base) + int(expected)
    got = kb.doc_count
    if got != want:
        return False, f"doc_count={got} != 预期 {want}"
    try:
        hits = kb.search(probe, top_k=5)
    except Exception as ex:
        return False, f"无 scopes 自检检索异常 {type(ex).__name__}: {str(ex)[:120]}"
    if not any(probe in (str(h.get("snippet") or "") + str(h.get("content") or "")) for h in hits):
        return False, f"无 scopes 自检检索命中 {len(hits)} 条，但无 {probe}"
    return True, ""


def build_kb_checked(tmp_root, factory, seed=None, probe="", name="kb_chroma"):
    """全新目录建库 + 播种 + 自检；不可用则换**全新目录**重建一次（最多一次）。

    factory(chroma_dir) -> KnowledgeBase（可自行清掉 __init__ 自动播种的默认文档）；
    seed(kb) 播种并返回本次新增片段数（expected）；probe 为自检检索串。
    返回 (kb, 实际使用的目录 Path)。

    两次都失败时：构造/播种异常 → 抛出最后一次异常（如实 FAIL）；
    自检不过 → 返回最后一次实例并打印 WARN，让调用方断言如实反映。
    """
    last_ex = None
    kb = None
    used = None
    for tag in (TAG_PRIMARY, TAG_REBUILD):
        if kb is not None:            # 上一个实例（含其目录句柄）先彻底释放
            release_kb(kb)
            kb = None
        d = fresh_dir(tmp_root, name, tag=tag)
        try:
            kb = factory(d)
            base = kb.doc_count
            expected = int(seed(kb)) if seed is not None else 0
        except Exception as ex:
            last_ex = ex
            print(f"[WARN] chroma 存储不可用（{d.name}）：构造/播种失败 "
                  f"{type(ex).__name__}: {str(ex)[:160]}", flush=True)
            # 异常路径同样要释放句柄：构造成功但播种失败时客户端已经打开
            release_kb(kb)
            kb = None
            continue
        ok, why = kb_storage_healthy(kb, base, expected, probe)
        if ok:
            if tag == TAG_REBUILD:
                print(f"[WARN] 存储自检失败后已在全新目录重建知识库：{d.name}", flush=True)
            return kb, d
        print(f"[WARN] 存储自检失败（{d.name}）：{why} → 换全新目录重建一次", flush=True)
        if tag == TAG_REBUILD:
            print("[WARN] 重建一次后存储自检仍不可用；后续断言将如实反映该状态", flush=True)
            return kb, d
        used = d
    if last_ex is not None:
        raise last_ex
    return kb, used


# ── corpus build（索引卡/正文两条集合）──────────────────────────────────────

def build_storage_healthy(meta, probe=None):
    """corpus build 的存储自检：卡/正文集合计数与 build.json 一致，且两条集合都真的能查。

    段文件不可读时集合 query 必失败（1/70 故障在 corpus 侧的表现形式）；卡与正文是
    两个独立集合、两条独立 HNSW 段，只查正文会漏掉卡的段故障，而 CorpusSearch 两路
    都会查 → 两条都必须验证（正文额外要求命中探针串）。
    """
    cdir = str((meta or {}).get("chroma_dir") or "")
    if not cdir:
        return False, "build.json 缺 chroma_dir"
    try:
        from core.chroma_utils import open_local_chroma
        client = open_local_chroma(cdir)
    except Exception as ex:
        return False, f"打开本地 chroma 失败 {type(ex).__name__}: {str(ex)[:120]}"
    try:
        cols = meta.get("collections") or {}
        counts = meta.get("counts") or {}
        want = {"cards": int(counts.get("cards") or 0), "blocks": int(counts.get("blocks") or 0)}
        mode = str((meta.get("embed") or {}).get("mode") or "none")
        ef = None
        if mode in ("bge", "ngram"):
            from corpus.build import embedding_for_mode
            ef, _name = embedding_for_mode(mode)
        cards = client.get_collection(str(cols.get("cards") or ""), embedding_function=ef)
        body = client.get_collection(str(cols.get("body") or ""), embedding_function=ef)
        got = {"cards": cards.count(), "blocks": body.count()}
        if got != want:
            return False, f"集合计数 {got} != 预期 {want}"
        if probe:
            cards.query(query_texts=[probe], n_results=5)   # 卡的段不可读时此处必抛
            res = body.query(query_texts=[probe], n_results=5)
            docs = (res.get("documents") or [[]])[0] or []
            if not any(probe in str(d or "") for d in docs):
                return False, f"正文集合自检检索命中 {len(docs)} 条，但无 {probe}"
        return True, ""
    except Exception as ex:
        return False, f"集合自检异常 {type(ex).__name__}: {str(ex)[:120]}"
    finally:
        try:
            client.close()
        except Exception:
            pass
        _clear_system_cache()
        gc.collect()


def build_index_checked(build_id, *, root, probe=None, **kw):
    """build_index + 存储自检；自检不过则回收该 build 的集合**重建一次**（最多一次）。

    存储收敛后向量落共享目录 indexes/chroma，不再每 build 一份目录；故重建时改为
    `drop_build_collections(build_id, root)` 删掉该 build 的两条集合（等价"换全新集合"），
    再删 build 目录（build.json 等）；两次都失败时：构建异常 → 抛出最后一次异常；
    自检不过 → 返回最后一次 meta 并打印 WARN，让调用方断言如实反映。
    """
    from corpus import paths
    from corpus.build import build_index

    meta = None
    last_ex = None
    for tag in (TAG_PRIMARY, TAG_REBUILD):
        if tag == TAG_REBUILD:
            try:
                from corpus.build import drop_build_collections
                drop_build_collections(build_id, root)
            except Exception:
                pass
            shutil.rmtree(str(paths.build_dir(build_id, root)), ignore_errors=True)
        try:
            meta = build_index(build_id, root=root, **kw)
        except Exception as ex:
            last_ex = ex
            print(f"[WARN] corpus build 存储不可用（{build_id}/{tag}）：构建失败 "
                  f"{type(ex).__name__}: {str(ex)[:160]}", flush=True)
            continue
        ok, why = build_storage_healthy(meta, probe)
        if ok:
            if tag == TAG_REBUILD:
                print(f"[WARN] build 存储自检失败后已在全新目录重建：{build_id}", flush=True)
            return meta
        print(f"[WARN] build 存储自检失败（{build_id}/{tag}）：{why}", flush=True)
        if tag == TAG_REBUILD:
            print("[WARN] 重建一次后 build 存储自检仍不可用；后续断言将如实反映该状态", flush=True)
            return meta
    if last_ex is not None:
        raise last_ex
    return meta
