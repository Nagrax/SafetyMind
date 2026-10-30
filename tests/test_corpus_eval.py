# -*- coding: utf-8 -*-
# K08 通用资料库评测骨架（离线为主，必须真实跑）：
# 用法: python tests/test_corpus_eval.py
# 覆盖：
#   - 用例集结构：120/30 配额与组配比、split 合法、expect 键合法、缺额必须在 content_gaps 有记录；
#   - runner 机制（注入 stub 答题器 + 真实小样本 corpus）：
#       四配置都产出 per_group 与 blocking_failures；无增量集时 B80/B300 标 not_applicable 且 ≠ B0；
#       Hit@5 按查询计且目录卡命中不计正文命中；阻断项（编造关键数字/错引现行状态）进 blocking_failures；
#       compare_configs 差值方向正确（B0 优于 A、A 优于 B0 两种构造）；
#       冻结集只评估不调参（回选/调参被拒）；
#   - 报告落 D 盘且结构可程序化断言。
# 铁律 9：corpus/chroma 全部走 scratch 下独立目录。
import asyncio
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

SCRATCH = Path("D:/claudecode/scratch/sm-corpus-eval-20261019/tests")
_ROOT_SEQ = [0]
RESULTS = []

HIT_TOKEN = "UNIQ-HIT1"
CAT_TOKEN = "UNIQ-CAT9"
MISS_TOKEN = "UNIQ-MISS7"

GROUPS = ("单主题定位与解释", "跨资料学习与组织", "版本·数字·引用",
          "复杂格式与结构", "资料发现与原件访问", "缺失·冲突·资料注入")


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def fresh_root(tag: str) -> Path:
    _ROOT_SEQ[0] += 1
    SCRATCH.mkdir(parents=True, exist_ok=True)
    root = SCRATCH / f"root_{tag}_{_ROOT_SEQ[0]}"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    os.environ["SAFETYMIND_CORPUS_ROOT"] = str(root)
    os.environ["SAFETYMIND_EMBEDDING"] = "ngram"
    return root


def seed_doc(m, item_id, title, content, *, with_blocks=True):
    sid = m.upsert_source("user_imported", item_id, title=title,
                          acquisition_status="acquired_verified", parse_status="parsed")
    lg = m.make_logical_doc_id("user_imported", item_id=item_id)
    vid = m.make_version_id(lg, "v1")
    m.add_version(lg, vid, sid, title, "", access_scope="")
    if with_blocks and content:
        m.add_blocks(vid, [{"content": content, "locator": {"para": 1},
                            "evidence_kind": "body"}])
    return vid


def build_small():
    """真实小样本 corpus：一条正文 + 一条仅目录（无正文）。"""
    from corpus.build import activate_build
    from corpus.manifest import CorpusManifest
    root = fresh_root("eval")
    m = CorpusManifest(root / "manifest.sqlite")
    try:
        seed_doc(m, "d1", "动火作业管理规定",
                 f"动火票有效期{HIT_TOKEN}不超过72小时，作业前应进行动火分析。")
        seed_doc(m, "d2", f"应急预案目录条目{CAT_TOKEN}", "", with_blocks=False)
    finally:
        m.close()
    # 自检存储可用后再跑评测（自检不过会删该 build 目录重建一次）
    guard.build_index_checked("eval-build", root=root, probe=HIT_TOKEN)
    activate_build("eval-build", root)
    return root


def _empty_search(query, top_k=5):
    return {"blocks": [], "cards": []}


def _hit_search(query, top_k=5):
    return {"blocks": [{"content": f"动火票有效期{HIT_TOKEN}不超过72小时。",
                        "title": "动火作业管理规定", "evidence_kind": "body"}], "cards": []}


def _mk(cid, group, message, **kw):
    case = {"id": cid, "group": group, "split": "frozen", "message": message,
            "expect": {}, "accept_sources": [], "must_include": [], "forbidden": [],
            "label_source": "synthetic"}
    case.update(kw)
    return case


