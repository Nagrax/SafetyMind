# -*- coding: utf-8 -*-
"""安全台账文件生成器：把台账清单/隐患记录真正产出为 .xlsx / .docx。

## 为什么需要（用户明确指出的真实痛点）

安全员日常用 Office/WPS。当前 `safety_ledger_assistant` skill 只会"告诉你要准备哪些文件"，
但安全员真正要交上去的是**文件本身**。缺这一层，"台账助手"就停留在聊天层面。

## 依赖策略（沿用仓库既有权衡）

主 venv 不装 Office 重库（requirements.txt 明确把 DOCX/XLSX/PPTX 隔离在解析工具链 venv）。
本模块因此：
  1. **openpyxl 优先**（主环境已有，3.1.5）→ .xlsx 可零新增依赖产出；
  2. **python-docx 惰性导入**，缺失时抛带说明的 RuntimeError，而不是崩溃或静默降级；
  3. 提供 `--check` 报告当前环境能产出哪些格式。

## 产物落盘（铁律 7：一切只进 D 盘）

默认输出到 D:/SafetyMindData/台账输出/，文件名带时间戳与台账类型，便于版本区分。

## 用法

    # 能力自检
    <venv python> core/ledger_export.py --check

    # 生成隐患台账（xlsx）
    <venv python> core/ledger_export.py --ledger hazard --format xlsx

    # 生成安全检查记录（带示例数据的 docx）
    <venv python> core/ledger_export.py --ledger inspection --format docx --demo
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_OUT = Path("D:/SafetyMindData/台账输出")

# ── 台账模板定义（立足《安全生产法》法定台账口径）──────────────────────────
# 每类台账给出：中文名、列定义（xlsx）或章节结构（docx）、示例行
LEDGER_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "hazard": {
        "name": "隐患排查治理台账",
        "legal_basis": "《安全生产法》第四十一条：生产经营单位应当建立健全并落实生产安全事故隐患排查治理制度",
        "columns": [
            ("序号", 6), ("发现日期", 12), ("隐患地点", 18), ("隐患描述", 30),
            ("隐患等级", 10), ("排查人", 10), ("整改责任人", 12),
            ("整改期限", 12), ("整改措施", 30), ("整改完成日期", 12),
            ("验收人", 10), ("验收结果", 12), ("备注", 16),
        ],
        "demo_rows": [
            [1, "2026-10-20", "一号库房", "灭火器压力表指针处于红区，已失效",
             "一般隐患", "张安全", "李保管", "2026-10-25",
             "更换同型号干粉灭火器 2 具，并补齐检查卡", "2026-10-22", "王主任", "已闭环", ""],
            [2, "2026-10-21", "反应车间", "反应釜安全阀超期未校验（上次校验 2025-09）",
             "重大隐患", "张安全", "赵设备", "2026-11-05",
             "联系有资质机构校验，校验前降负荷运行并加强巡检", "", "", "整改中", "需报安环部备案"],
        ],
    },
    "inspection": {
        "name": "安全检查记录",
        "legal_basis": "《安全生产法》第四十四条：生产经营单位应当开展经常性安全检查",
        "columns": [
            ("序号", 6), ("检查日期", 12), ("检查类型", 14), ("检查区域", 16),
            ("检查内容", 26), ("发现问题", 30), ("处理意见", 26),
            ("检查人", 10), ("被检查部门确认", 14),
        ],
        "demo_rows": [
            [1, "2026-10-22", "日常检查", "危化品仓库",
             "通风、消防器材、标识、存量", "2 号货架标识脱落；通风机运转正常",
             "3 日内补贴标识，通风机保持常开", "张安全", "仓储部"],
        ],
    },
    "training": {
        "name": "安全培训台账",
        "legal_basis": "《安全生产法》第二十八条：应当对从业人员进行安全生产教育和培训",
        "columns": [
            ("序号", 6), ("培训日期", 12), ("培训主题", 22), ("培训类型", 12),
            ("授课人", 10), ("培训学时", 8), ("参训人数", 8),
            ("签到表编号", 14), ("考核方式", 12), ("考核合格人数", 12), ("备注", 18),
        ],
        "demo_rows": [
            [1, "2026-10-15", "动火作业安全规程", "专项培训", "李工", 2, 18,
             "PX-2026-010", "闭卷笔试", 18, ""],
        ],
    },
    "ticket": {
        "name": "作业票管理台账",
        "legal_basis": "GB 30871《危险化学品企业特殊作业安全规范》",
        "columns": [
            ("序号", 6), ("作业票编号", 16), ("作业类型", 12), ("作业地点", 18),
            ("申请单位", 14), ("作业开始", 12), ("作业结束", 12),
            ("审批人", 10), ("监护人", 10), ("作业人数", 8), ("是否按期关闭", 12), ("备注", 16),
        ],
        "demo_rows": [
            [1, "DH-2026-0087", "动火作业", "反应车间东侧", "维修班",
             "2026-10-20 08:00", "2026-10-20 16:00", "王主任", "张安全", 4, "是", "一级动火"],
        ],
    },
    "emergency_drill": {
        "name": "应急演练台账",
        "legal_basis": "《生产安全事故应急预案管理办法》第三十三条：每年至少组织一次综合应急预案演练",
        "columns": [
            ("序号", 6), ("演练日期", 12), ("演练名称", 22), ("演练类型", 12),
            ("演练地点", 16), ("参演人数", 8), ("组织人", 10),
            ("发现问题", 26), ("改进措施", 26), ("完成情况", 12),
        ],
        "demo_rows": [
            [1, "2026-09-18", "氨泄漏应急疏散", "专项演练", "合成车间",
             32, "刘应急", "疏散路线标识不清，2 人走错出口",
             "重绘疏散图并现场加装夜光指示", "已完成"],
        ],
    },
}

# docx 版本的章节结构（用于 inspection 这类叙述型记录）
DOCX_SECTIONS = {
    "inspection": [
        ("一、检查基本情况", "检查时间、检查人员、检查依据、检查范围"),
        ("二、检查内容与方法", "逐项列出检查项目及采用的检查方法（查证/现场查看/询问）"),
        ("三、发现的问题", "逐条记录问题、位置、依据条款、风险等级"),
        ("四、处理意见与整改要求", "针对每条问题给出整改措施、责任人、期限"),
        ("五、检查结论", "总体评价与后续跟踪安排"),
        ("检查人签字", "　　　　　　　　　　　　日期："),
        ("被检查部门负责人签字", "　　　　　　　　　　　　日期："),
    ],
    "hazard": [
        ("一、隐患基本情况", "发现时间、地点、发现人、隐患描述"),
        ("二、隐患定级依据", "依据《安全生产法》与行业标准判定等级（一般/重大）"),
        ("三、整改方案", "整改措施、责任人、资金来源、完成期限"),
        ("四、整改验收", "验收人、验收日期、验收结论"),
        ("五、举一反三", "同类隐患排查范围与结果"),
    ],
}


def capability_report() -> Dict[str, Any]:
    """报告当前环境能产出哪些格式。"""
    caps = {"xlsx": False, "docx": False, "xlsx_version": None, "docx_error": None}
    try:
        import openpyxl
        caps["xlsx"] = True
        caps["xlsx_version"] = getattr(openpyxl, "__version__", "?")
    except Exception as e:
        caps["xlsx_error"] = f"{type(e).__name__}: {e}"
    try:
        import docx  # noqa: F401
        caps["docx"] = True
    except Exception as e:
        caps["docx_error"] = f"{type(e).__name__}: {e}（python-docx 未装在主 venv）"
    return caps


def _safe_name(s: str) -> str:
    return "".join(c for c in s if c not in '\\/:*?"<>|').strip()


def export_xlsx(ledger: str, out_dir: Path, with_demo: bool = False) -> Path:
    """生成 .xlsx 台账。openpyxl 在主环境已有，零新增依赖。"""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    tpl = LEDGER_TEMPLATES[ledger]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = tpl["name"][:31]

    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="DCE6F1")

    # 标题行
    ncol = len(tpl["columns"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    c = ws.cell(row=1, column=1, value=tpl["name"])
    c.font = Font(bold=True, size=14)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 26

    # 依据行
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    c = ws.cell(row=2, column=1, value=f"法律依据：{tpl['legal_basis']}")
    c.font = Font(size=9, italic=True, color="666666")
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # 表头
    for j, (title, width) in enumerate(tpl["columns"], start=1):
        cell = ws.cell(row=3, column=j, value=title)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
        ws.column_dimensions[get_column_letter(j)].width = width
    ws.row_dimensions[3].height = 24

    # 数据行
    rows = tpl["demo_rows"] if with_demo else []
    for i, row in enumerate(rows, start=4):
        for j, val in enumerate(row, start=1):
            cell = ws.cell(row=i, column=j, value=val)
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    # 空白填写行（打印用）
    start_blank = 4 + len(rows)
    for i in range(start_blank, start_blank + 15):
        for j in range(1, ncol + 1):
            cell = ws.cell(row=i, column=j, value=None)
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    # 说明页
    ws2 = wb.create_sheet("填写说明")
    notes = [
        [tpl["name"] + " — 填写说明"],
        ["1", "本表格按法定台账口径生成，字段可依本单位实际增减，但法定必填项不得删除。"],
        ["2", "隐患等级按《安全生产法》与行业判定标准填写（一般隐患/重大隐患）。"],
        ["3", "重大隐患须同时报安环部备案，并附整改方案与资金落实说明。"],
        ["4", "整改闭环必须有验收人签字与验收日期，不得空置。"],
        ["5", f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"],
        ["6", "本文件为模板，正式台账请以本单位受控文件编号归档。"],
    ]
    for i, row in enumerate(notes, start=1):
        for j, val in enumerate(row, start=1):
            ws2.cell(row=i, column=j, value=val)
    ws2.column_dimensions["A"].width = 6
    ws2.column_dimensions["B"].width = 100

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_safe_name(tpl['name'])}_{time.strftime('%Y%m%d-%H%M%S')}.xlsx"
    wb.save(str(path))
    return path


def export_docx(ledger: str, out_dir: Path, with_demo: bool = False) -> Path:
    """生成 .docx 记录。python-docx 惰性导入，缺失时给可执行的补救说明。"""
    try:
        import docx
        from docx.shared import Pt
    except ImportError as e:
        raise RuntimeError(
            "生成 .docx 需要 python-docx，但当前主 venv 未安装。"
            "仓库有意把 Office 重库隔离在解析工具链 venv（见 requirements.txt 注释），"
            "可选做法：\n"
            "  1) 用解析工具链 venv 运行：D:/SafetyMindData/venv/Scripts/python.exe core/ledger_export.py ...\n"
            "  2) 或在主 venv 安装：.venv/Scripts/pip install python-docx\n"
            "  3) 或改用 --format xlsx（主环境已有 openpyxl，零新增依赖）"
        ) from e

    tpl = LEDGER_TEMPLATES[ledger]
    doc = docx.Document()

    h = doc.add_heading(tpl["name"], level=0)
    h.alignment = 1  # 居中

    p = doc.add_paragraph(f"法律依据：{tpl['legal_basis']}")
    p.runs[0].font.size = Pt(9)
    p.runs[0].italic = True

    doc.add_paragraph(f"填报单位：______________________    填报日期：____年__月__日")

    sections = DOCX_SECTIONS.get(ledger, [("一、基本情况", "说明背景与范围")])
    for title, hint in sections:
        doc.add_heading(title, level=1)
        if with_demo and ledger in DOCX_SECTIONS and title.startswith("一"):
            doc.add_paragraph("（示例）" + hint)
        else:
            doc.add_paragraph(f"（请填写：{hint}）")
            doc.add_paragraph("")

    doc.add_paragraph("")
    note = doc.add_paragraph(
        f"说明：本文件由 SafetyMind 于 {time.strftime('%Y-%m-%d %H:%M:%S')} 生成，"
        "为模板初稿；正式归档请套用本单位受控文件编号与格式。")
    note.runs[0].font.size = Pt(8)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_safe_name(tpl['name'])}_{time.strftime('%Y%m%d-%H%M%S')}.docx"
    doc.save(str(path))
    return path


def export(ledger: str, fmt: str = "xlsx", out_dir: Optional[Path] = None,
           with_demo: bool = False) -> Dict[str, Any]:
    """供 API/skill 调用的统一入口。返回 {ok, path, format, ledger, error?}。"""
    out = Path(out_dir) if out_dir else DEFAULT_OUT
    if ledger not in LEDGER_TEMPLATES:
        return {"ok": False, "error": f"未知台账类型: {ledger}",
                "available": list(LEDGER_TEMPLATES)}
    try:
        if fmt == "xlsx":
            p = export_xlsx(ledger, out, with_demo)
        elif fmt == "docx":
            p = export_docx(ledger, out, with_demo)
        else:
            return {"ok": False, "error": f"不支持的格式: {fmt}（可选 xlsx/docx）"}
        return {"ok": True, "path": str(p), "format": fmt, "ledger": ledger,
                "size": p.stat().st_size, "name": LEDGER_TEMPLATES[ledger]["name"]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "ledger": ledger, "format": fmt}


# ═══ SM-42 整改验收摘要试点（2026-11-08）══════════════════════════════════════
# 试点口径（任务卡 SM-42 + 行动指南 CarlcareDOC 反哺点）：
#   - 只对 stage=accept（验收通过）的隐患工单出摘要；
#   - 生成从授权（mandate 快照）、改判（patch）、反馈（feedback）到验收（accept）的
#     版本化摘要；业务字段只从 AuditStore 读（状态/责任方/证据/版本/确认者）；
#   - 两步授权：preview 返回字段预览 + data_hash（业务字段 canonical JSON 的 sha256，
#     不含版本号/时间戳）；export 必须回传该哈希，生成前重读 DB 重算比对——
#     不一致 = 审批过期（预览后工单已变化），授权状态无表可篡改；
#   - 正式生成记录模板/数据快照及产物 hash：快照 JSON + xlsx 落盘，manifest 行
#     （acceptance_summary_exports）与时间线 acceptance_export 事件同 commit；
#   - 版本化归档：文件名带版本号，已归档产物绝不覆写（归档后继续改工单不会篡改旧产物）；
#   - 结构化产物先走现有格式（xlsx + JSON 快照）；DOCX 受控模板按任务卡 deferred，
#     待 DOCX 业务条件确认后再加（模板不匹配校验已为其预留）。
# 决策规则全文：scratch/sm-loop/handoff/SM-42/run-wf/decision.md（R1–R10）。

ACCEPTANCE_SUMMARY_TEMPLATE_ID = "acceptance_summary@v1"
ACCEPTANCE_SUMMARY_LEGAL_BASIS = (
    "《安全生产法》第四十一条：生产经营单位应当建立健全并落实生产安全事故隐患排查治理制度")

# 字段与布局金样（单一定义；tests/test_ledger_export.py 的金样用例持有独立字面量副本——
# 改这里必须同步金样并明白自己在改渲染合同）。
ACCEPTANCE_SUMMARY_FIELD_LABELS = (
    "隐患描述", "隐患等级", "状态", "整改责任人", "责任部门",
    "验收确认者", "验收时间", "验收批注", "授权主体", "授权规则",
    "改判轮次", "反馈轮次", "摘要版本", "数据快照哈希",
)
ACCEPTANCE_SUMMARY_EVIDENCE_HEADER = ("序号", "时间", "事件", "阶段流转", "操作者", "批注")


def _acceptance_canonical(obj: Any) -> str:
    """规范化序列化（与 core.audit_store.canonical_json 同规范：键排序+紧凑分隔符）。
    本模块刻意不反向依赖 audit_store（CLI 直跑/解析 venv 场景保持零模块依赖）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fmt_ts(ts: Any) -> str:
    try:
        f = float(ts)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f)) if f > 0 else ""
    except (TypeError, ValueError):
        return ""


