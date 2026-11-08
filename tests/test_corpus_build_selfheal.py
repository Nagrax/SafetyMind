# -*- coding: utf-8 -*-
"""SM-41 门禁修复回归：corpus/build.py 写入后读回自愈。python tests/test_corpus_build_selfheal.py

背景（2026-11-08 门禁 wf-sm-41-c5 暴露）：本机 ChromaDB 1.5.9（Rust）在 Windows 上
约 1/70 偶发写入后 HNSW 段不可读（"Error creating hnsw segment reader: Nothing found
on disk"）——count() 仍通过、query() 才炸，core/chroma_utils.open_local_chroma 的打开
时 count 探测覆盖不到写入后的抖动，tests/_storage_guard 的 corpus 侧重建是同目录换
集合也救不回（门禁实录：primary/rebuild 两次自检均 InternalError）。修复：
build_index 收尾对两条集合各做一次 query 读回，命中段级损坏则删除**本 build** 的两条
集合并用内存全量数据重写一次（不触碰共享目录里其他 build 的集合）；重写后仍不可读
如实标 storage_selfcheck="failed"（fail-closed），绝不静默绿。

本回归以注入式 monkeypatch 驱动三态（不依赖真实抖动出现）：
  T1 首探段级异常 → 重写成功 → storage_selfcheck=recovered，counts 与播种一致，build.json 如实记录
  T2 首探+重写后探测均段级异常 → storage_selfcheck=failed（fail-closed，不静默绿）
  T3 健康路径 → ok（不误报）
  T4 段级判定命中产物真实错误文本；无关异常不误判
红灯锚点：修复前 build_index 不产出 storage_selfcheck 字段、corpus.build 无
_collections_queryable、chroma_utils 无 is_corruption——本测试首跑红（已验证，
red-first 见 handoff/SM-41/run-wf/selfheal-red.log）。测试失败只改产品代码。

退出码 0 = 全过；产物走 D 盘 scratch（corpus 路径断言），跑完清理。
"""
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRATCH = Path("D:/claudecode/scratch/sm-corpus-20261019")
RESULTS = []

REAL_HNSW_ERROR = ("InternalError: Error executing plan: Internal error: "
                   "Error creating hnsw segment reader: Nothing found on disk")


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return ok


def seed(root: Path):
    from corpus.manifest import CorpusManifest
    m = CorpusManifest(root / "manifest.sqlite")
    sid = m.upsert_source("user_imported", "i1", title="自检注入",
                          acquisition_status="parsed", parse_status="parsed")
    lg = m.make_logical_doc_id("user_imported", item_id="i1")
    vid = m.make_version_id(lg, "v1")
    m.add_version(lg, vid, sid, "自检注入", "", access_scope="")
    m.add_blocks(vid, [{"content": "探针UNIQ-HEAL-1 的内容。", "locator": {"para": 1},
                        "evidence_kind": "body"}])
    m.close()
    return vid


def run_build(build_id: str, root: Path, *, fail_first: bool, fail_again: bool):
    """注入式构建：_collections_queryable 第 n 次调用按参数返回段级异常/None。"""
    import corpus.build as B
    calls = {"n": 0}

    def fake(cols):
        calls["n"] += 1
        if (calls["n"] == 1 and fail_first) or (calls["n"] == 2 and fail_again):
            return type("InjectedSegmentError", (Exception,), {})(REAL_HNSW_ERROR)
        return None

    orig = B._collections_queryable
    B._collections_queryable = fake
    try:
        return B.build_index(build_id, root=root)
    finally:
        B._collections_queryable = orig


