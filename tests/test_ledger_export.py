# -*- coding: utf-8 -*-
# 安全台账文件导出测试（2026-10-26 闭环第 3 轮新增能力）。
#
# 背景（用户指出的真实痛点）：安全员台账最终要交 Office/WPS 文件，
# 而原 safety_ledger_assistant 只会"告诉你要准备哪些文件"，不能产出文件本身。
#
# 覆盖：
#   1. 五类法定台账模板齐备且带法律依据
#   2. xlsx 真实产出：文件存在、可被 openpyxl 重新读回、表头与模板一致
#   3. 产出内容不是空壳：含标题/依据/表头，且有空白填写行（可打印手填）
#   4. docx 能力缺失时给出**可执行的补救建议**而不是模糊报错
#   5. 未知台账类型 / 未知格式 → 结构化失败（不抛未捕获异常）
#   6. 产物落 D 盘（铁律 7），不写 C 盘
# 用法: python tests/test_ledger_export.py
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    from core.ledger_export import (LEDGER_TEMPLATES, capability_report, export,
                                     export_xlsx)

    # ── 1. 模板齐备性 ──────────────────────────────────────────────────
    required = {"hazard", "inspection", "training", "ticket", "emergency_drill"}
    record("五类法定台账模板齐备", required <= set(LEDGER_TEMPLATES),
           f"实际={list(LEDGER_TEMPLATES)}")
    no_basis = [k for k, v in LEDGER_TEMPLATES.items() if not v.get("legal_basis")]
    record("每类台账都有法律依据说明", not no_basis, f"缺依据={no_basis}")
    empty_cols = [k for k, v in LEDGER_TEMPLATES.items() if len(v.get("columns") or []) < 5]
    record("每类台账列定义不少于 5 列", not empty_cols, f"列不足={empty_cols}")

    # ── 2. xlsx 真实产出与可读回 ────────────────────────────────────────
    tmp = Path("D:/claudecode/scratch/sm-loop/test_ledger_out")
    if tmp.exists():
        shutil.rmtree(str(tmp), ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    try:
        import openpyxl
        p = export_xlsx("hazard", tmp, with_demo=True)
        record("xlsx 产出文件真实存在", p.exists() and p.stat().st_size > 1000,
               f"path={p} size={p.stat().st_size if p.exists() else 0}")
        wb = openpyxl.load_workbook(str(p))
        record("xlsx 可被 openpyxl 重新读回（不是损坏文件）", len(wb.sheetnames) >= 1,
               f"sheets={wb.sheetnames}")
        ws = wb[wb.sheetnames[0]]
        header = [ws.cell(row=3, column=c).value for c in range(1, ws.max_column + 1)]
        expect_cols = [c[0] for c in LEDGER_TEMPLATES["hazard"]["columns"]]
        record("xlsx 表头与模板列定义逐列一致", header == expect_cols,
               f"实际={header} 期望={expect_cols}")

        title = ws.cell(row=1, column=1).value
        basis = ws.cell(row=2, column=1).value
        record("xlsx 含台账标题行", title == LEDGER_TEMPLATES["hazard"]["name"], f"title={title}")
        record("xlsx 含法律依据行", bool(basis) and "安全生产法" in str(basis), f"basis={basis}")

        # 示例数据 + 空白填写行都要在
        demo_present = any(ws.cell(row=r, column=1).value == 1 for r in range(4, 10))
        blanks = sum(1 for r in range(4, ws.max_row + 1)
                     if ws.cell(row=r, column=1).value is None)
        record("xlsx 含示例数据行", demo_present)
        record("xlsx 留有空白填写行（可打印手填）", blanks >= 5, f"空白行={blanks}")
        record("xlsx 含填写说明页", "填写说明" in wb.sheetnames, f"sheets={wb.sheetnames}")
    except Exception as e:
        record("xlsx 产出链路", False, f"{type(e).__name__}: {e}")

    # ── 3. 无示例时也应有空白可填行（空模板场景）────────────────────────
    try:
        p2 = export_xlsx("inspection", tmp, with_demo=False)
        wb2 = openpyxl.load_workbook(str(p2))
        ws2 = wb2[wb2.sheetnames[0]]
        record("空模板也有表头与空白行", ws2.max_row >= 10 and ws2.cell(row=3, column=1).value == "序号",
               f"max_row={ws2.max_row}")
    except Exception as e:
        record("空模板生成", False, f"{type(e).__name__}: {e}")

    # ── 4. docx 能力缺失时给可执行补救建议 ──────────────────────────────
    caps = capability_report()
    r = export("inspection", "docx", tmp, False)
    if caps["docx"]:
        record("docx 可用时能真实产出", r.get("ok") is True and Path(r["path"]).exists(),
               f"r={r}")
    else:
        err = str(r.get("error") or "")
        record("docx 缺失时返回结构化失败", r.get("ok") is False, f"r={r}")
        record("docx 缺失的错误里给出可执行补救建议（装包路径/替代格式）",
               ("python-docx" in err) and ("xlsx" in err or "pip install" in err),
               f"err={err[:200]}")

    # ── 5. 未知台账/格式结构化失败 ──────────────────────────────────────
    r_bad_ledger = export("__no_such__", "xlsx", tmp)
    record("未知台账类型返回结构化失败并列出可用项",
           r_bad_ledger.get("ok") is False and len(r_bad_ledger.get("available") or []) >= 5,
           f"r={r_bad_ledger}")
    r_bad_fmt = export("hazard", "pdf", tmp)
    record("未知格式返回结构化失败", r_bad_fmt.get("ok") is False, f"r={r_bad_fmt}")

    # ── 6. 产物只落 D 盘（铁律 7）───────────────────────────────────────
    r_d = export("hazard", "xlsx", tmp, True)
    path_d = str(r_d.get("path") or "")
    record("产物落在 D 盘（不写 C 盘）", path_d.startswith("D:"), f"path={path_d}")

    # 清理
    shutil.rmtree(str(tmp), ignore_errors=True)

    # ── 7. 同秒并发导出回归（export-same-second-overwrite）──────────────
    conc_xlsx_tests()
    conc_docx_tests()

    # ── SM-42 整改验收摘要试点（2026-11-08）─────────────────────────────────
    sm42_main()

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


# ═══════════════════════════════════════════════════════════════════════════
# 同秒并发导出回归（闭环发现 export-same-second-overwrite，2026-11-09）
#
# 发现：export_xlsx/export_docx 文件名只带秒级时间戳（%Y%m%d-%H%M%S），同一秒内的
#       连发/并发导出拿到同一路径，后写者静默覆写前写者（写探针实测 5 连发
#       files_created 仅 +1），且直接 save() 无原子发布。
# 修后契约（对齐 SM-42 版本化归档"产物一旦存在绝不覆写"范式）：
#   C1 同一秒内 N 次导出（顺序连发 + 多线程并发）→ N 个互不相同且完好可读回的文件；
#   C2 已存在文件（哪怕恰好占用本次时间戳名）绝不被覆写，导出走冲突序号路径；
#   C3 渲染失败零残留（不留认领占位/临时半成品）。
# 时间在模块命名空间内冻结到同一秒：让旧实现确定性复现"同秒同路径"，
# 不赌真实秒边界（否则红灯变偶发，等于没测）。
# ═══════════════════════════════════════════════════════════════════════════

CONC_OUT = Path("D:/claudecode/scratch/sm-loop/test_ledger_conc_out")


class _FrozenSecondTime:
    """替换 core.ledger_export 命名空间里的 time 模块（仅测试内），冻结同一秒。"""

    FROZEN_STAMP = "20261109-120000"

    @staticmethod
    def strftime(fmt, *args, **kwargs):
        return _FrozenSecondTime.FROZEN_STAMP


def _conc_subdir(name: str) -> Path:
    d = CONC_OUT / name
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _conc_assert_c1(label: str, do_export, readback) -> None:
    """C1：同一秒内顺序 5 连发 + 多线程并发 8 导出 → 各自 N 个互不相同且完好的文件。"""
    paths = [do_export() for _ in range(5)]
    uniq = {str(p) for p in paths}
    record(f"[{label}] C1: 同秒 5 连发产出 5 个互不相同的文件", len(uniq) == 5,
           f"uniq={len(uniq)} names={[Path(str(p)).name for p in paths]}")
    sizes = {str(p): (Path(p).stat().st_size if Path(p).exists() else 0) for p in paths}
    record(f"[{label}] C1: 5 个产物全部落盘且非空", all(s > 1000 for s in sizes.values()),
           f"sizes={sizes}")
    bad = []
    for p in paths:
        try:
            readback(p)
        except Exception as e:
            bad.append(f"{Path(p).name}: {type(e).__name__}: {e}")
    record(f"[{label}] C1: 5 个产物全部完好可读回（无覆写/撕裂损坏）", not bad, f"bad={bad}")

    results, errors = [], []

    def _worker():
        try:
            results.append(do_export())
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    uniq_t = {str(p) for p in results}
    ok_thr = (not errors and len(uniq_t) == 8
              and all(Path(p).exists() and Path(p).stat().st_size > 1000 for p in results))
    record(f"[{label}] C1: 多线程并发 8 导出零异常且产出 8 个互不相同的非空文件", ok_thr,
           f"errors={errors} uniq={len(uniq_t)} names={[Path(p).name for p in results]}")
    bad = []
    for p in results:
        try:
            readback(p)
        except Exception as e:
            bad.append(f"{Path(p).name}: {type(e).__name__}: {e}")
    record(f"[{label}] C1: 并发 8 产物全部完好可读回", not bad, f"bad={bad}")


def _conc_assert_c2(label: str, suffix: str, do_export) -> None:
    """C2：已存在文件（恰好占用本次将生成的时间戳名）绝不被覆写。"""
    d2 = _conc_subdir(f"{label}_c2")
    canary = d2 / f"隐患排查治理台账_{_FrozenSecondTime.FROZEN_STAMP}{suffix}"
    canary.write_bytes(b"canary-not-a-real-export")
    p = do_export(d2)
    record(f"[{label}] C2: 同名已存在文件不被覆写（导出走冲突序号路径）",
           Path(p) != canary and canary.read_bytes() == b"canary-not-a-real-export",
           f"export={Path(p).name} canary_bytes={len(canary.read_bytes())}")


def _conc_assert_c3(label: str, boom_save) -> None:
    """C3：渲染失败返回结构化失败且零残留（不留认领占位/临时半成品）。"""
    from core import ledger_export as lx
    d3 = _conc_subdir(f"{label}_c3")
    with boom_save():
        r = lx.export("hazard", label, d3, True)
    residue = sorted(q.name for q in d3.iterdir())
    record(f"[{label}] C3: 渲染失败返回结构化失败",
           r.get("ok") is False and "注入的保存故障" in str(r.get("error")), f"r={r}")
    record(f"[{label}] C3: 渲染失败零残留（无认领占位/临时文件）", not residue,
           f"residue={residue}")


def conc_xlsx_tests():
    from core import ledger_export as lx
    import openpyxl

    d1 = _conc_subdir("xlsx_c1")
    do_export = lambda out=None: lx.export_xlsx("hazard", out or d1, with_demo=True)

    def _readback(p):
        wb = openpyxl.load_workbook(str(p))
        assert len(wb.sheetnames) >= 1, f"sheets={wb.sheetnames}"

    import contextlib

    @contextlib.contextmanager
    def _boom_save():
        orig = openpyxl.Workbook.save

        def _boom(self, filename):
            raise RuntimeError("注入的保存故障")
        openpyxl.Workbook.save = _boom
        try:
            yield
        finally:
            openpyxl.Workbook.save = orig

    orig_time = lx.time
    lx.time = _FrozenSecondTime
    try:
        _conc_assert_c1("xlsx", do_export, _readback)
        _conc_assert_c2("xlsx", ".xlsx", do_export)
        _conc_assert_c3("xlsx", _boom_save)
    finally:
        lx.time = orig_time
        shutil.rmtree(str(CONC_OUT), ignore_errors=True)


def conc_docx_tests():
    """docx 分支：能力存在跑完整契约；主 venv 无 python-docx 时该环境下
    契约 = 结构化失败且零落盘（与上方第 4 节同口径），不虚报并发结果。"""
    from core import ledger_export as lx
    caps = lx.capability_report()
    d1 = _conc_subdir("docx_c1")
    do_export = lambda out=None: lx.export_docx("hazard", out or d1, with_demo=True)

    orig_time = lx.time
    lx.time = _FrozenSecondTime
    try:
        if caps.get("docx"):
            import docx
            import contextlib

            def _readback(p):
                docx.Document(str(p))

            # docx.Document 是工厂函数，save 挂在实例类型上（docx.document.Document）
            _doc_cls = type(docx.Document())

            @contextlib.contextmanager
            def _boom_save():
                orig = _doc_cls.save

                def _boom(self, filename):
                    raise RuntimeError("注入的保存故障")
                _doc_cls.save = _boom
                try:
                    yield
                finally:
                    _doc_cls.save = orig

            _conc_assert_c1("docx", do_export, _readback)
            _conc_assert_c2("docx", ".docx", do_export)
            _conc_assert_c3("docx", _boom_save)
        else:
            r = lx.export("hazard", "docx", d1, True)
            record("docx: 无能力环境导出为结构化失败且零落盘（不进入并发链路）",
                   r.get("ok") is False and not any(d1.iterdir()), f"r={r}")
    finally:
        lx.time = orig_time
        shutil.rmtree(str(CONC_OUT), ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# SM-42 整改验收摘要试点（2026-11-08 闭环新增）
#
# 决策规则：scratch/sm-loop/handoff/SM-42/run-wf/decision.md（R1–R10，跑测试前固化）
# 口径（任务卡验收三条）：
#   S1 合成样例导出中的状态、责任方、证据、版本和确认者与 DB 一致（store 直读对照）
#   S2 格式以字段与布局金样验收：xlsx 读回逐格对照**测试内嵌字面量金样**
#      （不 import 产品布局常量——改布局/改字段序必须红灯）
#   S3 真实入口测试（FastAPI TestClient 走 HTTP）：未验收 409 / 过期审批 409 /
#      模板不匹配 409 / 渲染失败结构化失败且零残留 / 跨主体下载 403、
#      归属主体与管理员放行、匿名 401
#   S4 重复导出语义一致（data_hash 相等、版本递增）+ 归档快照后继续改工单
#      不会篡改旧产物（旧文件字节 sha256 与 manifest 记录一致）
# 铁律：存储与产物全部落临时目录；不碰生产 D:/SafetyMindData；环境不可用退出码 2。
# ═══════════════════════════════════════════════════════════════════════════

SM42_OUT = Path("D:/claudecode/scratch/sm-loop/test_sm42_out")
SM42_TPL = "acceptance_summary@v1"

# 金样（字面量，独立于产品常量）：字段区固定行序 = 产品渲染合同
SM42_GOLDEN_FIELDS = [
    "隐患描述", "隐患等级", "状态", "整改责任人", "责任部门",
    "验收确认者", "验收时间", "验收批注", "授权主体", "授权规则",
    "改判轮次", "反馈轮次", "摘要版本", "数据快照哈希",
]
SM42_GOLDEN_EVIDENCE_HEADER = ["序号", "时间", "事件", "阶段流转", "操作者", "批注"]


def _sm42_fixture(root: Path):
    """合成"已验收"隐患工单：授权快照(SM-20 mandate) + 过程流转 + 改判(patch) +
    反馈一轮 + 验收通过。返回 (store, ticket_id)。"""
    from core.audit_store import AuditStore
    from core import admission

    store = AuditStore(str(root / "audit.db"))
    mandate = admission.mandate_snapshot(
        source_type="hazard_confirm", strength="explicit", subject_id="u-A",
        scopes=["plant-a"], rule_id="hazard.auto.major", basis="用户确认",
        proposal_id="sm42-prop-1", evidence_ids=["ev-1"])
    tid = store.create_ticket(
        "一号库房灭火器压力表失效整改", type_="hazard", assignee="李保管",
        source_request_id="RID-SM42-1",
        brief={"intent": "hazard_report", "urgency": "medium",
               "entities": {"area": ["一号库房"]},
               "message_digest": "一号库房灭火器压力表指针处于红区",
               "routing_reason": "sm42-fixture", "request_id": "RID-SM42-1"},
        actor="system", user_id="u-A", severity="general",
        responsible_dept="仓储部", mandate=mandate)
    store.transition_ticket(tid, "discuss", actor="王主任", note="方案研讨通过")
    store.transition_ticket(tid, "execute", actor="李保管", note="开始整改")
    r = store.update_ticket_fields(tid, {"severity": "major"}, actor="admin",
                                   note="复核改判为重大隐患")
    assert r.get("ok"), f"夹具改判失败: {r}"
    store.transition_ticket(tid, "feedback", actor="张安全", note="整改证据待补充")
    r = store.transition_ticket(tid, "accept", actor="王主任", note="现场核验通过")
    assert r.get("ok"), f"夹具验收失败: {r}"
    return store, tid


def _sha256_file(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sm42_core_tests():
    """S1/S2/S4：store 级（不经 HTTP），覆盖字段一致性 / 金样 / 重复导出与归档不可篡改。"""
    from core import ledger_export as lx
    store = None   # 夹具失败时 finally 不至于 NameError

    if SM42_OUT.exists():
        shutil.rmtree(str(SM42_OUT), ignore_errors=True)
    SM42_OUT.mkdir(parents=True, exist_ok=True)

    # ── S1 合成样例字段与 DB 一致 ────────────────────────────────────────
    try:
        store, tid = _sm42_fixture(SM42_OUT / "s1")
        fields = lx.build_acceptance_summary_fields(store, tid)
        record("S1: 摘要字段可从真实 AuditStore 构建", isinstance(fields, dict),
               f"fields={type(fields)}")
        t = store.get_ticket(tid)
        tl = store.get_ticket_timeline(tid) or []
        record("S1: 状态与 DB 一致（stage/stage_label/status）",
               fields["status"]["stage"] == t["stage"] == "accept"
               and fields["status"]["status"] == t["status"] == "closed"
               and fields["status"]["stage_label"] == "验收通过",
               f"fields={fields['status']} db={t['stage']}/{t['status']}")
        record("S1: 责任方与 DB 一致（整改责任人+责任部门）",
               fields["responsible"]["assignee"] == t["assignee"] == "李保管"
               and fields["responsible"]["responsible_dept"] == t["responsible_dept"] == "仓储部",
               f"fields={fields['responsible']}")
        record("S1: 证据链与 DB 时间线逐条一致（条数+ts+action+actor+批注）",
               len(fields["evidence"]) == len(tl)
               and all(e["ts"] == tl[i]["ts"] and e["action"] == tl[i]["action"]
                       and e["actor"] == tl[i]["actor"] and e["note"] == tl[i]["note"]
                       for i, e in enumerate(fields["evidence"])),
               f"摘要={len(fields['evidence'])}条 DB={len(tl)}条")
        last_accept = [e for e in tl if e["action"] == "stage_change"
                       and e["to_stage"] == "accept"][-1]
        record("S1: 确认者=最后验收流转的 actor（DB 时间线）",
               fields["confirmer"]["actor"] == last_accept["actor"] == "王主任"
               and fields["confirmer"]["note"] == "现场核验通过",
               f"confirmer={fields['confirmer']}")
        record("S1: 授权快照与 DB mandate 一致（rule_id/subject/快照状态）",
               fields["authorization"]["mandate"].get("rule_id") == "hazard.auto.major"
               and fields["authorization"]["mandate"].get("subject_id") == "u-A"
               and fields["authorization"]["mandate_status"] == "bound",
               f"auth={fields['authorization']}")
        patches = [e for e in tl if e["action"] == "patch"]
        record("S1: 改判轮次与 DB patch 事件一致（含前后值）",
               fields["revisions"] == [
                   {"ts": e["ts"], "actor": e["actor"], "note": e["note"],
                    "detail": e["detail"]} for e in patches]
               and len(fields["revisions"]) == 1
               and fields["revisions"][0]["detail"].get("severity", {}).get("to") == "major",
               f"revisions={fields['revisions']}")
        record("S1: 反馈轮次与 DB 流转一致", fields["feedback_rounds"] == 1,
               f"feedback_rounds={fields['feedback_rounds']}")

        # ── S2 金样（真实导出产物逐格对照字面量金样）───────────────────────
        pv = lx.preview_acceptance_summary(store, tid, SM42_TPL)
        record("S2: 预览返回 data_hash 与模板", pv.get("ok") is True
               and len(pv.get("data_hash") or "") == 64
               and pv.get("template_id") == SM42_TPL, f"pv={ {k: pv.get(k) for k in ('ok','data_hash','template_id')} }")
        r = lx.export_acceptance_summary(store, tid, pv["data_hash"], SM42_TPL,
                                         SM42_OUT / "s1", approved_by="admin")
        record("S2: 授权后导出成功并带版本/产物 hash", r.get("ok") is True
               and r.get("version") == 1 and len(r.get("file_hash") or "") == 64,
               f"r={r}")
        import openpyxl
        xlsx_path = Path(r["file_path"])
        wb = openpyxl.load_workbook(str(xlsx_path))
        ws = wb[wb.sheetnames[0]]
        record("S2: 金样 A1 标题", ws.cell(1, 1).value == f"整改验收摘要（工单 #{tid}）",
               f"A1={ws.cell(1, 1).value!r}")
        meta_cell = str(ws.cell(2, 1).value or "")
        record("S2: 金样 A2 元信息（模板+版本+摘要哈希）",
               SM42_TPL in meta_cell and "v1" in meta_cell and pv["data_hash"][:16] in meta_cell,
               f"A2={meta_cell!r}")
        basis_cell = str(ws.cell(3, 1).value or "")
        record("S2: 金样 A3 依据行含法律依据与授权状态",
               "安全生产法" in basis_cell and "bound" in basis_cell, f"A3={basis_cell!r}")
        record("S2: 金样表头（项目,内容）",
               [ws.cell(4, 1).value, ws.cell(4, 2).value] == ["项目", "内容"],
               f"row4={[ws.cell(4, 1).value, ws.cell(4, 2).value]}")
        labels = [ws.cell(5 + i, 1).value for i in range(len(SM42_GOLDEN_FIELDS))]
        record("S2: 金样字段行序逐行一致", labels == SM42_GOLDEN_FIELDS,
               f"实际={labels}")
        values = {ws.cell(5 + i, 1).value: ws.cell(5 + i, 2).value
                  for i in range(len(SM42_GOLDEN_FIELDS))}
        record("S2: 金样字段值（状态/责任方/确认者/版本）",
               values["状态"] == "验收通过（closed）" and values["整改责任人"] == "李保管"
               and values["责任部门"] == "仓储部" and values["验收确认者"] == "王主任"
               and values["摘要版本"] == "v1",
               f"values={ {k: values[k] for k in ('状态','整改责任人','责任部门','验收确认者','摘要版本')} }")
        record("S2: 金样 验收时间格式（YYYY-MM-DD HH:MM:SS）",
               bool(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", str(values["验收时间"]))),
               f"验收时间={values['验收时间']!r}")
        ev_head_row = 5 + len(SM42_GOLDEN_FIELDS) + 1   # 字段区后空 1 行
        ev_header = [ws.cell(ev_head_row, c).value for c in range(1, 7)]
        record("S2: 金样证据区表头", ev_header == SM42_GOLDEN_EVIDENCE_HEADER,
               f"row{ev_head_row}={ev_header}")
        ev_rows = sum(1 for rr in range(ev_head_row + 1, ws.max_row + 1)
                      if ws.cell(rr, 1).value is not None)
        record("S2: 金样证据区行数=DB 时间线条数", ev_rows == len(tl),
               f"证据行={ev_rows} DB={len(tl)}")
        record("S2: 数据快照 JSON 落盘且含字段+哈希",
               Path(r["snapshot_path"]).exists()
               and json.loads(Path(r["snapshot_path"]).read_text(encoding="utf-8"))["fields"]
               == fields
               and json.loads(Path(r["snapshot_path"]).read_text(encoding="utf-8"))["data_hash"]
               == pv["data_hash"],
               f"snapshot={r.get('snapshot_path')}")
        record("S2: manifest 记录产物与快照 sha256",
               store.get_acceptance_export(r["export_id"])["file_hash"] == _sha256_file(xlsx_path)
               and store.get_acceptance_export(r["export_id"])["snapshot_hash"]
               == _sha256_file(r["snapshot_path"]),
               f"export_id={r['export_id']}")
    except Exception as e:
        record("S1/S2: 合成样例导出链路", False, f"{type(e).__name__}: {e}")
    finally:
        if store is not None:
            try:
                store.close()   # Windows：未关连接会让下方 rmtree 静默残留
            except Exception:
                pass

    # ── S4 重复导出语义一致 + 归档不可篡改 ────────────────────────────────
    try:
        store, tid = _sm42_fixture(SM42_OUT / "s4")
        def _export_once():
            pv = lx.preview_acceptance_summary(store, tid, SM42_TPL)
            assert pv.get("ok"), f"预览失败: {pv}"
            r = lx.export_acceptance_summary(store, tid, pv["data_hash"], SM42_TPL,
                                             SM42_OUT / "s4", approved_by="admin")
            assert r.get("ok"), f"导出失败: {r}"
            return pv, r
        pv1, v1 = _export_once()
        pv2, v2 = _export_once()
        record("S4: 同一状态重复导出 data_hash 一致（语义一致）",
               pv1["data_hash"] == pv2["data_hash"] == v1["data_hash"] == v2["data_hash"],
               f"v1={v1['data_hash'][:12]} v2={v2['data_hash'][:12]}")
        record("S4: 重复导出版本递增且都可追溯",
               v1["version"] == 1 and v2["version"] == 2
               and len(store.list_acceptance_exports(tid)) == 2,
               f"versions={v1['version']},{v2['version']}")
        v1_bytes = _sha256_file(v1["file_path"])
        record("S4: 重复导出不覆写旧产物（两份文件并存且 v1 字节未变）",
               Path(v1["file_path"]).exists() and Path(v2["file_path"]).exists()
               and v1["file_path"] != v2["file_path"]
               and _sha256_file(v1["file_path"]) == v1_bytes)
        # 归档后继续改工单：验收通过 → 反馈 → 再验收（新增时间线事件）
        r1 = store.transition_ticket(tid, "feedback", actor="王主任",
                                     note="验收后追加整改说明", operator_is_admin=True)
        r2 = store.transition_ticket(tid, "accept", actor="王主任", note="复核后重新验收")
        record("S4: 夹具可对已归档工单继续流转", r1.get("ok") and r2.get("ok"),
               f"r1={r1} r2={r2}")
        record("S4: 归档快照后改工单，旧产物字节 sha256 不变（未篡改）",
               _sha256_file(v1["file_path"]) == v1_bytes
               and store.get_acceptance_export(v1["export_id"])["file_hash"] == v1_bytes)
        pv3, v3 = _export_once()
        record("S4: 工单变化后 data_hash 变化且新版本正常归档",
               pv3["data_hash"] != pv1["data_hash"] and v3["version"] == 3
               and len(store.list_acceptance_exports(tid)) == 3,
               f"v3_hash={v3['data_hash'][:12]} version={v3['version']}")
        record("S4: 全部历史版本产物完好（v1/v2 逐字节核对 manifest hash）",
               all(store.get_acceptance_export(x["export_id"])["file_hash"]
                   == _sha256_file(x["file_path"]) for x in (v1, v2, v3)))
    except Exception as e:
        record("S4: 重复导出与归档保护", False, f"{type(e).__name__}: {e}")
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


def sm42_api_tests():
    """S3：真实 HTTP 入口（TestClient）。未验收/过期审批/模板不匹配/渲染失败/跨主体下载。"""
    store = None
    if SM42_OUT.exists():
        shutil.rmtree(str(SM42_OUT), ignore_errors=True)
    SM42_OUT.mkdir(parents=True, exist_ok=True)
    access, admin = "sm42-access-1", "sm42-admin-1"
    users = [
        {"token": "sm42-tok-a", "subject_id": "u-A", "roles": ["reporter"], "scopes": ["plant-a"]},
        {"token": "sm42-tok-b", "subject_id": "u-B", "roles": ["reporter"], "scopes": ["plant-b"]},
        {"token": "sm42-tok-adm", "subject_id": "u-ADM", "roles": ["admin"], "scopes": ["*"]},
    ]
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_EMERGENCY_FASTPATH": "1",
        "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
        "SAFETYMIND_OUTBOX_ENABLED": "0",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "API_HOST": "127.0.0.1",
        "SAFETYMIND_DEPLOYED": "1",
        "ADMIN_TOKEN": admin,
        "ACCESS_TOKEN": access,
        "SAFETYMIND_USERS": json.dumps(users, ensure_ascii=False),
        "AUDIT_DB_PATH": str(SM42_OUT / "api_audit.db"),
        "SAFETYMIND_LEDGER_OUT_DIR": str(SM42_OUT / "api_out"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(SM42_OUT / "convs"),
        "CHROMA_HOST": "127.0.0.1",
        "CHROMA_PORT": "8973",
        "CHROMA_PERSIST_DIRECTORY": str(SM42_OUT / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(SM42_OUT / "kb_chroma"),
        "PROMETHEUS_PORT": "0",
        "AUDIT_ENABLED": "1",
    })
    try:
        import api.main as api_main
    except Exception as e:
        record("S3: api.main 可离线导入", False, f"{type(e).__name__}: {e}")
        return

    def ahdr(user_token="sm42-tok-adm"):
        h = {"X-Access-Token": access, "X-Admin-Token": admin}
        if user_token:
            h["X-User-Token"] = user_token
        return h

    try:
        from fastapi.testclient import TestClient
        from core import ledger_export as lx
        client = TestClient(api_main.app)   # 不进 lifespan：夹具组件即全部依赖（role-matrix 同款）
        store, tid = _sm42_fixture(SM42_OUT / "api_fixture")
        api_main._audit = store             # 夹具存储注入（对齐 test_role_matrix_api 模式）
        record("S3: 夹具工单已注入 API 存储", api_main._audit.get_ticket(tid) is not None,
               f"tid={tid}")

        # ── 未验收 ───────────────────────────────────────────────────────
        tid_raw = api_main._audit.create_ticket(
            "未验收工单（执行中）", type_="hazard", assignee="赵设备",
            brief={"intent": "hazard_report", "urgency": "high",
                   "entities": {}, "message_digest": "反应釜安全阀超期",
                   "routing_reason": "sm42-fixture-raw", "request_id": "RID-SM42-RAW"},
            actor="system", user_id="u-B", severity="major", responsible_dept="设备动力部")
        r = client.post("/ledger/acceptance-summary/preview", json={"ticket_id": tid_raw},
                        headers=ahdr())
        record("S3: 未验收工单预览被拒（409 not_accepted）",
               r.status_code == 409 and "验收" in r.json().get("detail", ""),
               f"code={r.status_code} body={r.text[:140]}")
        r = client.post("/ledger/acceptance-summary/export",
                        json={"ticket_id": tid_raw, "approval": "0" * 64},
                        headers=ahdr())
        record("S3: 未验收工单导出被拒（409）且不落产物",
               r.status_code == 409 and not any(
                   (SM42_OUT / "api_out").glob(f"*工单{tid_raw}*")),
               f"code={r.status_code}")

        # ── 正常两步：预览 → 授权导出 ────────────────────────────────────
        r = client.post("/ledger/acceptance-summary/preview", json={"ticket_id": tid},
                        headers=ahdr())
        record("S3: 预览入口 200 且带 data_hash/字段/模板",
               r.status_code == 200 and len(r.json().get("data_hash", "")) == 64
               and "fields" in r.json() and r.json().get("template_id") == SM42_TPL,
               f"code={r.status_code} body={r.text[:160]}")
        approval = r.json()["data_hash"]
        r2 = client.post("/ledger/acceptance-summary/export",
                         json={"ticket_id": tid, "approval": approval},
                         headers=ahdr())
        record("S3: 授权导出入口 200（版本 1 + export_id + 文件真实落盘）",
               r2.status_code == 200 and r2.json().get("version") == 1
               and Path(r2.json().get("file_path", "")).exists(),
               f"code={r2.status_code} body={r2.text[:200]}")
        export_id = r2.json().get("export_id")

        # ── 过期审批：预览后改判 → 旧 approval 拒绝 ──────────────────────
        r = client.post("/ledger/acceptance-summary/preview", json={"ticket_id": tid},
                        headers=ahdr())
        stale = r.json()["data_hash"]
        rp = client.patch(f"/tickets/{tid}", json={"severity": "general", "note": "验收摘要复核改判"},
                          headers=ahdr())
        record("S3: 夹具改判成功（PATCH 真实入口）", rp.status_code == 200,
               f"code={rp.status_code} body={rp.text[:120]}")
        r = client.post("/ledger/acceptance-summary/export",
                        json={"ticket_id": tid, "approval": stale}, headers=ahdr())
        record("S3: 过期审批被拒（409 expired_approval，零落盘）",
               r.status_code == 409 and "过期" in r.json().get("detail", "")
               and not any((SM42_OUT / "api_out").glob(f"*工单{tid}_v2*")),
               f"code={r.status_code} body={r.text[:160]}")

        # ── 模板不匹配 ───────────────────────────────────────────────────
        r = client.post("/ledger/acceptance-summary/preview", json={"ticket_id": tid},
                        headers=ahdr())
        approval2 = r.json()["data_hash"]
        r = client.post("/ledger/acceptance-summary/export",
                        json={"ticket_id": tid, "approval": approval2,
                              "template_id": "acceptance_summary@docx-v0"},
                        headers=ahdr())
        record("S3: 模板不匹配被拒（409 template_mismatch）",
               r.status_code == 409 and "模板" in r.json().get("detail", ""),
               f"code={r.status_code} body={r.text[:160]}")

        # ── 渲染失败：seam 注入，结构化失败且零残留 ───────────────────────
        rows_before = len(api_main._audit.list_acceptance_exports(tid))
        files_before = {p.name for p in (SM42_OUT / "api_out").iterdir()}
        orig_render = lx.render_acceptance_summary_xlsx
        def _boom(*a, **k):
            raise RuntimeError("注入的渲染故障")
        lx.render_acceptance_summary_xlsx = _boom
        try:
            r = client.post("/ledger/acceptance-summary/export",
                            json={"ticket_id": tid, "approval": approval2}, headers=ahdr())
        finally:
            lx.render_acceptance_summary_xlsx = orig_render
        record("S3: 渲染失败返回结构化失败",
               r.status_code == 500 and "渲染" in r.json().get("detail", ""),
               f"code={r.status_code} body={r.text[:160]}")
        files_after = {p.name for p in (SM42_OUT / "api_out").iterdir()}
        record("S3: 渲染失败不落 manifest 行、无新增半成品文件",
               len(api_main._audit.list_acceptance_exports(tid)) == rows_before
               and not (files_after - files_before),
               f"rows={len(api_main._audit.list_acceptance_exports(tid))}"
               f" 新增文件={sorted(files_after - files_before)}")

        # ── 下载授权 ─────────────────────────────────────────────────────
        r_none = client.get(f"/ledger/acceptance-summary/{export_id}/download",
                            headers={"X-Admin-Token": admin})
        record("S3: 匿名（无访问令牌）下载 401", r_none.status_code == 401,
               f"code={r_none.status_code}")
        r_b = client.get(f"/ledger/acceptance-summary/{export_id}/download",
                         headers={"X-Access-Token": access, "X-Admin-Token": admin,
                                  "X-User-Token": "sm42-tok-b"})
        record("S3: 跨主体下载被拒（403，他人主体）", r_b.status_code == 403,
               f"code={r_b.status_code} body={r_b.text[:140]}")
        r_a = client.get(f"/ledger/acceptance-summary/{export_id}/download",
                         headers={"X-Access-Token": access, "X-Admin-Token": admin,
                                  "X-User-Token": "sm42-tok-a"})
        r_adm = client.get(f"/ledger/acceptance-summary/{export_id}/download",
                           headers={"X-Access-Token": access, "X-Admin-Token": admin,
                                    "X-User-Token": "sm42-tok-adm"})
        disk = Path(r2.json()["file_path"]).read_bytes()
        record("S3: 归属主体下载放行且字节与产物一致",
               r_a.status_code == 200 and r_a.content == disk,
               f"code={r_a.status_code}")
        record("S3: 管理员下载放行", r_adm.status_code == 200,
               f"code={r_adm.status_code}")
        r404 = client.get("/ledger/acceptance-summary/999999/download", headers=ahdr())
        record("S3: 未知产物 404", r404.status_code == 404, f"code={r404.status_code}")
    except Exception as e:
        record("S3: 真实入口测试链路", False, f"{type(e).__name__}: {e}")
    finally:
        if store is not None:
            try:
                store.close()   # 先关 SQLite 连接（Windows 文件锁），再清目录
            except Exception:
                pass
        shutil.rmtree(str(SM42_OUT), ignore_errors=True)


def sm42_main():
    sm42_core_tests()
    sm42_api_tests()


if __name__ == "__main__":
    main()
