# -*- coding: utf-8 -*-
# K03/K07 corpus 管线（离线为主）回归测试：
# 用法: D:/claudecode/SafetyMind/.venv/Scripts/python.exe tests/test_corpus_pipeline.py
# 覆盖：自有资料导入幂等；来源与采集分母分开（user_imported 不计入 toolbox_acquired；
#       目录层占位不被算作正文）；build 唯一 marker 落盘、ACTIVE 切换与回滚、上限 stopped_by；
#       卡/正文分离（blocks 不含 catalog；命中卡回取正文；无正文版本明确标"仅目录"）；
#       scopes 过滤与 fail-closed（异常注入返回空）；原件访问如实回无原件。
# 铁律 9：全部产物走 scratch 下独立子目录（SAFETYMIND_CORPUS_ROOT），不碰桌面端存储。
import gc
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 共享的存储自检 helper

import _storage_guard as guard   # 共享的"存储自检 + 一次重建重试"（只依赖标准库）

SCRATCH = Path("D:/claudecode/scratch/sm-corpus-20261019")
_ROOT_SEQ = [0]

RESULTS = []


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def fresh_root(tag: str) -> Path:
    _ROOT_SEQ[0] += 1
    SCRATCH.mkdir(parents=True, exist_ok=True)
    root = SCRATCH / f"root_{tag}_{_ROOT_SEQ[0]}"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    os.environ["SAFETYMIND_CORPUS_ROOT"] = str(root)
    os.environ["SAFETYMIND_EMBEDDING"] = "ngram"   # 离线确定性嵌入
    return root


def write_seed(root: Path, records) -> Path:
    p = root / "seed.json"
    p.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return p


def seed_doc(m, item_id, title, content, *, scope="", provider="user_imported",
             parse_status="parsed", acq="parsed", with_blocks=True):
    """直接写 manifest：来源 → 版本 → 正文块（可控 access_scope / 是否有正文）。"""
    sid = m.upsert_source(provider, item_id, title=title,
                          acquisition_status=acq, parse_status=parse_status)
    lg = m.make_logical_doc_id(provider, item_id=item_id)
    vid = m.make_version_id(lg, "v1")
    m.add_version(lg, vid, sid, title, "", access_scope=scope)
    if with_blocks and content:
        m.add_blocks(vid, [{"content": content, "locator": {"para": 1},
                            "evidence_kind": "body"}])
    return vid


# ── 1. 自有资料导入幂等 ─────────────────────────────────────────────────────
def test_import_idempotent():
    from corpus.manifest import CorpusManifest
    from corpus.pipeline import import_user_owned
    root = fresh_root("import")
    seed = write_seed(root, [
        {"title": "动火作业管理规定", "content": "动火票有效期UNIQ-A1，作业前30分钟动火分析。",
         "meta": {"big": "操作技术"}},
        {"title": "受限空间作业规程", "content": "受限空间作业氧含量19.5%~21%。",
         "meta": {"big": "操作技术"}},
    ])
    first = import_user_owned(root, seed_path=seed)
    m = CorpusManifest(root / "manifest.sqlite")
    s1 = m.stats()
    m.close()
    second = import_user_owned(root, seed_path=seed)
    m = CorpusManifest(root / "manifest.sqlite")
    s2 = m.stats()
    m.close()
    record("两次导入来源行数不变", s1["sources"]["total"] == s2["sources"]["total"],
           f"{s1['sources']['total']} vs {s2['sources']['total']}")
    record("两次导入版本数不变", s1["versions"] == s2["versions"], f"{s1['versions']} vs {s2['versions']}")
    record("两次导入正文块数不变", s1["blocks"]["total"] == s2["blocks"]["total"],
           f"{s1['blocks']['total']} vs {s2['blocks']['total']}")
    record("第二次导入新增块数为 0（幂等）", second["added_blocks_this_run"] == 0,
           f"added={second['added_blocks_this_run']}")
    record("user_imported 条数 == 记录数（含唯一 item_id）",
           first["user_imported_sources"] == 2 and first["records"] == 2,
           f"src={first['user_imported_sources']}")