def build_acceptance_summary_fields(store, ticket_id: int) -> Optional[Dict[str, Any]]:
    """从 AuditStore 读出摘要业务字段（R1：状态/责任方/证据/版本/确认者的数据源）。
    工单不存在返回 None；store 缺时间线按空链处理。纯读，无副作用。"""
    t = store.get_ticket(int(ticket_id))
    if t is None:
        return None
    tl = store.get_ticket_timeline(int(ticket_id)) or []
    try:
        mandate = json.loads(t.get("mandate_json") or "{}")
        if not isinstance(mandate, dict):
            mandate = {}
    except (ValueError, TypeError):
        mandate = {}
    accepts = [e for e in tl if e.get("action") == "stage_change"
               and e.get("to_stage") == "accept"]
    last_accept = accepts[-1] if accepts else {}
    patches = [e for e in tl if e.get("action") == "patch"]
    feedbacks = [e for e in tl if e.get("action") == "stage_change"
                 and e.get("to_stage") == "feedback"]
    # 证据链只承载"授权→改判→反馈→验收"业务链（任务卡口径）。acceptance_export 是
    # 导出动作自身的时间线痕（归档在 manifest + 时间线，可追溯），不进摘要业务字段——
    # 否则第二次导出会因"含第一次导出事件"而语义漂移，违背"重复导出语义一致"。
    evidence_src = [e for e in tl if e.get("action") != "acceptance_export"]
    return {
        "ticket_id": t.get("id"),
        "title": t.get("title", ""),
        "hazard_digest": t.get("message_digest", ""),
        "severity": t.get("severity", ""),
        "status": {"stage": t.get("stage", ""), "stage_label": t.get("stage_label", ""),
                   "status": t.get("status", "")},
        "responsible": {"assignee": t.get("assignee", ""),
                        "responsible_dept": t.get("responsible_dept", "")},
        "confirmer": {"actor": str(last_accept.get("actor") or ""),
                      "accepted_ts": last_accept.get("ts"),
                      "note": str(last_accept.get("note") or "")},
        "authorization": {"mandate_status": t.get("mandate_status", "legacy"),
                          "policy_version": t.get("policy_version", ""),
                          "mandate": mandate},
        "revisions": [{"ts": e.get("ts"), "actor": e.get("actor", ""),
                       "note": e.get("note", ""), "detail": e.get("detail", {})}
                      for e in patches],
        "feedback_rounds": len(feedbacks),
        "evidence": [{"ts": e.get("ts"), "action": e.get("action", ""),
                      "from_stage": e.get("from_stage", ""),
                      "to_stage": e.get("to_stage", ""),
                      "actor": e.get("actor", ""), "note": e.get("note", "")}
                     for e in evidence_src],
    }


