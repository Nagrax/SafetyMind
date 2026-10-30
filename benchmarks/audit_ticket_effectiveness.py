# -*- coding: utf-8 -*-
"""工单闭环实效性审计：工单体系是否真的"闭环"，而不只是"有工单功能"。

## 为什么需要（立足真实安全管理）

一个工单系统"能建单"不等于"能闭环"。真实安全管理里，安环部考核的是：
  - 隐患是否**在期限内**整改完成（超期就是失职）
  - 是否有**验收人签字**（无验收=未闭环）
  - 是否**重复隐患**（同一地点同类问题反复出现=治标不治本）
  - 重大隐患是否**报备**（法定要求）

本脚本对隔离库里的工单数据做实效性审计，输出上面四项的实测数字。
没有数据时如实报告"样本不足"，绝不编造。

## 用法

    # 造一批覆盖各场景的工单再审计（sonde 模式，用于验证审计逻辑本身）
    <venv python> benchmarks/audit_ticket_effectiveness.py --seed

    # 审计真实库
    <venv python> benchmarks/audit_ticket_effectiveness.py --db data/audit.db

产物：scratch/sm-loop/runs/ticket_audit.json
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def audit(db_path: str) -> dict:
    from core.audit_store import AuditStore
    st = AuditStore(db_path)
    rows = st.list_tickets(limit=10000) if hasattr(st, "list_tickets") else []
    if not rows:
        # 兜底：直接读表
        try:
            cur = st._con.execute("SELECT * FROM tickets LIMIT 10000")
            rows = [dict(r) for r in cur.fetchall()]
        except Exception:
            rows = []

    now = time.time()
    total = len(rows)
    if total == 0:
        return {"total": 0, "note": "库内无工单，样本不足，四项指标均无法计算"}

    closed = [r for r in rows if str(r.get("status")) == "closed" or str(r.get("stage")) == "accept"]
    open_ = [r for r in rows if r not in closed]

    # ① 按期闭环率：有截止时间且已关闭的，判断关闭时间是否早于截止
    overdue_open = []
    ontime = 0
    for r in open_:
        due = r.get("due_ts") or r.get("deadline_ts")
        if due:
            try:
                if float(due) < now:
                    overdue_open.append(r)
            except (TypeError, ValueError):
                pass
    for r in closed:
        due = r.get("due_ts") or r.get("deadline_ts")
        closed_ts = r.get("closed_ts") or r.get("updated_ts")
        if due and closed_ts:
            try:
                if float(closed_ts) <= float(due):
                    ontime += 1
            except (TypeError, ValueError):
                pass

    # ② 验收留痕率：关闭时是否有验收批注/验收人
    accept_with_note = 0
    for r in closed:
        note = (r.get("accept_note") or r.get("verdict_note") or "")
        if str(note).strip():
            accept_with_note += 1

    # ③ 重复隐患：同地点同类型出现 >=2 次
    from collections import Counter
    key = Counter()
    for r in rows:
        loc = str(r.get("location") or r.get("dept") or "")
        cat = str(r.get("category") or r.get("title") or "")[:20]
        if loc or cat:
            key[(loc, cat)] += 1
    repeats = {f"{k[0]}|{k[1]}": v for k, v in key.items() if v >= 2}

    # ④ 重大隐患报备：severity 为 major/major_hazard 的，是否有报备字段
    major = [r for r in rows if str(r.get("severity") or "").lower() in ("major", "high", "重大")]
    major_reported = [r for r in major
                      if str(r.get("reported") or r.get("escalated") or "").lower() in ("1", "true")
                      or r.get("report_ref")]

    def rate(a, b):
        return round(a / b, 4) if b else None

    return {
        "total": total,
        "closed": len(closed),
        "open": len(open_),
        "closed_rate": rate(len(closed), total),
        "overdue_open": len(overdue_open),
        "ontime_close_rate": rate(ontime, len(closed)),
        "accept_note_rate": rate(accept_with_note, len(closed)),
        "repeat_pairs": len(repeats),
        "repeat_top": dict(sorted(repeats.items(), key=lambda x: -x[1])[:5]),
        "major_total": len(major),
        "major_reported_rate": rate(len(major_reported), len(major)),
        "indicators_without_sample": [
            k for k, v in {
                "ontime_close_rate": len(closed),
                "accept_note_rate": len(closed),
                "major_reported_rate": len(major),
            }.items() if v == 0
        ],
    }


def seed(db_path: str) -> dict:
    """造一批覆盖各场景的工单，用于验证审计逻辑（隔离库，不碰真实数据）。"""
    from core.audit_store import AuditStore
    st = AuditStore(db_path)
    now = time.time()
    day = 86400
    made = []

    def add(title, dept, severity, due_offset_days, close_offset_days=None, note=""):
        # 用真实签名；brief 必须是 dict（含 intent/urgency/entities/message_digest/
        # routing_reason/request_id），传字符串会 AttributeError——真实接口契约，不是本脚本的选择。
        due_ts = now + due_offset_days * day if due_offset_days is not None else None
        rid = f"seed_{int(time.time()*1000)}_{len(made)}"
        tid = st.create_ticket(
            title=title,
            type_="hazard",
            priority="high" if severity in ("major", "重大") else "normal",
            responsible_dept=dept,
            severity=severity,
            source_request_id=rid,
            user_id="seed_user",
            due_ts=due_ts,
            brief={"intent": "hazard_report", "urgency": "high",
                   "entities": {}, "message_digest": title[:80],
                   "routing_reason": "审计探针造样本", "request_id": rid},
            actor="audit_seed",
        )
        if close_offset_days is not None:
            try:
                st._con.execute(
                    "UPDATE tickets SET status='closed', stage='accept', closed_ts=? WHERE id=?",
                    (now + close_offset_days * day, tid))
                st._con.commit()
            except Exception as ex:
                print(f"  [warn] 关闭工单 {tid} 失败: {ex}")
        made.append(tid)
        return tid

    # 按期闭环（截止 +5 天，提前 2 天关闭）
    add("一号库房灭火器压力不足", "仓储部", "general", 5, 3, "已更换并复检")
    # 超期未闭环（截止 -3 天，仍未关）
    add("反应釜安全阀超期未校验", "反应车间", "major", -3, None)
    # 已关闭但无验收批注
    add("二号车间照明线管裸露", "电气班", "general", 7, 5, "")
    # 重复隐患：同地点同类第二次
    add("一号库房灭火器压力不足", "仓储部", "general", 5, 4, "第二次出现")
    # 无截止（正常）
    add("培训室灭火器检查卡缺失", "安环部", "general", 10, 8, "补齐检查卡")
    try:
        st._con.commit()
    except Exception:
        pass
    return {"created": len(made), "ids": made}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--seed", action="store_true", help="先造样本再审计")
    args = ap.parse_args()

    if args.seed:
        iso = Path("D:/claudecode/scratch/sm-loop/iso/ticket_audit")
        iso.mkdir(parents=True, exist_ok=True)
        db = str(iso / f"audit_{int(time.time())}.db")
        print(f"=== 造样本：{db} ===")
        s = seed(db)
        print(f"已建 {s['created']} 张工单：{s['ids']}\n")
    else:
        db = args.db or os.getenv("AUDIT_DB_PATH", str(REPO / "data" / "audit.db"))

    print(f"=== 审计 {db} ===")
    try:
        r = audit(db)
    except Exception as e:
        print(f"审计失败: {type(e).__name__}: {e}")
        return 1

    print(json.dumps(r, ensure_ascii=False, indent=2))

    print("\n=== 实效性判读 ===")
    if r.get("total", 0) == 0:
        print("库内无工单：四项指标无法计算（如实报告，不编造）")
    else:
        if r.get("overdue_open"):
            print(f"[!] 超期未闭环 {r['overdue_open']} 张 —— 真实管理场景下这是首要整改项")
        ocr = r.get("ontime_close_rate")
        if ocr is not None:
            print(f"按期闭环率 {ocr:.0%}" + ("（低于 80%，需关注）" if ocr < 0.8 else "（达标）"))
        anr = r.get("accept_note_rate")
        if anr is not None:
            print(f"验收留痕率 {anr:.0%}" + ("（有闭合无签字的工单，等于未闭环）" if anr < 1.0 else ""))
        if r.get("repeat_pairs"):
            print(f"[!] 重复隐患 {r['repeat_pairs']} 组 —— 同一地点同类问题反复，说明整改停留在治标")
        mr = r.get("major_reported_rate")
        if mr is not None:
            print(f"重大隐患报备率 {mr:.0%}" + ("（法定要求，未报备即风险）" if mr < 1.0 else ""))

    out = Path("D:/claudecode/scratch/sm-loop/runs/ticket_audit.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"db": db, "result": r}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
