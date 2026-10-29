# -*- coding: utf-8 -*-
# K01 corpus.catalog_export 回归测试（standalone，零业务依赖）：
# 用法: D:/claudecode/SafetyMind/.venv/Scripts/python.exe tests/test_catalog_export.py
# 覆盖方案 §2.2/§2.3/§4.1/§9：
#   目录快照导入 → source 行数/受控引用/状态只到 catalog_registered 或 link_exported；
#   与 2640 对账（catalog_only scope，差异可解释且明确「不能声称文件全量」）；
#   分享维度预核验（唯一分享数/复用/多名复用/needs_manual_verify）；
#   阻塞登记落盘到 D:/SafetyMindData/corpus/reports/blocked_items.json 且结构可断言。
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import paths  # noqa: E402
from corpus import catalog_export  # noqa: E402
from corpus.manifest import CorpusManifest, _SECRET_RE  # noqa: E402

RESULTS = []
SCRATCH = Path("D:/claudecode/scratch/sm-corpus-20261019")  # 临时产物只落 D 盘（铁律 7）
REAL_REPORTS = Path("D:/SafetyMindData/corpus/reports")
_TMPS = []
SECRET = "SECRETPWD888"
SHARE_A = "https://pan.quark.cn/s/abcdEF"
SHARE_FOLDER = "https://pan.quark.cn/s/folderXYZ/folder/abc"


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def tmp_dir(prefix):
    SCRATCH.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=str(SCRATCH)))
    _TMPS.append(d)
    return d


def set_root(root):
    prev = os.environ.get("SAFETYMIND_CORPUS_ROOT")
    os.environ["SAFETYMIND_CORPUS_ROOT"] = str(root)
    return prev


def restore_root(prev):
    if prev is None:
        os.environ.pop("SAFETYMIND_CORPUS_ROOT", None)
    else:
        os.environ["SAFETYMIND_CORPUS_ROOT"] = prev


ITEMS = [
    {"item_id": "tb-1", "title": "动火作业管理规定", "category": "制度管理",
     "updated_at": "2026-10-17", "summary": "动火票有效期与监护人要求",
     "downloadUrl": f"{SHARE_A}?pwd={SECRET}"},
    {"id": "tb-2", "name": "职业健康监护监督管理.ppt", "type": "PPT",
     "date": "2026-10-17", "quarkLink": SHARE_A},          # 同一分享、不同标题 → 多名复用
    {"item_id": "tb-3", "title": "无外链模板清单", "category": "模板表单"},   # 无外链
    {"item_id": "tb-4", "title": "制度合集", "downloadUrl": SHARE_FOLDER},   # 文件夹形态
    {"title": "无条目ID的资料", "downloadUrl": "https://pan.quark.cn/s/noid"},  # 无 id，派生
    {"item_id": "tb-6", "title": "事故案例汇编", "downloadUrl": "https://pan.quark.cn/s/caseXYZ"},
    {"item_id": "tb-7", "title": "培训教材PDF", "downloadUrl": "https://pan.quark.cn/s/checklist.pdf"},
    {"item_id": "tb-8", "title": "MSDS 化学品安全技术说明书", "downloadUrl": f"{SHARE_A}#extract"},
]


def write_snapshot(tmp, items=None, wrapper="items"):
    snap = {wrapper: list(items if items is not None else ITEMS)}
    p = Path(tmp) / "catalog_snapshot_in.json"
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def new_env(prefix):
    tmp = tmp_dir(prefix)
    prev = set_root(tmp)
    m = CorpusManifest(paths.manifest_db(tmp))
    return m, tmp, prev


def cleanup(m, tmp, prev):
    try:
        m.close()
    except Exception:
        pass
    restore_root(prev)
    shutil.rmtree(tmp, ignore_errors=True)