# ── 1. 用例集结构 ────────────────────────────────────────────────────────────
def test_case_set_structure():
    from evaluation.corpus_eval.runner import EXPECT_KEYS
    from evaluation.corpus_eval import load_document, validate_cases
    doc = load_document()
    res = validate_cases(doc["cases"], content_gaps=doc.get("content_gaps"),
                         quotas=doc.get("quotas"))
    counts = res["counts"]
    frozen_q = doc["quotas"]["frozen"]
    record("冻结集逐组配额与 content_gaps 校验通过（36/24/18/±/12/12）",
           all(counts.get(f"frozen/{g}", 0) <= q for g, q in frozen_q.items())
           and counts.get("frozen/单主题定位与解释") == 36
           and counts.get("frozen/跨资料学习与组织") == 24
           and counts.get("frozen/版本·数字·引用") == 18
           and counts.get("frozen/缺失·冲突·资料注入") == 12,
           f"counts={counts}")
    record("开发集 30 条", counts.get("dev/单主题定位与解释", 0)
           + counts.get("dev/跨资料学习与组织", 0) + counts.get("dev/版本·数字·引用", 0)
           + counts.get("dev/复杂格式与结构", 0) + counts.get("dev/资料发现与原件访问", 0)
           + counts.get("dev/缺失·冲突·资料注入", 0) == 30, f"dev={counts}")
    record("每条 case 的 expect 键都在 e2e_runner 允许集内",
           all(set(c.get("expect") or {}) <= set(EXPECT_KEYS) for c in doc["cases"]))
    record("split 只取 frozen/dev 且 label_source 非空",
           {c["split"] for c in doc["cases"]} <= {"frozen", "dev"}
           and all(str(c.get("label_source")).strip() for c in doc["cases"]))
    gaps = doc["content_gaps"]
    record("缺内容题组在 content_gaps 有记录（复杂格式/资料发现）",
           "frozen/复杂格式与结构" in gaps and "frozen/资料发现与原件访问" in gaps
           and gaps["frozen/复杂格式与结构"]["quota"] == 18
           and gaps["frozen/复杂格式与结构"]["written"] < 18
           and gaps["frozen/资料发现与原件访问"]["written"] < 12,
           f"gaps={list(gaps)}")
    record("content_gaps 逐组写明缺什么内容与占位条数",
           all(g.get("missing_content") and g.get("note") and "placeholders" in g
               for g in gaps.values()), f"gaps={gaps}")

    # 负例：缺额未记录 → 报错
    one = [_mk("x1", "单主题定位与解释", "q")]
    try:
        validate_cases(one, quotas={"frozen": {"单主题定位与解释": 36}})
        record("未达配额且无 content_gaps 记录被拒", False, "未报错")
    except ValueError as ex:
        record("未达配额且无 content_gaps 记录被拒", "配额" in str(ex), str(ex))
    # 负例：未知 expect 键 → 报错
    bad = [_mk("x2", "单主题定位与解释", "q")]
    bad[0]["expect"] = {"not_a_real_key": 1}
    try:
        validate_cases(bad, quotas=None)
        record("未知 expect 键被拒", False, "未报错")
    except ValueError as ex:
        record("未知 expect 键被拒", "未知 expect 键" in str(ex), str(ex))
    # 负例：非法 split → 报错
    bad2 = [_mk("x3", "单主题定位与解释", "q")]
    bad2[0]["split"] = "nonsense"
    try:
        validate_cases(bad2, quotas=None)
        record("非法 split 被拒", False, "未报错")
    except ValueError as ex:
        record("非法 split 被拒", "split" in str(ex), str(ex))
    # 负例：非法 group → 报错
    bad3 = [_mk("x4", "不存在的组", "q")]
    try:
        validate_cases(bad3, quotas=None)
        record("非法 group 被拒", False, "未报错")
    except ValueError as ex:
        record("非法 group 被拒", "group" in str(ex), str(ex))