def summary_data_hash(fields: Dict[str, Any]) -> str:
    """业务字段内容哈希（R2 内容审批的凭据）：canonical JSON + sha256。
    只覆盖业务字段——版本号/导出时刻等产物元数据不进哈希，重复导出同状态必同哈希。"""
    return hashlib.sha256(
        _acceptance_canonical(fields).encode("utf-8")).hexdigest()


def preview_acceptance_summary(store, ticket_id: int,
                               template_id: str = ACCEPTANCE_SUMMARY_TEMPLATE_ID) -> Dict[str, Any]:
    """两步授权第一步：返回字段预览 + data_hash。不写任何文件、不落任何行。"""
    if template_id != ACCEPTANCE_SUMMARY_TEMPLATE_ID:
        return {"ok": False, "code": "template_mismatch",
                "error": f"模板不匹配：请求 {template_id}，可用 "
                         f"{ACCEPTANCE_SUMMARY_TEMPLATE_ID}"}
    fields = build_acceptance_summary_fields(store, ticket_id)
    if fields is None:
        return {"ok": False, "code": "not_found", "error": f"工单不存在: {ticket_id}"}
    if fields["status"]["stage"] != "accept":
        return {"ok": False, "code": "not_accepted",
                "error": f"未验收工单不能导出整改验收摘要"
                         f"（当前阶段：{fields['status']['stage_label'] or fields['status']['stage']}）"
                         f"，请验收通过后再试"}
    return {"ok": True, "ticket_id": int(ticket_id), "template_id": template_id,
            "data_hash": summary_data_hash(fields), "fields": fields}