# ── 2. 来源与采集分母分开；目录层占位不算正文 ───────────────────────────────
def test_source_denominator_separation():
    from corpus.manifest import CorpusManifest
    from corpus.pipeline import TOOLBOX_PLACEHOLDER_ID, import_user_owned
    root = fresh_root("denom")
    seed = write_seed(root, [
        {"title": "隐患排查依据-选址", "content": "厂址选择应符合工业布局要求。GB50187-2012。",
         "meta": {"big": "隐患排查"}},
        {"title": "隐患排查依据-布置", "content": "总平面布置应满足防火间距要求。",
         "meta": {"big": "隐患排查"}},
    ])
    imp = import_user_owned(root, seed_path=seed)
    m = CorpusManifest(root / "manifest.sqlite")
    try:
        r_user = m.reconciliation(provider="user_imported")
        r_acq = m.reconciliation(provider="toolbox_acquired")
        r_cat = m.reconciliation(provider="toolbox_catalog")
        stats = m.stats()
        placeholder = [s for s in m.list_sources(provider="toolbox_catalog")
                       if s["item_id"] == TOOLBOX_PLACEHOLDER_ID]
        ph_versions = [v for v in m.list_versions()
                       if placeholder and v["source_id"] == placeholder[0]["id"]]
        ph_blocks = []
        for v in ph_versions:
            ph_blocks.extend(m.list_blocks(str(v["version_id"])))
    finally:
        m.close()

    record("user_imported 计入 verified、不计入 toolbox_acquired",
           r_user["verified"] == 2 and r_acq["expected_leaf_files"] == 0,
           f"user_verified={r_user['verified']} acq_expected={r_acq['expected_leaf_files']}")
    record("来源谱系分开统计（by_provider）",
           stats["sources"]["by_provider"]["user_imported"] == 2
           and stats["sources"]["by_provider"]["toolbox_acquired"] == 0,
           f"by_provider={stats['sources']['by_provider']}")
    record("目录层占位记 catalog_registered（todo 桶）",
           r_cat["by_acquisition_status"].get("catalog_registered", 0) == 1,
           f"cat={r_cat['by_acquisition_status']}")
    record("目录层占位没有版本/正文（不得把目录算成正文）",
           len(ph_versions) == 0 and len(ph_blocks) == 0,
           f"versions={len(ph_versions)} blocks={len(ph_blocks)}")
    record("无原件 → 未声称已取得文件（ladder 未记 file_downloaded_verified）",
           r_user["acquired_files_verified"] == 0,
           f"verified_files={r_user['acquired_files_verified']}")
    record("导入返回中记录 seed 与说明", "seed" in imp and "用户自行加入" in imp["note"])


# ── 3. build 唯一 marker、ACTIVE 切换与回滚 ─────────────────────────────────
def test_build_marker_and_activation():
    from corpus.build import activate_build, active_build, build_meta, list_builds
    from corpus.manifest import CorpusManifest
    root = fresh_root("build")
    m = CorpusManifest(root / "manifest.sqlite")
    try:
        seed_doc(m, "d1", "动火作业票管理", "动火票有效期UNIQ-B1不超过72小时。")
        seed_doc(m, "d2", "受限空间作业管理", "受限空间先通风再检测后作业。")
    finally:
        m.close()
    # 自检存储可用后再看 marker/切换/回滚（自检不过会换全新目录重建一次）
    meta1 = guard.build_index_checked("b1", root=root, probe="UNIQ-B1")
    meta2 = guard.build_index_checked("b2", root=root, probe="UNIQ-B1")
    record("build.json 落唯一 marker（两次 build marker 不同）",
           bool(meta1["marker"]) and meta1["marker"] != meta2["marker"],
           f"{meta1['marker'][:8]} vs {meta2['marker'][:8]}")
    record("build.json 落嵌入配置与分块版本",
           meta1["embed"]["mode"] == "ngram" and meta1["embed"]["dim"] == 256
           and meta1["chunking"]["max_tokens"] == 220,
           f"embed={meta1['embed']} chunk={meta1['chunking']}")
    record("build.json 落依赖工具版本与耗时",
           bool(meta1["tools"].get("chromadb")) and meta1["elapsed_s"] >= 0,
           f"tools={meta1['tools']}")
    activate_build("b2", root)
    record("ACTIVE 指针指向 b2", (active_build(root) or {}).get("build_id") == "b2")
    activate_build("b1", root)
    record("回滚：ACTIVE 指针切回 b1（旧 build 目录保留）",
           (active_build(root) or {}).get("build_id") == "b1" and build_meta("b2", root) is not None)
    record("build 列表保留两个 build", len([b for b in list_builds(root)]) == 2,
           f"builds={[b['build_id'] for b in list_builds(root)]}")