# ── 2. 四配置 / per_group / not_applicable / Hit@5 / 目录卡 ────────────────
def test_runner_configs_and_hit():
    from evaluation.corpus_eval import run_ab
    build_small()
    cases = [
        _mk("t-hit", "单主题定位与解释", f"{HIT_TOKEN} 的动火票有效期是多少",
            accept_sources=[HIT_TOKEN], must_include=["72小时"]),
        _mk("t-miss", "单主题定位与解释", f"{MISS_TOKEN} 的规定是什么",
            accept_sources=[MISS_TOKEN]),
        _mk("t-cat", "单主题定位与解释", f"应急预案目录条目{CAT_TOKEN}",
            accept_sources=[CAT_TOKEN], placeholder=True,
            expect_any=["未检索到", "缺", "无相关资料", "无法", "仅目录"]),
    ]
    out = SCRATCH / "reports"
    report = _run(run_ab(cases, baseline_search_fn=_empty_search, out_path=str(out)))
    cfgs = report["configs"]
    record("四个配置都进入报告", set(cfgs) == {"A", "B0", "B80", "B300"}, f"cfgs={list(cfgs)}")
    for name in ("A", "B0"):
        c = cfgs[name]
        record(f"{name} 产出 per_group 与 blocking_failures",
               isinstance(c.get("per_group"), dict) and isinstance(c.get("blocking_failures"), list)
               and "单主题定位与解释" in c["per_group"], f"{name}={c.get('total')}")
    record("B80 无增量集时标 not_applicable",
           cfgs["B80"]["applicable"] is False and cfgs["B80"]["pass_rate"] is None
           and "增量集未取得" in cfgs["B80"]["not_applicable_reason"], f"B80={cfgs['B80']}")
    record("B80 不得被编成与 B0 相同（B80.pass_rate != B0.pass_rate）",
           cfgs["B80"]["pass_rate"] != cfgs["B0"]["pass_rate"])
    record("B300 同样标 not_applicable", cfgs["B300"]["applicable"] is False)

    hit = cfgs["B0"]["hit_at_5"]
    record("Hit@5 按查询计：3 条单主题题命中 1 条（手工核对）",
           hit["n"] == 3 and hit["hits"] == 1 and abs(hit["rate"] - 1 / 3) < 5e-4,
           f"hit={hit}")
    # 目录卡命中不计入正文命中：t-cat 只命中卡（小语料下正文通道可能返回无关块，但不得计入命中）
    cat_trace = next(t for t in cfgs["B0"]["cases"] if t["id"] == "t-cat")
    record("仅命中目录卡时该查询正文命中为 0（不计入 Hit@5）",
           cat_trace["hit_at_5"] is False and cat_trace["citation_tokens_hit"] == []
           and not any(CAT_TOKEN in b.get("content", "")
                       for b in cat_trace["retrieval_blocks"]),
           f"blocks={[b.get('content', '')[:20] for b in cat_trace['retrieval_blocks']]}")
    record("仅命中目录卡被计入 catalog_only（不并入正文命中）",
           hit["catalog_only_hits"] >= 1, f"hit={hit}")
    record("仅目录条目不以目录卡形式进入 citations",
           all(c.get("evidence_kind") == "body" for c in cat_trace["citations"]))
    record("正文通道返回的块一律 evidence_kind=body（目录卡不混入）",
           all(b.get("evidence_kind") == "body" for t in cfgs["B0"]["cases"]
               for b in t["retrieval_blocks"]))

    hit_trace = next(t for t in cfgs["B0"]["cases"] if t["id"] == "t-hit")
    record("命中题以正文块作答并带正文引用（citations 非空）",
           hit_trace["hit_at_5"] is True and bool(hit_trace["citations"])
           and all(c["evidence_kind"] == "body" for c in hit_trace["citations"]))

    # 无任何检索结果时如实回"未检索到"（不编造）——注入空检索通道
    empty = _run(run_ab([_mk("t-none", "单主题定位与解释", "动火票有效期是多少",
                             accept_sources=[HIT_TOKEN], must_include=["72小时"])],
                        baseline_search_fn=_empty_search, corpus_search_fn=_empty_search,
                        configs=("B0",)))
    none_trace = empty["configs"]["B0"]["cases"][0]
    record("无检索结果时如实回'未检索到'（不编造）",
           "未检索到" in none_trace["response"] and none_trace["hit_at_5"] is False,
           f"resp={none_trace['response'][:60]}")
    record("必答信息缺失时该题判失败（不因'没查到'而放行）",
           none_trace["passed"] is False
           and any(f["rule"] == "must_include" for f in none_trace["failures"]))

    # A（空检索） vs B0（真实检索）方向：以必答信息题手工核对
    directional = _run(run_ab(
        [_mk("d1", "单主题定位与解释", f"{HIT_TOKEN} 的有效期是多少",
             accept_sources=[HIT_TOKEN], must_include=["72小时"]),
         _mk("d2", "单主题定位与解释", f"{HIT_TOKEN} 作业前要做什么",
             accept_sources=[HIT_TOKEN], must_include=["动火分析"])],
        baseline_search_fn=_empty_search))
    record("A 链路（注入空检索）通过率低于 B0（链路改造收益方向）",
           directional["configs"]["A"]["pass_rate"] < directional["configs"]["B0"]["pass_rate"]
           and directional["configs"]["A"]["pass_rate"] == 0.0
           and directional["configs"]["B0"]["pass_rate"] == 1.0,
           f"A={directional['configs']['A']['pass_rate']} "
           f"B0={directional['configs']['B0']['pass_rate']}")


