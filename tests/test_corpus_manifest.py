# -*- coding: utf-8 -*-
# K02 corpus.manifest 契约/状态机/去重对账 回归测试（standalone，零依赖 venv 外库）：
# 用法: D:/claudecode/SafetyMind/.venv/Scripts/python.exe tests/test_corpus_manifest.py
# 覆盖方案 §5「最低契约」与 §9「状态与对账」：
#   幂等 upsert/link_asset/add_blocks/register_asset；
#   同名不同源/版共存（文档 ID 不得只按 title 生成，用户自有不被覆盖）；
#   同内容共享 asset 但来源谱系与分母不合并；
#   状态机枚举 fail-loud 且 acquisition_status 与索引台阶/parse_status 不串用；
#   台阶纪律（链接不得冒充已取得文件）；分母恒等式与 catalog_only 口径；
#   目录卡与正文分集合；非破坏迁移；铁律 7（非 D 盘直接抛错）。
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


SCRATCH = Path("D:/claudecode/scratch/sm-corpus-20261019")  # 只在 D 盘落临时产物（铁律 7）
_TMPS = []


def tmp_dir(prefix):
    SCRATCH.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=str(SCRATCH)))
    _TMPS.append(d)
    return d


def set_root(root):
    """把 SAFETYMIND_CORPUS_ROOT 指到独立临时子目录，返回旧值以便还原。"""
    prev = os.environ.get("SAFETYMIND_CORPUS_ROOT")
    os.environ["SAFETYMIND_CORPUS_ROOT"] = str(root)
    return prev


def restore_root(prev):
    if prev is None:
        os.environ.pop("SAFETYMIND_CORPUS_ROOT", None)
    else:
        os.environ["SAFETYMIND_CORPUS_ROOT"] = prev


def close_quiet(obj):
    try:
        obj.close()
    except Exception:
        pass