# ── 4. 上限 stopped_by ──────────────────────────────────────────────────────
def test_build_limits_stopped_by():
    from corpus.manifest import CorpusManifest
    # 场景 A：超文档上限
    root = fresh_root("limit_docs")
    m = CorpusManifest(root / "manifest.sqlite")
    try:
        for i in range(5):
            seed_doc(m, f"doc{i}", f"资料{i}", f"第{i}份资料的正文内容UNIQ-L{i}。")
    finally:
        m.close()
    ma = guard.build_index_checked("bx", root=root, max_docs=2, probe="UNIQ-L0")
    record("超文档上限：stopped_by=docs", ma["stopped_by"] == "docs",
           f"stopped_by={ma['stopped_by']}")
    record("超文档上限：docs 计数 == max_docs", ma["counts"]["docs"] == 2,
           f"docs={ma['counts']['docs']}")

    # 场景 B：超块上限（单文档切出多块）
    root2 = fresh_root("limit_blocks")
    m2 = CorpusManifest(root2 / "manifest.sqlite")
    try:
        long_text = "动火作业前必须完成风险辨识与安全交底并确认措施落实。 " * 60
        seed_doc(m2, "long1", "长规程", long_text)
    finally:
        m2.close()
    mb = guard.build_index_checked("by", root=root2, max_blocks=3,
                                   probe="动火作业前必须完成风险辨识")
    record("超块上限：stopped_by=blocks", mb["stopped_by"] == "blocks",
           f"stopped_by={mb['stopped_by']}")
    record("超块上限：blocks 计数 == max_blocks", mb["counts"]["blocks"] == 3,
           f"blocks={mb['counts']['blocks']}")
    record("未触达上限时 stopped_by=none（对照）", ma["limits"]["max_blocks"] == 30000)


# ── 5. 卡/正文分离与定位回取 ────────────────────────────────────────────────
def test_catalog_body_separation_and_backfetch():
    from corpus.build import activate_build
    from corpus.manifest import CorpusManifest
    from corpus.search import CorpusSearch
    root = fresh_root("card")
    m = CorpusManifest(root / "manifest.sqlite")
    try:
        seed_doc(m, "body1", "动火作业安全管理规定",
                 "动火票有效期UNIQ-C1特殊动火不超过8小时，监护人不得离开现场。")
        # 已解析但无可回取正文的版本（仅目录）
        seed_doc(m, "cat1", "应急预案目录条目UNIQ-C2", "", with_blocks=False)
    finally:
        m.close()
    guard.build_index_checked("bc", root=root, probe="UNIQ-C1")
    activate_build("bc", root)
    cs = CorpusSearch()
    try:
        res = cs.search("动火票有效期UNIQ-C1", top_k=5)
        record("search 的 blocks 全为 evidence_kind=body（不出现 catalog）",
               all(b["evidence_kind"] == "body" for b in res["blocks"]),
               f"kinds={[b['evidence_kind'] for b in res['blocks']]}")
        record("正文命中进入 blocks（UNIQ-C1 可见）",
               any("UNIQ-C1" in b["content"] for b in res["blocks"]),
               f"n={len(res['blocks'])}")
        hit = next((c for c in res["cards"] if c.get("backfetched_block_ids")), None)
        record("命中目录卡会回取实际正文（backfetched_block_ids 非空）", hit is not None,
               f"cards={[(c['title'], c['catalog_only']) for c in res['cards']]}")

        res2 = cs.search("应急预案目录条目UNIQ-C2", top_k=5)
        cat_only = [c for c in res2["cards"] if c["catalog_only"]]
        record("无正文版本命中卡时明确标'仅目录'",
               bool(cat_only) and "仅目录" in cat_only[0]["note"],
               f"cards={[(c['title'], c['catalog_only'], c['note'][:12]) for c in res2['cards']]}")
        record("'仅目录'条目不以正文形式进入 blocks",
               all(b["evidence_kind"] == "body" for b in res2["blocks"])
               and not any("UNIQ-C2" in b["content"] for b in res2["blocks"]),
               f"blocks={[b['content'][:16] for b in res2['blocks']]}")
    finally:
        cs.close()