# ── 3. 阻断项 ────────────────────────────────────────────────────────────────
def test_blocking_failures():
    from evaluation.corpus_eval import run_ab
    build_small()

    def bad_stub(message, evidence):
        return {"response": "动火票有效期是 24 小时；该标准现行有效，可以照此执行。",
                "citations": [], "knowledge_used": False, "escalated": False,
                "intent": "other", "total_ms": 0.0, "request_id": "", "conv_id": ""}

    cases = [
        _mk("b-num", "版本·数字·引用", "动火票有效期是多少",
            accept_sources=[HIT_TOKEN], must_include=["72小时"], blocking_missing=["72小时"]),
        _mk("b-status", "版本·数字·引用", "GB50187-2012 是不是现行版本",
            accept_sources=[HIT_TOKEN], blocking_forbidden=["现行有效"]),
    ]
    report = _run(run_ab(cases, chat_fn=bad_stub, baseline_search_fn=_empty_search,
                         configs=("B0",)))
    cfg = report["configs"]["B0"]
    by_rule = {b["rule"] for b in cfg["blocking_failures"]}
    record("编造关键数字进 blocking_failures", "blocking_missing" in by_rule,
           f"rules={by_rule}")
    record("错引现行状态进 blocking_failures", "blocking_forbidden" in by_rule,
           f"rules={by_rule}")
    record("阻断项使该组不通过（blocking=2 且 failed=2）",
           cfg["per_group"]["版本·数字·引用"]["blocking"] == 2
           and cfg["per_group"]["版本·数字·引用"]["failed"] == 2,
           f"group={cfg['per_group']['版本·数字·引用']}")
    record("阻断项不计入普通失分清单之外的普通 rule（单独成列）",
           cfg["blocking_failure_count"] == 2)