# ── 1. 目录快照导入 ──────────────────────────────────────────────────────────
def test_export_snapshot():
    m, tmp, prev = new_env("cat_export_")
    try:
        snap = write_snapshot(tmp)
        r = catalog_export.export_snapshot(snap, provider="toolbox_catalog",
                                           exported_at="2026-10-19T21:00:00",
                                           note="本机可做的目录导入", manifest=m, root=tmp)
        rows = m.list_sources(provider="toolbox_catalog")
        record("source 行数 == 快照条目数", len(rows) == len(ITEMS) == r["total"],
               f"rows={len(rows)} total={r['total']}")
        record("全部只记 catalog_registered",
               all(x["acquisition_status"] == "catalog_registered" for x in rows),
               f"{sorted({x['acquisition_status'] for x in rows})}")
        record("绝不置 file_downloaded_verified / body_parsed",
               all("file_downloaded_verified" not in x["ladder"] and "body_parsed" not in x["ladder"]
                   for x in rows)
               and r["file_downloaded_verified"] == 0 and r["body_parsed"] == 0)
        with_link = [x for x in rows if x["link_ref"]]
        without = [x for x in rows if not x["link_ref"]]
        record("有外链条目置 link_exported",
               len(with_link) == 7 and all(x["ladder"] == ["link_exported"] for x in with_link),
               f"{[(x['item_id'], x['ladder']) for x in with_link]}")
        record("无外链条目不置 link_exported",
               len(without) == 1 and without[0]["item_id"] == "tb-3" and without[0]["ladder"] == [],
               f"{[(x['item_id'], x['ladder']) for x in without]}")
        blobs = json.dumps([x for x in rows], ensure_ascii=False)
        record("link_ref 已脱敏（提取码不进库）",
               SECRET not in blobs and not _SECRET_RE.search(blobs),
               f"secret_in_db={SECRET in blobs}")
        tb1 = [x for x in rows if x["item_id"] == "tb-1"][0]
        record("受控引用仅保留有效部分",
               tb1["link_ref"] == SHARE_A and SECRET not in tb1["link_ref"], tb1["link_ref"])
        record("raw 保留原条目但已脱敏",
               "downloadUrl" in tb1["raw"] and SECRET not in json.dumps(tb1["raw"], ensure_ascii=False),
               f"{tb1['raw']}")
        record("netdisk_provider 归一到 quark",
               all(x["netdisk_provider"] == "quark" for x in with_link))
        noid = [x for x in rows if x["title"] == "无条目ID的资料"]
        record("无条目 ID 时派生稳定 ID",
               len(noid) == 1 and len(noid[0]["item_id"]) == 24, f"{[x['item_id'] for x in noid]}")
        snapshot_report = [f for f in r.get("report_files", []) if "catalog_snapshot" in f]
        record("目录快照报告落盘且已脱敏",
               snapshot_report and Path(snapshot_report[0]).is_file()
               and SECRET not in Path(snapshot_report[0]).read_text(encoding="utf-8"),
               f"{snapshot_report}")
        record("导入结果内嵌 catalog_only 对账", r.get("reconciliation", {}).get("scope") == "catalog_only")
    finally:
        cleanup(m, tmp, prev)


# ── 2. 与 2640 对账 ──────────────────────────────────────────────────────────
def test_reconcile_catalog():
    m, tmp, prev = new_env("cat_recon_")
    try:
        catalog_export.export_snapshot(write_snapshot(tmp), manifest=m, root=tmp,
                                       exported_at="2026-10-19T21:00:00")
        r = catalog_export.reconcile_catalog(2640, manifest=m, root=tmp, write_report=True)
        record("scope=catalog_only", r["scope"] == "catalog_only", f"{r['scope']}")
        record("registered_total == 已导入条目数", r["registered_total"] == len(ITEMS),
               f"{r['registered_total']}")
        record("差异可解释（多/少条数与方向）",
               r["delta"] == len(ITEMS) - 2640 and r["delta_direction"] == "缺口",
               f"delta={r['delta']} dir={r['delta_direction']}")
        record("未取得远端清单",
               r["remote_file_list_obtained"] is False, f"{r['remote_file_list_obtained']}")
        record("明确不能声称文件全量",
               r["cannot_claim_file_completeness"] is True
               and "不能声称文件全量" in r["why_not_file_denominator"],
               f"{r['why_not_file_denominator']}")
        record("对账中已取得文件仍为 0（链路未取得文件）",
               r["acquired_files_verified"] == 0 and r["by_ladder"]["link_exported"] == 7,
               f"verified={r['acquired_files_verified']} ladder={r['by_ladder']}")
        record("catalog_reconciliation.json 落盘",
               Path(r["report_files"][0]).name == "catalog_reconciliation.json"
               and Path(r["report_files"][0]).is_file(), f"{r.get('report_files')}")
        # 只对账目录项：期望分母 == verified+duplicate+excluded+todo+blocked
        buckets = r["buckets"]
        record("分母恒等式成立",
               sum(buckets.values()) == r["registered_total"], f"{buckets}")
    finally:
        cleanup(m, tmp, prev)