def _acceptance_field_rows(fields: Dict[str, Any], meta: Dict[str, Any]):
    """字段区行序（与 ACCEPTANCE_SUMMARY_FIELD_LABELS 一一对应）。"""
    mandate = fields["authorization"]["mandate"] or {}
    sev = str(fields.get("severity") or "")
    sev_label = {"major": "重大隐患", "general": "一般隐患"}.get(sev, sev or "未定级")
    st = fields["status"]
    cf = fields["confirmer"]
    return [
        ("隐患描述", fields.get("hazard_digest") or fields.get("title") or "—"),
        ("隐患等级", sev_label),
        ("状态", f"{st.get('stage_label') or st.get('stage') or '—'}"
                 f"（{st.get('status') or '—'}）"),
        ("整改责任人", fields["responsible"].get("assignee") or "—"),
        ("责任部门", fields["responsible"].get("responsible_dept") or "—"),
        ("验收确认者", cf.get("actor") or "—"),
        ("验收时间", _fmt_ts(cf.get("accepted_ts")) or "—"),
        ("验收批注", cf.get("note") or "—"),
        ("授权主体", str(mandate.get("subject_id") or "") or "—"),
        ("授权规则", str(mandate.get("rule_id") or "") or "—"),
        ("改判轮次", str(len(fields.get("revisions") or []))),
        ("反馈轮次", str(fields.get("feedback_rounds", 0))),
        ("摘要版本", f"v{meta['version']}"),
        ("数据快照哈希", meta["data_hash"]),
    ]