# ── 4. compare_configs 差值方向 ──────────────────────────────────────────────
def test_compare_configs_direction():
    from evaluation.corpus_eval import compare_configs, run_ab
    build_small()
    cases = [_mk("c1", "单主题定位与解释", f"{HIT_TOKEN} 的有效期是多少",
                 accept_sources=[HIT_TOKEN], must_include=["72小时"])]

    better = _run(run_ab(cases, baseline_search_fn=_empty_search))
    link_better = compare_configs(better)["link_upgrade"]
    record("B0 优于 A 时 link_upgrade 差值为正",
           link_better["applicable"] and link_better["pass_rate_delta_pp"] > 0,
           f"link={link_better}")

    worse = _run(run_ab(cases, baseline_search_fn=_hit_search,
                        corpus_search_fn=_empty_search))
    link_worse = compare_configs(worse)["link_upgrade"]
    record("B0 退化（A 命中、B0 空）时差值为负",
           link_worse["applicable"] and link_worse["pass_rate_delta_pp"] < 0,
           f"link={link_worse}")

    verdict = compare_configs(worse)["increment_value_verdict"]
    record("增量集未取得时 §8.3 判定为 not_applicable（不谎称达标）",
           verdict["applicable"] is False and verdict["status"] == "not_applicable",
           f"verdict={verdict}")


# ── 5. 冻结集纪律 ────────────────────────────────────────────────────────────
def test_frozen_discipline():
    from evaluation.corpus_eval import (assert_frozen_untouched, select_corpus_thresholds)
    frozen = [_mk("fz1", "单主题定位与解释", "q")]
    record("冻结集在 evaluate 用途下放行",
           assert_frozen_untouched(frozen, purpose="evaluate")["untouched"] is True)
    try:
        assert_frozen_untouched(frozen, purpose="retune")
        record("用冻结集调参被拒", False, "未报错")
    except ValueError as ex:
        record("用冻结集调参被拒", "冻结集" in str(ex), str(ex))
    try:
        assert_frozen_untouched(frozen, purpose="select_sources")
        record("用冻结集回选资料被拒", False, "未报错")
    except ValueError as ex:
        record("用冻结集回选资料被拒", "拒绝" in str(ex), str(ex))
    try:
        select_corpus_thresholds("frozen", {"cases": []})
        record("冻结集回选阈值被拒", False, "未报错")
    except ValueError as ex:
        record("冻结集回选阈值被拒", "冻结集" in str(ex), str(ex))
    record("开发集上选定口径被允许（对照）",
           select_corpus_thresholds("dev", {"cases": [1, 2]})["selected_on"]["kind"] == "dev")


# ── 6. 报告落盘 ──────────────────────────────────────────────────────────────
def test_report_disk():
    from evaluation.corpus_eval import run_ab
    build_small()
    cases = [_mk("r1", "单主题定位与解释", f"{HIT_TOKEN} 的有效期是多少",
                 accept_sources=[HIT_TOKEN], must_include=["72小时"])]
    out = SCRATCH / "reports_disk"
    report = _run(run_ab(cases, baseline_search_fn=_empty_search, out_path=str(out)))
    path = Path(report["report_path"])
    record("报告落盘到 D 盘", path.exists() and path.drive.upper() == "D:",
           f"path={path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    record("落盘报告结构可程序化断言",
           isinstance(data.get("configs"), dict) and isinstance(data.get("comparisons"), dict)
           and isinstance(data["configs"]["B0"].get("per_group"), dict)
           and isinstance(data["configs"]["B0"].get("blocking_failures"), list)
           and "content_gaps" in data,
           f"keys={sorted(data)}")
    record("报告中逐组披露（per_group 含本组）",
           "单主题定位与解释" in data["configs"]["B0"]["per_group"])


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
    for fn in (test_case_set_structure, test_runner_configs_and_hit, test_blocking_failures,
               test_compare_configs_direction, test_frozen_discipline, test_report_disk):
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {type(ex).__name__}: {ex}")
    _cleanup()
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} PASSED")
    sys.exit(0 if passed == total else 1)