def main():
    import corpus.paths as P
    root = SCRATCH / "selfheal_root"
    if root.parent.exists():
        shutil.rmtree(str(root.parent), ignore_errors=True)
    root.mkdir(parents=True)
    os.environ.update({"SAFETYMIND_EMBEDDING": "ngram",
                       "SAFETYMIND_CORPUS_ROOT": str(root)})
    try:
        # 模块级契约：旧代码（修复前）无 _collections_queryable / is_corruption → 红灯
        try:
            import corpus.build as B
            from core.chroma_utils import is_corruption  # noqa: F401
            has_api = hasattr(B, "_collections_queryable")
        except ImportError as ex:
            record("T0|修复后模块可导入（corpus.build._collections_queryable + "
                   "chroma_utils.is_corruption）", False, f"ImportError: {ex}")
            print(f"\n0/{len(RESULTS)} PASSED")
            sys.exit(1)
        record("T0|修复后模块可导入（corpus.build._collections_queryable + "
               "chroma_utils.is_corruption）", has_api)

        vid = seed(root)

        # T1 重写成功：recovered + counts 完好 + build.json 如实记录
        meta = run_build("chk-heal", root, fail_first=True, fail_again=False)
        record("T1-1|首探注入段级异常 → storage_selfcheck=recovered（重写成功）",
               meta.get("storage_selfcheck") == "recovered",
               str(meta.get("storage_selfcheck")))
        record("T1-2|重写后数据完好（counts 与播种一致）",
               meta["counts"]["cards"] == 1 and meta["counts"]["blocks"] == 1,
               json.dumps(meta["counts"], ensure_ascii=False))
        bj = json.loads(P.build_dir("chk-heal", root).joinpath("build.json")
                        .read_text(encoding="utf-8"))
        record("T1-3|build.json 如实记录 recovered", bj.get("storage_selfcheck") == "recovered",
               str(bj.get("storage_selfcheck")))

        # T2 重写后仍坏：failed（fail-closed，绝不静默绿）
        meta2 = run_build("chk-fail", root, fail_first=True, fail_again=True)
        record("T2-1|重写后仍段级异常 → storage_selfcheck=failed（fail-closed）",
               meta2.get("storage_selfcheck") == "failed",
               str(meta2.get("storage_selfcheck")))
        bj2 = json.loads(P.build_dir("chk-fail", root).joinpath("build.json")
                         .read_text(encoding="utf-8"))
        record("T2-2|build.json 如实记录 failed", bj2.get("storage_selfcheck") == "failed",
               str(bj2.get("storage_selfcheck")))

        # T3 健康路径不误报
        meta3 = run_build("chk-ok", root, fail_first=False, fail_again=False)
        record("T3|健康路径 → storage_selfcheck=ok（不误报）",
               meta3.get("storage_selfcheck") == "ok",
               str(meta3.get("storage_selfcheck")))

        # T4 段级判定：命中门禁产物真实错误文本；无关异常不误判
        class E(Exception):
            pass
        record("T4-1|is_corruption 命中门禁实录错误文本（Nothing found on disk）",
               is_corruption(E(REAL_HNSW_ERROR)) is True)
        record("T4-2|is_corruption 不误判无关异常", is_corruption(E("ValueError: x")) is False)

        # T5 空正文场景（body 集合可能为空）探测不抛、路径可用
        m = __import__("corpus.manifest", fromlist=["CorpusManifest"]).CorpusManifest(
            root / "manifest.sqlite")
        try:
            sid = m.upsert_source("user_imported", "i2", title="目录卡无正文",
                                  acquisition_status="parsed", parse_status="parsed")
            lg = m.make_logical_doc_id("user_imported", item_id="i2")
            vid2 = m.make_version_id(lg, "v1")
            m.add_version(lg, vid2, sid, "目录卡无正文", "", access_scope="")
            # 不加 blocks：docs_without_body=1，body 集合为空
        finally:
            m.close()
        meta5 = run_build("chk-emptybody", root, fail_first=False, fail_again=False)
        record("T5|空正文集合（docs_without_body）读回探测不抛、selfcheck=ok",
               meta5.get("storage_selfcheck") == "ok"
               and meta5["counts"]["docs_without_body"] == 1,
               f"selfcheck={meta5.get('storage_selfcheck')} "
               f"counts={json.dumps(meta5.get('counts'), ensure_ascii=False)}")
    finally:
        shutil.rmtree(str(root.parent), ignore_errors=True)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
