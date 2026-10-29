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