# ── 6. scopes 过滤与 fail-closed ────────────────────────────────────────────
def test_scope_filter_and_failclosed():
    from corpus.build import activate_build
    from corpus.manifest import CorpusManifest
    from corpus.search import CorpusSearch
    root = fresh_root("scope")
    m = CorpusManifest(root / "manifest.sqlite")
    try:
        seed_doc(m, "a1", "甲厂制度", "甲厂条款UNIQ-SCOPE-A 的作业要求。", scope="plant-a")
        seed_doc(m, "b1", "乙厂制度", "乙厂条款UNIQ-SCOPE-B 的作业要求。", scope="plant-b")
        seed_doc(m, "p1", "公共须知", "公共条款UNIQ-SCOPE-PUB 适用于所有厂区。", scope="")
    finally:
        m.close()
    guard.build_index_checked("bs", root=root, probe="UNIQ-SCOPE-A")
    activate_build("bs", root)

    cs = CorpusSearch()
    try:
        all_res = cs.search("条款UNIQ-SCOPE", top_k=8, scopes=None)
        blob_all = " ".join(b["content"] for b in all_res["blocks"])
        record("scopes=None 不过滤（三份都可见）",
               all(k in blob_all for k in ("UNIQ-SCOPE-A", "UNIQ-SCOPE-B", "UNIQ-SCOPE-PUB")),
               f"blob={blob_all[:120]}")

        r_a = cs.search("条款UNIQ-SCOPE", top_k=8, scopes=["plant-a"])
        blob_a = " ".join(b["content"] for b in r_a["blocks"])
        record("给 scopes 时只出本范围与公共（绝不出现他人范围正文）",
               "UNIQ-SCOPE-A" in blob_a and "UNIQ-SCOPE-PUB" in blob_a
               and "UNIQ-SCOPE-B" not in blob_a,
               f"blob={blob_a[:160]}")
    finally:
        cs.close()

    # fail-closed：过滤环节异常注入 → 返回空
    cs2 = CorpusSearch()
    def _boom(_vid):
        raise RuntimeError("injected scope failure")
    cs2._scope_of = _boom
    try:
        r = cs2.search("条款UNIQ-SCOPE", top_k=8, scopes=["plant-a"])
        record("过滤无法生效时 fail-closed 返回空（filter_failed=True）",
               r["filter_failed"] is True and r["blocks"] == [] and r["cards"] == [],
               f"blocks={len(r['blocks'])} cards={len(r['cards'])}")
    finally:
        cs2.close()


# ── 7. 原件访问如实回未取得 ─────────────────────────────────────────────────
def test_open_asset_no_original():
    from corpus.manifest import CorpusManifest
    from corpus.pipeline import import_user_owned
    from corpus.search import open_asset
    root = fresh_root("asset")
    seed = write_seed(root, [{"title": "自有资料一", "content": "自有资料正文UNIQ-AS1。",
                              "meta": {"big": "操作技术"}}])
    import_user_owned(root, seed_path=seed)
    m = CorpusManifest(root / "manifest.sqlite")
    try:
        item = m.list_sources(provider="user_imported")[0]["item_id"]
    finally:
        m.close()
    info = open_asset(item)
    record("无原件条目如实回 found=False / is_materialized=False",
           info["found"] is False and info["is_materialized"] is False,
           f"info={info}")
    record("无原件时回阻塞原因（不得当作已取得）",
           "无原件" in info["blocking_reason"], f"reason={info['blocking_reason']}")
    record("open_asset 不返回正文内容（不把正文当证据）",
           "content" not in info and "UNIQ-AS1" not in json.dumps(info, ensure_ascii=False))


def _cleanup():
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    gc.collect()
    for d in sorted(SCRATCH.glob("root_*"), reverse=True):
        if not guard.rmtree_retry(d):
            print(f"[WARN] 临时目录残留未清干净（句柄未释放？）: {d}", flush=True)


if __name__ == "__main__":
    os.environ["SAFETYMIND_EMBEDDING"] = "ngram"
    fns = [test_import_idempotent, test_source_denominator_separation,
           test_build_marker_and_activation, test_build_limits_stopped_by,
           test_catalog_body_separation_and_backfetch,
           test_scope_filter_and_failclosed, test_open_asset_no_original]
    for fn in fns:
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {type(ex).__name__}: {ex}")
    _cleanup()
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} PASSED")
    sys.exit(0 if passed == total else 1)
