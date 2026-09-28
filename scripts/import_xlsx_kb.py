# -*- coding: utf-8 -*-
"""xlsx 知识库导入脚本：把厂内表格转为结构化文档批量写入 /knowledge/add。

用法：
  python scripts/import_xlsx_kb.py --base-url http://47.106.200.90/api/python \
      --file "2026《隐患排查依据大全》查找版 (1).xlsx" --source hazard-book [--dry-run]
  python scripts/import_xlsx_kb.py --base-url ... --file "安环部工作清单(1).xlsx" --source worklist [--dry-run]

分块策略（按知识库 chunk_size=500 邻近句重叠切分友好设计）：
  - hazard-book（隐患排查依据大全）：按 检查项大类+中类 分组，一组一篇文档，
    每行一条检查项（含小类/内容/依据/隐患级别）。
  - worklist（安环部工作清单）：按 工作模块（安全/环保等一级模块，前向填充）分组，
    每行一条任务（含子模块/任务/频次/完成时间）。
"""
import argparse
import sys
from pathlib import Path

import openpyxl
import requests


def _clean(v):
    return str(v).replace("\xa0", " ").strip() if v is not None else ""


def parse_hazard_book(path: str):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    groups = {}
    order = []
    cur_big, cur_mid = "", ""
    for row in ws.iter_rows(min_row=3, values_only=True):
        vals = [_clean(v) for v in (row[:7] if len(row) >= 7 else row)]
        if len(vals) < 6 or not vals[4]:
            continue
        _seq, big, mid, small, content, basis, level = (vals + [""] * 7)[:7]
        if not content or content.startswith("检查内容"):
            continue
        # 合并单元格：空值继承上一行的大类/中类/小类
        if big:
            cur_big = big
        if mid:
            cur_mid = mid or cur_big
        big, mid = cur_big or "未分类", cur_mid or cur_big or "未分类"
        key = (big, mid)
        if key not in groups:
            groups[key] = []
            order.append(key)
        line = f"- 【{(small or mid).strip()}】{content}"
        if basis:
            line += f"（依据：{basis}）"
        if level:
            line += f"［隐患级别：{level}］"
        groups[key].append(line)
    wb.close()
    docs = []
    for key in order:
        items = groups[key]
        big, mid = key
        # 超长组按 40 条/篇拆分：父文档返回有 1200 字符截断，单片过大检索会丢条目
        for part in range(0, len(items), 40):
            chunk = items[part:part + 40]
            suffix = f"（{part // 40 + 1}）" if len(items) > 40 else ""
            docs.append({
                "title": f"隐患排查依据·{big}·{mid}（{len(items)}项）{suffix}".replace("（{}项）（".format(len(items)), "（" + str(len(items)) + "项）（"),
                "content": f"【检查项大类：{big}｜检查项中类：{mid}】本篇 {len(chunk)} 条检查项（共 {len(items)} 条）：\n"
                           + "\n".join(chunk),
            })
    return docs


def parse_worklist(path: str):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    groups = {}
    order = []
    cur_module, cur_sub1 = "", ""
    for row in ws.iter_rows(min_row=3, values_only=True):
        vals = [_clean(v) for v in row]
        if not any(vals):
            continue
        seq = vals[0] if len(vals) > 0 else ""
        module = vals[1] if len(vals) > 1 else ""
        sub1 = vals[2] if len(vals) > 2 else ""
        sub2 = vals[3] if len(vals) > 3 else ""
        task = vals[4] if len(vals) > 4 else ""
        freq = vals[5] if len(vals) > 5 else ""
        deadline = vals[6] if len(vals) > 6 else ""
        if not task or task.startswith("工作任务"):
            continue
        if module:
            cur_module = module
        if sub1:
            cur_sub1 = sub1
        mod = cur_module or "未分类"
        s1 = cur_sub1 or mod
        key = (mod, s1)
        if key not in groups:
            groups[key] = []
            order.append(key)
        line = f"- 【{s1}{'·' + sub2 if sub2 else ''}】{task}"
        if freq:
            line += f"（频次：{freq}）"
        if deadline:
            line += f"［完成时间：{deadline}］"
        groups[key].append(line)
    wb.close()
    docs = []
    for key in order:
        items = groups[key]
        mod, s1 = key
        for part in range(0, len(items), 40):
            chunk = items[part:part + 40]
            suffix = f"（{part // 40 + 1}）" if len(items) > 40 else ""
            docs.append({
                "title": f"安环部工作清单·{mod}·{s1}（{len(items)}项）{suffix}",
                "content": f"【工作模块：{mod}｜子模块：{s1}】本篇 {len(chunk)} 项任务（共 {len(items)} 项）：\n"
                           + "\n".join(chunk),
            })
    return docs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="API 基址，如 http://47.106.200.90/api/python")
    ap.add_argument("--file", required=True)
    ap.add_argument("--source", choices=["hazard-book", "worklist"], required=True)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    docs = parse_hazard_book(args.file) if args.source == "hazard-book" else parse_worklist(args.file)
    total_items = sum(d["content"].count("\n- ") + 1 for d in docs)
    print(f"解析完成：{len(docs)} 篇文档 / 约 {total_items} 条目，来源={args.source}")
    for d in docs[:3]:
        print(f"  预览: {d['title']}")
        print(f"    {d['content'][:90]}...")

    if args.dry_run:
        print("dry-run：不写入。")
        return

    ok = fail = 0
    for i in range(0, len(docs), args.batch):
        chunk = docs[i:i + args.batch]
        try:
            r = requests.post(f"{args.base_url}/knowledge/add",
                              json={"documents": chunk}, timeout=120)
            r.raise_for_status()
            ok += len(chunk)
            print(f"[{i + len(chunk)}/{len(docs)}] 已写入", flush=True)
        except Exception as e:
            fail += len(chunk)
            print(f"[{i + 1}..{i + len(chunk)}] 失败: {e}", flush=True)
    print(f"完成：成功 {ok} 篇 / 失败 {fail} 篇")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