def render_acceptance_summary_xlsx(fields: Dict[str, Any], meta: Dict[str, Any],
                                   path: Path) -> None:
    """渲染验收摘要 .xlsx（SM-42 渲染 seam：测试可注入故障做渲染失败用例）。
    布局由本模块金样常量唯一定义；本函数不读 DB、不做授权判断。"""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    lay_sheet = "验收摘要"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = lay_sheet
    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="DCE6F1")
    ncol = len(ACCEPTANCE_SUMMARY_EVIDENCE_HEADER)

    # A1 标题
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    c = ws.cell(row=1, column=1, value=f"整改验收摘要（工单 #{fields['ticket_id']}）")
    c.font = Font(bold=True, size=14)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 26
    # A2 元信息（版本/模板/哈希只进元数据，不进 data_hash）
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    c = ws.cell(row=2, column=1,
                value=f"模板：{meta['template_id']} · 版本：v{meta['version']}"
                      f" · 摘要哈希：{meta['data_hash']}")
    c.font = Font(size=9, italic=True, color="666666")
    # A3 依据行
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncol)
    ws.cell(row=3, column=1,
            value=f"法律依据：{ACCEPTANCE_SUMMARY_LEGAL_BASIS}"
                  f" · 授权快照：{fields['authorization']['mandate_status']}"
                  f" · 策略版本：{fields['authorization'].get('policy_version') or '-'}")
    # 第 4 行表头
    for j, title in enumerate(("项目", "内容"), start=1):
        cell = ws.cell(row=4, column=j, value=title)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 66
    # 字段区（固定行序）
    for i, (label, value) in enumerate(_acceptance_field_rows(fields, meta)):
        r = 5 + i
        a = ws.cell(row=r, column=1, value=label)
        b = ws.cell(row=r, column=2, value=value)
        a.font = Font(bold=True)
        for cell in (a, b):
            cell.border = border
        b.alignment = Alignment(vertical="top", wrap_text=True)
    # 证据区（字段区后空 1 行）
    ev_head = 5 + len(ACCEPTANCE_SUMMARY_FIELD_LABELS) + 1
    for j, title in enumerate(ACCEPTANCE_SUMMARY_EVIDENCE_HEADER, start=1):
        cell = ws.cell(row=ev_head, column=j, value=title)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for i, e in enumerate(fields["evidence"]):
        r = ev_head + 1 + i
        flow = f"{e['from_stage'] or '—'}→{e['to_stage'] or '—'}" \
            if (e["from_stage"] or e["to_stage"]) else "—"
        for j, v in enumerate([i + 1, _fmt_ts(e["ts"]), e["action"], flow,
                               e["actor"], e["note"]], start=1):
            cell = ws.cell(row=r, column=j, value=v)
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for col, w in (("C", 16), ("D", 16), ("E", 10), ("F", 30)):
        ws.column_dimensions[col].width = w
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))