# ── 3. 分享维度预核验 ────────────────────────────────────────────────────────
def test_precheck_shares():
    m, tmp, prev = new_env("cat_precheck_")
    try:
        catalog_export.export_snapshot(write_snapshot(tmp), manifest=m, root=tmp)
        rows = m.list_sources(provider="toolbox_catalog")
        r = catalog_export.precheck_shares(rows, limit=10, manifest=m, root=tmp)
        record("checked 受 limit 约束", r["checked"] == len(ITEMS), f"{r['checked']}")
        record("唯一分享数 < 条目数（复用已归并）",
               r["unique_shares"] < len(ITEMS), f"unique={r['unique_shares']} items={len(ITEMS)}")
        record("多名复用同一链接被识别",
               bool(r["reused_shares"]) and bool(r["multi_title_shares"]),
               f"reused={r['reused_shares']} multi_title={r['multi_title_shares']}")
        record("文件夹/合集形态被识别",
               bool(r["folder_or_collection"]), f"{r['folder_or_collection']}")
        record("needs_manual_verify 覆盖全部条目（未实际打开）",
               len(r["needs_manual_verify"]) == len(ITEMS)
               and all("reason" in n for n in r["needs_manual_verify"]),
               f"{len(r['needs_manual_verify'])}")
        record("只记 link_exported，不跳级",
               r["only_ladder_marked"] == "link_exported"
               and all(("share_accessible" not in x["ladder"] and "file_downloaded_verified" not in x["ladder"])
                       for x in m.list_sources(provider="toolbox_catalog")))
        r2 = catalog_export.precheck_shares(rows, limit=2, manifest=m, root=tmp)
        record("limit 生效", r2["checked"] == 2, f"{r2['checked']}")
    finally:
        cleanup(m, tmp, prev)


# ── 4. 多种快照形态 ──────────────────────────────────────────────────────────
def test_snapshot_shapes():
    m, tmp, prev = new_env("cat_shapes_")
    try:
        # 裸数组
        r1 = catalog_export.export_snapshot(list(ITEMS[:2]), manifest=m, root=tmp, write_reports=False)
        # wxapkg 解包报告形态：{"data": {"adminFiles": [...]}}
        payload = {"meta": {"appid": "<WX_APPID>"}, "data": {"adminFiles": [
            {"item_id": "wx-1", "title": "目录导出条目", "downloadUrl": "https://pan.quark.cn/s/wx1"}]}}
        r2 = catalog_export.export_snapshot(payload, manifest=m, root=tmp, write_reports=False)
        record("裸数组快照可导入", r1["total"] == 2 and r1["created"] == 2, f"{r1['total']}")
        record("wxapkg 报告形态可导入", r2["total"] == 1 and r2["created"] == 1, f"{r2}")
        record("两种形态合并后来源数正确",
               len(m.list_sources(provider="toolbox_catalog")) == 3,
               f"{len(m.list_sources(provider='toolbox_catalog'))}")
    finally:
        cleanup(m, tmp, prev)


# ── 5. 阻塞登记落盘 ──────────────────────────────────────────────────────────
def test_blocked_register():
    # 明确写入真实资料根目录（交付要求），不依赖环境变量
    r = catalog_export.blocked_register("D:/SafetyMindData/corpus")
    path = REAL_REPORTS / "blocked_items.json"
    record("blocked_items.json 落 D 盘报告目录",
           path.is_file() and path.as_posix().startswith("D:/SafetyMindData/corpus/reports/"),
           f"{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = {"blocker", "why", "needs", "what_can_still_run", "next_action"}
    record("条目数与非空字段齐全",
           data["count"] == 5 and len(data["blockers"]) == 5
           and all(keys <= set(b) and all(b[k] for k in keys) for b in data["blockers"]),
           f"{data.get('count')}")
    text = json.dumps(data, ensure_ascii=False)
    record("阻塞登记不含凭据赋值",
           SECRET not in text and not catalog_export._CRED_ASSIGN_RE.search(text))
    record("登记了微信会话/Storage 不可得",
           any("微信小程序会话" in b["blocker"] for b in data["blockers"]))
    record("登记了网盘登录不可得",
           any("网盘登录" in b["blocker"] for b in data["blockers"]))
    record("登记了无法取得远端完整文件清单",
           any("远端完整文件清单" in b["blocker"] for b in data["blockers"]))
    record("登记了容量预算与 D 盘现状（含实测值）",
           any("容量预算" in b["blocker"] for b in data["blockers"])
           and data.get("disk_free_gib", 0) > 0)
    record("登记了 1698 自有时段无原件",
           any("1698" in b["blocker"] for b in data["blockers"]))
    record("返回值与落盘一致", r["count"] == data["count"] and r["blockers"] == data["blockers"])


if __name__ == "__main__":
    for fn in [test_export_snapshot, test_reconcile_catalog, test_precheck_shares,
               test_snapshot_shapes, test_blocked_register]:
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {type(ex).__name__}: {ex}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} passed")
    for d in _TMPS:
        shutil.rmtree(d, ignore_errors=True)
    sys.exit(0 if passed == total else 1)
