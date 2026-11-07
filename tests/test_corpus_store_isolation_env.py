# -*- coding: utf-8 -*-
# corpus 共享语料库存储隔离回归（SM-01）。
#
# ## 缺口（证据 scratch/sm-loop/handoff/SM-00/run-001/isolation_churn.json）
#
# corpus 组测试在门禁 runner 下写共享语料库存储：
#   D:/SafetyMindData/corpus/indexes/chroma（新建测试集合、chroma.sqlite3 mtime 变更）
#   D:/SafetyMindData/corpus/reports/blocked_items.json
# 根因（路径决策点）：
#   - corpus/paths.py corpus_root() 的既有旋钮 SAFETYMIND_CORPUS_ROOT（默认
#     D:/SafetyMindData/corpus = 现行为）未注入 runner child_env，入口 root 形参
#     缺省时（build_index / CorpusSearch / blocked_register / CorpusManifest …）
#     全部回落共享根；
#   - tests/test_catalog_export.py::test_blocked_register 显式传真实根目录绕过旋钮
#     （已改为走旋钮；断言语义不变：报告仍须落 D 盘 reports/ 目录）。
#
# ## 本测试固化的契约
#
#   1) 设置 SAFETYMIND_CORPUS_ROOT → corpus 全链路（manifest、build_index、
#      activate_build、CorpusSearch、blocked_register）在"不传 root 形参"的默认
#      调用下全部落旋钮指向目录；期间共享存储零改动（chroma.sqlite3 字节与 mtime、
#      集合清单、indexes/ 全部文件、blocked_items.json）；
#   2) 未设置旋钮 → corpus_root() 解析保持现行为（= DEFAULT_ROOT D:/SafetyMindData/corpus）；
#      该分支只断言解析结果、绝不写共享存储（子进程 + 剥旋钮 + 只调纯解析函数）；
#   3) runner 门禁接线：child_env 注入 SAFETYMIND_CORPUS_ROOT 且纳入 STORAGE_ENV_KEYS 守卫。
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 共享的存储自检 helper

REPO = Path(__file__).resolve().parents[1]
SCRATCH = Path("D:/claudecode/scratch")          # 铁律 7：corpus 产物只落 D 盘
SHARED_ROOT = Path("D:/SafetyMindData/corpus")   # 共享语料库存储（本测试绝不写入）
SHARED_CHROMA = SHARED_ROOT / "indexes" / "chroma"
SHARED_BLOCKED = SHARED_ROOT / "reports" / "blocked_items.json"
KNOB = "SAFETYMIND_CORPUS_ROOT"
BUILD_ID = "iso-store-build"

TMP = Path(tempfile.mkdtemp(prefix="corpus_store_iso_", dir=str(SCRATCH)))
KNOBS = TMP / "knobs"

RESULTS = []


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def shared_state():
    """共享语料库存储的只读指纹：索引文件（含 chroma.sqlite3）+ 集合清单 + 阻塞登记。

    只读打开（mode=ro），绝不写共享目录；用于"测试前后零改动"断言。
    """
    st = {"indexes_files": {}, "blocked": None, "collections": None}
    indexes = SHARED_ROOT / "indexes"
    if indexes.is_dir():
        for p in sorted(indexes.rglob("*")):
            if p.is_file():
                s = p.stat()
                st["indexes_files"][p.relative_to(SHARED_ROOT).as_posix()] = [
                    s.st_size, s.st_mtime]
    if SHARED_BLOCKED.exists():
        s = SHARED_BLOCKED.stat()
        st["blocked"] = [hashlib.sha256(SHARED_BLOCKED.read_bytes()).hexdigest(),
                         s.st_size, s.st_mtime]
    db = SHARED_CHROMA / "chroma.sqlite3"
    if db.exists():
        s = db.stat()
        st["chroma_sqlite3"] = [s.st_size, s.st_mtime]
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            st["collections"] = sorted(r[0] for r in con.execute(
                "SELECT name FROM collections"))
        finally:
            con.close()
    return st


def iso_collections():
    """旋钮指向 chroma 的集合清单（证明写入确实落在隔离目录）。"""
    db = KNOBS / "indexes" / "chroma" / "chroma.sqlite3"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return sorted(r[0] for r in con.execute("SELECT name FROM collections"))
    finally:
        con.close()