def raises_value_error(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


def count_original_files(tmp):
    base = tmp / "originals" / "sha256"
    return sum(1 for p in base.rglob("*") if p.is_file()) if base.exists() else 0


def new_manifest():
    """在临时根目录上建 manifest（env 指向临时目录，避免污染真实资料根）。"""
    from corpus.manifest import CorpusManifest
    tmp = tmp_dir("manifest_")
    prev = set_root(tmp)
    return CorpusManifest(), tmp, prev


# ── 1. 幂等 ────────────────────────────────────────────────────────────────
def test_idempotency():
    m, tmp, prev = new_manifest()
    try:
        sid = m.upsert_source("toolbox_catalog", "idem-1", title="动火作业管理规定")
        sid2 = m.upsert_source("toolbox_catalog", "idem-1", title="动火作业管理规定")
        n_src = len(m.list_sources())
        record("upsert_source 同(provider,item_id) 不新增行",
               sid == sid2 and n_src == 1, f"sid={sid}/{sid2} n={n_src}")

        asset = m.register_asset(b"identical-content-bytes-for-idempotency")
        m.link_asset("idem-1", asset)
        m.link_asset("idem-1", asset)
        record("link_asset 重复不新增映射行",
               len(m.list_mappings("idem-1")) == 1, f"n={len(m.list_mappings('idem-1'))}")

        vid = "VID-idem"
        blocks = [{"content": "第一条正文", "locator": {"para": 1}},
                  {"content": "第二条正文", "locator": {"para": 2}}]
        first = m.add_blocks(vid, blocks)
        second = m.add_blocks(vid, blocks)
        record("同 version 重复 add_blocks 第二次返回 0",
               first == 2 and second == 0, f"first={first} second={second}")

        m.register_asset(b"identical-content-bytes-for-idempotency")
        n_files = count_original_files(tmp)
        record("register_asset 同内容不重复落盘（原件唯一）", n_files == 1, f"files={n_files}")
    finally:
        close_quiet(m)
        restore_root(prev)
        shutil.rmtree(tmp, ignore_errors=True)


# ── 2. 同名不同源/版共存 ────────────────────────────────────────────────────
def test_same_title_coexistence():
    m, tmp, prev = new_manifest()
    try:
        ld_user = m.make_logical_doc_id("user_imported", item_id="u-1")
        ld_tb = m.make_logical_doc_id("toolbox_catalog", item_id="tb-1")
        record("同名不同源 logical_doc_id 不同", ld_user != ld_tb,
               f"{ld_user[:8]} vs {ld_tb[:8]}")
        ld_v24 = m.make_logical_doc_id("toolbox_catalog", title="应急预案", version_key="2024")
        ld_v26 = m.make_logical_doc_id("toolbox_catalog", title="应急预案", version_key="2026")
        record("同名不同版 logical_doc_id 不同", ld_v24 != ld_v26,
               f"{ld_v24[:8]} vs {ld_v26[:8]}")

        m.add_version(ld_user, m.make_version_id(ld_user, "v1"), None, "安全检查表", "")
        m.add_version(ld_tb, m.make_version_id(ld_tb, "v1"), None, "安全检查表", "")
        record("两条同名版本都能查到",
               len(m.list_versions(ld_user)) == 1 and len(m.list_versions(ld_tb)) == 1
               and len(m.list_versions()) == 2,
               f"n_user={len(m.list_versions(ld_user))} n_tb={len(m.list_versions(ld_tb))}")

        m.upsert_source("user_imported", "u-1", title="安全检查表", summary="用户自有的检查表")
        m.upsert_source("toolbox_catalog", "tb-1", title="安全检查表", summary="百宝箱同名条目")
        user_rows = m.list_sources(provider="user_imported")
        record("用户自有同名条目仍在且未被覆盖",
               len(user_rows) == 1 and user_rows[0]["title"] == "安全检查表"
               and user_rows[0]["summary"] == "用户自有的检查表",
               f"user={[(r['title'], r['summary']) for r in user_rows]}")
        record("同名不同源两条来源共存", len(m.list_sources()) == 2,
               f"n={len(m.list_sources())}")
    finally:
        close_quiet(m)
        restore_root(prev)
        shutil.rmtree(tmp, ignore_errors=True)


# ── 3. 内容去重与谱系分离 ───────────────────────────────────────────────────
def test_content_dedupe_lineage_separation():
    m, tmp, prev = new_manifest()
    try:
        content = b"%PDF-1.4 identical bytes shared by two different sources"
        a1 = m.register_asset(content, mime="application/pdf")
        a2 = m.register_asset(content, mime="application/pdf")
        m.upsert_source("toolbox_acquired", "A-1", title="甲来源")
        m.upsert_source("other_verified_source", "B-1", title="乙来源")
        m.link_asset("A-1", a1)
        m.link_asset("B-1", a2)
        record("不同来源同内容 → 同一 asset_id",
               a1 == a2 and m.stats()["assets"]["total"] == 1,
               f"a1==a2:{a1 == a2} assets={m.stats()['assets']['total']}")
        record("谱系分离：sources 仍两条", len(m.list_sources()) == 2,
               f"n={len(m.list_sources())}")
        record("谱系分离：映射仍两条", len(m.list_mappings()) == 2,
               f"n={len(m.list_mappings())}")
    finally:
        close_quiet(m)
        restore_root(prev)
        shutil.rmtree(tmp, ignore_errors=True)


# ── 4. 状态机枚举校验 ───────────────────────────────────────────────────────
def test_state_machine_enum():
    m, tmp, prev = new_manifest()
    try:
        m.upsert_source("toolbox_catalog", "sm-1", title="状态机样例")
        m.set_status("sm-1", acquisition_status="acquired_verified", parse_status="parsed")
        record("合法状态可写",
               m.list_sources()[0]["acquisition_status"] == "acquired_verified"
               and m.list_sources()[0]["parse_status"] == "parsed")

        record("非法 acquisition_status → ValueError",
               raises_value_error(lambda: m.set_status("sm-1", acquisition_status="banana")))
        record("非法 parse_status → ValueError",
               raises_value_error(lambda: m.set_status("sm-1", parse_status="banana")))
        record("acquisition_status 不得写成索引台阶取值",
               raises_value_error(lambda: m.set_status("sm-1", acquisition_status="fts_indexed")))
        record("acquisition_status 不得写成 parse_status 取值",
               raises_value_error(lambda: m.set_status("sm-1", acquisition_status="low_quality")))
        record("非法 ladder 台阶 → ValueError",
               raises_value_error(lambda: m.set_status("sm-1", ladder=["share_opened"])))
        record("非法索引台阶键 → ValueError",
               raises_value_error(lambda: m.set_status("sm-1", bogus_flag=1)))
        record("upsert_source 非法 provider → ValueError",
               raises_value_error(lambda: m.upsert_source("not_a_provider", "x")))
        record("非法写入后来源状态未被污染",
               m.list_sources()[0]["acquisition_status"] == "acquired_verified"
               and m.list_sources()[0]["parse_status"] == "parsed")
    finally:
        close_quiet(m)
        restore_root(prev)
        shutil.rmtree(tmp, ignore_errors=True)


# ── 5. 台阶纪律 ─────────────────────────────────────────────────────────────
def test_ladder_discipline():
    m, tmp, prev = new_manifest()
    try:
        m.upsert_source("toolbox_catalog", "lad-1", title="台阶样例")
        m.set_status("lad-1", ladder=["link_exported"])
        r1 = m.reconciliation(provider="toolbox_catalog")
        record("只置 link_exported 不计入已取得文件",
               r1["acquired_files_verified"] == 0 and r1["verified"] == 0,
               f"verified={r1['acquired_files_verified']} bucket={r1['verified']}")
        record("link_exported 台阶逐级计数",
               r1["acquired_files"]["link_exported"] == 1,
               f"ladder={r1['acquired_files']}")

        m.set_status("lad-1", ladder=["file_downloaded_verified"])
        r2 = m.reconciliation(provider="toolbox_catalog")
        record("置 file_downloaded_verified 后才计入已取得文件",
               r2["acquired_files_verified"] == 1,
               f"verified={r2['acquired_files_verified']}")
        record("台阶合并保留历史等级",
               r2["acquired_files"]["link_exported"] == 1
               and r2["acquired_files"]["file_downloaded_verified"] == 1,
               f"ladder={r2['acquired_files']}")
    finally:
        close_quiet(m)
        restore_root(prev)
        shutil.rmtree(tmp, ignore_errors=True)


# ── 6. 分母对账 ─────────────────────────────────────────────────────────────
def test_denominator_reconciliation():
    m, tmp, prev = new_manifest()
    try:
        spec = [
            ("pending", "todo"),
            ("catalog_registered", "todo"),
            ("acquired_verified", "verified"),
            ("duplicate_confirmed", "duplicate_mapping"),
            ("irrelevant_excluded", "excluded_with_reason"),
            ("invalid_access_blocked", "blocked"),
        ]
        for i, (st, _bucket) in enumerate(spec):
            m.upsert_source("toolbox_catalog", f"rec-{i}", title=f"对账{i}",
                            acquisition_status=st)
        r = m.reconciliation()
        lhs = r["expected_leaf_files"]
        rhs = (r["verified"] + r["duplicate_mapping"] + r["excluded_with_reason"]
               + r["todo"] + r["blocked"])
        record("分母恒等式：预期叶文件 == 各桶之和",
               lhs == rhs == 6, f"lhs={lhs} rhs={rhs}")
        buckets = (r["verified"], r["duplicate_mapping"], r["excluded_with_reason"],
                   r["todo"], r["blocked"])
        record("各桶计数正确", buckets == (1, 1, 1, 2, 1), f"buckets={buckets}")
        record("未提供文件清单时 scope=catalog_only", r["scope"] == "catalog_only",
               f"scope={r['scope']}")
        rf = m.reconciliation(scope="files")
        record("显式 files 口径如实声明",
               rf["scope"] == "files" and rf["expected_leaf_files"] == 6,
               f"scope={rf['scope']} expected={rf['expected_leaf_files']}")
        record("非法 scope → ValueError",
               raises_value_error(lambda: m.reconciliation(scope="bogus")))
    finally:
        close_quiet(m)
        restore_root(prev)
        shutil.rmtree(tmp, ignore_errors=True)


# ── 7. 目录卡与正文分离 ─────────────────────────────────────────────────────
def test_catalog_card_body_separation():
    m, tmp, prev = new_manifest()
    try:
        vid = "CATV-1"
        n = m.add_blocks(vid, [
            {"content": "正文块一：动火票有效期", "locator": {"para": 1}},
            {"content": "正文块二：监护人要求", "locator": {"para": 2}},
            {"content": "目录卡摘要：标题+章节", "locator": {"card": 1},
             "evidence_kind": "catalog"},
        ])
        bodies = m.list_blocks(vid, evidence_kind="body")
        cards = m.list_blocks(vid, evidence_kind="catalog")
        allb = m.list_blocks(vid)
        record("目录卡与正文分集合写入", n == 3 and len(allb) == 3,
               f"added={n} total={len(allb)}")
        record("按 body 过滤不包含目录卡",
               len(bodies) == 2 and all(b["evidence_kind"] == "body" for b in bodies)
               and all("目录卡" not in b["content"] for b in bodies),
               f"bodies={[b['content'] for b in bodies]}")
        record("按 catalog 过滤只含目录卡",
               len(cards) == 1 and cards[0]["evidence_kind"] == "catalog",
               f"cards={[c['content'] for c in cards]}")
    finally:
        close_quiet(m)
        restore_root(prev)
        shutil.rmtree(tmp, ignore_errors=True)


# ── 8. 迁移非破坏 ───────────────────────────────────────────────────────────
def test_non_destructive_migration():
    tmp = tmp_dir("mig_")
    db = tmp / "manifest.sqlite"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " provider TEXT NOT NULL DEFAULT '', item_id TEXT NOT NULL DEFAULT '',"
        " title TEXT NOT NULL DEFAULT '')")
    con.execute(
        "INSERT INTO sources (provider, item_id, title)"
        " VALUES ('toolbox_catalog', 'old-1', '迁移前的旧条目')")
    con.commit()
    con.close()

    prev = set_root(tmp)
    from corpus.manifest import CorpusManifest
    m = CorpusManifest(db)
    ok_data = False
    try:
        rows = m.list_sources()
        ok_data = (len(rows) == 1 and rows[0]["title"] == "迁移前的旧条目"
                   and rows[0]["provider"] == "toolbox_catalog")
    finally:
        close_quiet(m)

    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(sources)")}
    row = con.execute(
        "SELECT provider, item_id, title, acquisition_status FROM sources").fetchone()
    con.close()
    restore_root(prev)

    record("旧库既有数据在迁移后仍在", ok_data, f"ok_data={ok_data}")
    record("迁移补列：缺列被补齐且默认值 / 既有行保留",
           {"unknown_fields", "acquisition_status", "parse_status", "ladder"} <= cols
           and row is not None and row[2] == "迁移前的旧条目"
           and row[3] == "catalog_registered",
           f"cols={sorted(cols)} row={row}")
    shutil.rmtree(tmp, ignore_errors=True)


# ── 9. 路径纪律（铁律 7）────────────────────────────────────────────────────
def test_path_discipline_blocks_non_d_drive():
    from corpus.manifest import CorpusManifest
    probe = Path("C:/tmp/sm-corpus-must-not-write-20261019")
    prev = set_root(probe)
    raised = False
    try:
        CorpusManifest()
    except ValueError:
        raised = True
    finally:
        restore_root(prev)
    record("非 D 盘资料根目录直接抛错（铁律 7）", raised)
    record("未在目标盘创建任何目录", not probe.exists())


if __name__ == "__main__":
    for fn in [test_idempotency, test_same_title_coexistence,
               test_content_dedupe_lineage_separation, test_state_machine_enum,
               test_ladder_discipline, test_denominator_reconciliation,
               test_catalog_card_body_separation, test_non_destructive_migration,
               test_path_discipline_blocks_non_d_drive]:
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {ex}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} passed")
    # 清理临时产物（只删本测试创建的目录）
    for d in _TMPS:
        shutil.rmtree(d, ignore_errors=True)
    sys.exit(0 if passed == total else 1)