def export_acceptance_summary(store, ticket_id: int, approval: str,
                              template_id: str = ACCEPTANCE_SUMMARY_TEMPLATE_ID,
                              out_dir: Optional[Path] = None,
                              approved_by: str = "") -> Dict[str, Any]:
    """两步授权第二步：校验（模板/未验收/审批新鲜度）→ 渲染 → 快照 → manifest 归档。
    顺序保证失败零残留：先渲染（失败无文件），再快照，最后 manifest（冲突清理两文件）。
    产物文件名带版本号；manifest 已有该版本 = 已归档产物，绝不覆写（R4）。"""
    out = Path(out_dir) if out_dir else DEFAULT_OUT
    if template_id != ACCEPTANCE_SUMMARY_TEMPLATE_ID:
        return {"ok": False, "code": "template_mismatch",
                "error": f"模板不匹配：请求 {template_id}，审批对应"
                         f" {ACCEPTANCE_SUMMARY_TEMPLATE_ID}"}
    fields = build_acceptance_summary_fields(store, int(ticket_id))
    if fields is None:
        return {"ok": False, "code": "not_found", "error": f"工单不存在: {ticket_id}"}
    if fields["status"]["stage"] != "accept":
        return {"ok": False, "code": "not_accepted",
                "error": f"未验收工单不能导出整改验收摘要"
                         f"（当前阶段：{fields['status']['stage_label'] or fields['status']['stage']}）"
                         f"，请验收通过后再试"}
    data_hash = summary_data_hash(fields)
    if not str(approval or "").strip() or str(approval).strip() != data_hash:
        return {"ok": False, "code": "expired_approval",
                "error": "审批已过期（预览后工单已变化或审批为空），请重新预览后再导出"}
    version = store.next_acceptance_version(int(ticket_id))
    base = f"验收摘要_工单{int(ticket_id)}_v{version}"
    xlsx_path = out / f"{base}.xlsx"
    snapshot_path = out / f"{base}.json"
    # 归档保护（R4）：该版本已有 manifest 行 = 已归档产物，绝不覆写；
    # 无行的同名文件 = 崩溃窗口孤儿（未批准产物），可安全重写。
    if xlsx_path.exists() or snapshot_path.exists():
        already = store.list_acceptance_exports(int(ticket_id))
        if any(int(r.get("version") or 0) == version for r in already):
            return {"ok": False, "code": "version_conflict",
                    "error": f"摘要 v{version} 已归档且产物在册，拒绝覆写"}
    meta = {"template_id": template_id, "version": version, "data_hash": data_hash}
    try:
        render_acceptance_summary_xlsx(fields, meta, xlsx_path)
    except Exception as e:
        # 渲染失败：清理可能的半成品文件，manifest 零行（R9）
        try:
            xlsx_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "code": "render_failed",
                "error": f"验收摘要渲染失败: {type(e).__name__}: {e}"}
    snapshot = {"schema_version": 1, "template_id": template_id,
                "ticket_id": int(ticket_id), "version": version,
                "data_hash": data_hash, "fields": fields}
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    file_hash = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()
    snapshot_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    rec = store.record_acceptance_export(
        ticket_id=int(ticket_id), version=version, template_id=template_id,
        data_hash=data_hash, file_path=str(xlsx_path), file_hash=file_hash,
        snapshot_path=str(snapshot_path), snapshot_hash=snapshot_hash,
        approved_by=str(approved_by or ""))
    if not rec.get("ok"):
        # 并发归档撞唯一索引：清理本轮产物，零残留
        for p in (xlsx_path, snapshot_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": False, "code": str(rec.get("code") or "version_conflict"),
                "error": str(rec.get("error") or "归档冲突")}
    return {"ok": True, "export_id": rec["export_id"], "ticket_id": int(ticket_id),
            "version": version, "template_id": template_id, "data_hash": data_hash,
            "file_hash": file_hash, "snapshot_hash": snapshot_hash,
            "file_path": str(xlsx_path), "snapshot_path": str(snapshot_path)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告能力")
    ap.add_argument("--ledger", default="hazard", help="台账类型")
    ap.add_argument("--format", default="xlsx", choices=["xlsx", "docx"])
    ap.add_argument("--demo", action="store_true", help="带示例数据")
    ap.add_argument("--out", default="")
    ap.add_argument("--list", action="store_true", help="列出支持的台账类型")
    args = ap.parse_args()

    if args.check:
        caps = capability_report()
        print(json.dumps(caps, ensure_ascii=False, indent=2))
        print(f"\n可用台账类型：{list(LEDGER_TEMPLATES)}")
        return 0 if caps["xlsx"] else 1

    if args.list:
        for k, v in LEDGER_TEMPLATES.items():
            print(f"  {k:18s} {v['name']}（{len(v['columns'])} 列）")
        return 0

    out = Path(args.out) if args.out else None
    r = export(args.ledger, args.format, out, args.demo)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