def cleanup_knobs():
    """释放 chroma 句柄后清隔离目录（Windows 句柄延迟释放 → 重试删除）。

    返回是否删净；删不净只打 WARN，不算用例失败（范式见 _storage_guard.rmtree_retry）。
    """
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    import gc
    gc.collect()
    for _ in range(4):
        shutil.rmtree(str(TMP), ignore_errors=True)
        if not TMP.exists():
            return True
        gc.collect()
        time.sleep(0.25)
    return not TMP.exists()


def main():
    os.environ.update({"ANTHROPIC_API_KEY": "dummy",
                       "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
                       "SAFETYMIND_EMBEDDING": "ngram"})   # 离线确定性嵌入
    before = shared_state()
    record("共享语料库存储存在（前提）",
           (SHARED_CHROMA / "chroma.sqlite3").is_file() and bool(before["collections"]),
           f"collections={before['collections']}")

    # ── 0. 源码守护：旋钮仍在产品读取点上（防止被删后测试静默失去意义）──────
    src = (REPO / "corpus" / "paths.py").read_text(encoding="utf-8")
    record("旋钮契约存在于产品代码: SAFETYMIND_CORPUS_ROOT", KNOB in src, "corpus/paths.py")
    record("默认值契约保持现行为（DEFAULT_ROOT=D:/SafetyMindData/corpus）",
           'DEFAULT_ROOT = "D:/SafetyMindData/corpus"' in src, "corpus/paths.py")

    # ── 1. 设置旋钮 → corpus 全链路默认调用落旋钮指向目录 ─────────────────
    prev_knob = os.environ.get(KNOB)             # runner 注入值（若有）原样还原
    os.environ[KNOB] = str(KNOBS)
    try:
        from corpus import paths
        record("corpus_root() 走旋钮指向",
               paths.corpus_root() == KNOBS.resolve(), str(paths.corpus_root()))

        # 1a. blocked_register（不传 root 形参）→ 报告落旋钮目录
        from corpus import catalog_export
        r = catalog_export.blocked_register()
        report_path = Path(r["report_files"][0])
        record("blocked_register 落旋钮 reports/ 目录（不写共享 store）",
               report_path.is_file()
               and report_path.parent.as_posix().startswith(KNOBS.as_posix())
               and report_path.parent.name == "reports"
               and report_path.drive.upper() == "D:", str(report_path))
        record("blocked_register 返回值与落盘一致",
               r["count"] == 5 and json.loads(
                   report_path.read_text(encoding="utf-8"))["count"] == r["count"],
               f"count={r['count']}")

        # 1b. 建库全链路（manifest / build_index / activate_build 均不传 root）
        from corpus.build import activate_build, build_index
        from corpus.manifest import CorpusManifest
        m = CorpusManifest()                      # 无参 → 旋钮指向的 manifest.sqlite
        try:
            sid = m.upsert_source("user_imported", "iso-1", title="动火作业管理规定",
                                  acquisition_status="acquired_verified",
                                  parse_status="parsed")
            lg = m.make_logical_doc_id("user_imported", item_id="iso-1")
            vid = m.make_version_id(lg, "v1")
            m.add_version(lg, vid, sid, "动火作业管理规定", "", access_scope="")
            m.add_blocks(vid, [{"content": "动火票有效期不超过72小时，作业前应进行动火分析。",
                                "locator": {"para": 1}, "evidence_kind": "body"}])
        finally:
            m.close()
        meta = build_index(BUILD_ID)              # 无 root 形参 → 旋钮指向
        record("build_index 落旋钮指向（manifest/chroma/build.json）",
               Path(meta["chroma_dir"]).as_posix().startswith(KNOBS.as_posix())
               and (KNOBS / "manifest.sqlite").is_file()
               and bool(list((KNOBS / "indexes").glob(f"{BUILD_ID}/*"))),
               f"chroma_dir={meta.get('chroma_dir')}")
        activate_build(BUILD_ID)                  # 无 root 形参 → 旋钮指向

        # 1c. CorpusSearch（无参构造）→ 从旋钮指向检索
        from corpus.search import CorpusSearch
        cs = CorpusSearch()
        try:
            hits = cs.search("动火票有效期", top_k=3)
        finally:
            cs.close()
        record("CorpusSearch 从旋钮指向检出本 build 正文",
               (hits.get("build") or {}).get("build_id") == BUILD_ID
               and any("72小时" in b.get("content", "") for b in (hits.get("blocks") or [])),
               f"build={hits.get('build')}")

        # 1d. 写入确实落在隔离 chroma（集合存在），且共享集合清单不变
        cols = iso_collections()
        record("隔离 chroma 出现本 build 两条集合",
               f"corpus_cards__{BUILD_ID}" in cols and f"corpus_body__{BUILD_ID}" in cols,
               f"cols={cols}")
    finally:
        if prev_knob is None:
            os.environ.pop(KNOB, None)
        else:
            os.environ[KNOB] = prev_knob         # 还原 runner 注入值

    after = shared_state()
    record("共享 chroma.sqlite3 零改动（size/mtime）",
           before.get("chroma_sqlite3") == after.get("chroma_sqlite3"),
           f"before={before.get('chroma_sqlite3')} after={after.get('chroma_sqlite3')}")
    record("共享集合清单零改动（无新建测试集合）",
           before["collections"] == after["collections"],
           f"before={before['collections']} after={after['collections']}")
    record("共享 indexes/ 全部文件零改动（含 HNSW 段与 mtime）",
           before["indexes_files"] == after["indexes_files"],
           f"文件数 before={len(before['indexes_files'])} after={len(after['indexes_files'])}")
    record("共享 blocked_items.json 零改动（sha256/size/mtime）",
           before["blocked"] == after["blocked"],
           f"before={before['blocked']} after={after['blocked']}")

    # ── 2. 未设置旋钮 → 解析保持现行为（只解析，绝不写共享 store）────────
    probe = (
        "import json, os, sys\n"
        "sys.path.insert(0, r'%s')\n"
        "from corpus import paths\n"
        "assert 'SAFETYMIND_CORPUS_ROOT' not in os.environ\n"
        "root = paths.corpus_root()\n"
        "print('##JSON##' + json.dumps({'root': str(root), 'default': paths.DEFAULT_ROOT},\n"
        "                              ensure_ascii=False))\n"
    ) % str(REPO)
    env = {k: v for k, v in os.environ.items() if k != KNOB}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    unset_dir = TMP / "unset"
    unset_dir.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([sys.executable, "-c", probe], cwd=str(unset_dir), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    payload = {}
    for line in reversed((p.stdout or "").splitlines()):
        if line.startswith("##JSON##"):
            payload = json.loads(line[len("##JSON##"):])
            break
    record("未设置旋钮 → corpus_root() 默认真实根（现行为保持，仅解析不写盘）",
           payload.get("root") == str(Path("D:/SafetyMindData/corpus").resolve())
           and payload.get("default") == "D:/SafetyMindData/corpus",
           f"payload={payload} err={p.stderr[-120:]}")

    # ── 3. runner 门禁接线（本工作区布局下）：child_env 注入 + 守卫清单收录 ──
    runner_py = Path(__file__).resolve().parents[2] / "scratch" / "sm-loop" / "baseline_run.py"
    if runner_py.exists():
        rsrc = runner_py.read_text(encoding="utf-8")
        record("runner child_env 注入 SAFETYMIND_CORPUS_ROOT",
               '"SAFETYMIND_CORPUS_ROOT": str(iso_dir / "corpus")' in rsrc,
               "scratch/sm-loop/baseline_run.py")
        record("runner STORAGE_ENV_KEYS 收录 SAFETYMIND_CORPUS_ROOT",
               bool(re.search(r"STORAGE_ENV_KEYS\s*=\s*\[[^\]]*SAFETYMIND_CORPUS_ROOT",
                              rsrc, re.S)), "scratch/sm-loop/baseline_run.py")
    else:
        record("runner 接线检查（非本工作区布局，跳过）", True, str(runner_py))

    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} passed")
    if not cleanup_knobs():
        # Windows 上 chroma 句柄延迟释放会让目录暂时删不掉：按既有范式打印 WARN，
        # 不记为用例失败（scratch 残留由下次运行清理扫掉）。
        print(f"[WARN] 隔离目录暂未删净（句柄延迟释放）: {TMP}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
