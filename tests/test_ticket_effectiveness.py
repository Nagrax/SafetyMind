# -*- coding: utf-8 -*-
# 工单闭环实效性审计测试（2026-10-26 闭环第 6 轮）。
#
# 为什么需要：一个工单系统"能建单"不等于"能闭环"。真实安环部考核的是
#   ① 隐患是否在期限内整改（超期=失职）② 是否有验收人签字（无验收=未闭环）
#   ③ 是否重复隐患（治标不治本）④ 重大隐患是否报备（法定要求）
# 本测试用**构造好的样本**验证这四项审计逻辑各自算得对，而不是只看"能跑"。
#
# 用法: python tests/test_ticket_effectiveness.py
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []
ISO = Path("D:/claudecode/scratch/sm-loop/iso/ticket_audit_test")


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def build_db(path: Path) -> list:
    """构造 5 张覆盖各场景的工单（与审计脚本的 seed 同构）。"""
    from core.audit_store import AuditStore
    st = AuditStore(str(path))
    now = time.time()
    day = 86400
    ids = []

    def add(title, dept, severity, due_off, close_off):
        rid = f"t_{int(time.time() * 1000)}_{len(ids)}"
        tid = st.create_ticket(
            title=title, type_="hazard",
            priority="high" if severity == "major" else "normal",
            responsible_dept=dept, severity=severity,
            source_request_id=rid, user_id="u_test",
            due_ts=(now + due_off * day) if due_off is not None else None,
            brief={"intent": "hazard_report", "urgency": "high", "entities": {},
                   "message_digest": title[:80], "routing_reason": "测试", "request_id": rid},
            actor="test",
        )
        if close_off is not None:
            st._con.execute(
                "UPDATE tickets SET status='closed', stage='accept', closed_ts=? WHERE id=?",
                (now + close_off * day, tid))
            st._con.commit()
        ids.append(tid)
        return tid

    add("一号库房灭火器压力不足", "仓储部", "general", 5, 3)      # 按期闭环
    add("反应釜安全阀超期未校验", "反应车间", "major", -3, None)   # 超期未闭环
    add("二号车间照明线管裸露", "电气班", "general", 7, 5)         # 闭环但无验收批注
    add("一号库房灭火器压力不足", "仓储部", "general", 5, 4)       # 重复隐患
    add("培训室灭火器检查卡缺失", "安环部", "general", 10, 8)      # 正常闭环
    return ids


def main():
    from benchmarks.audit_ticket_effectiveness import audit

    if ISO.exists():
        shutil.rmtree(str(ISO), ignore_errors=True)
    ISO.mkdir(parents=True, exist_ok=True)
    db = ISO / f"t_{int(time.time())}.db"

    try:
        ids = build_db(db)
        record("样本构造成功（5 张工单）", len(ids) == 5, f"ids={ids}")
    except Exception as e:
        record("样本构造", False, f"{type(e).__name__}: {e}")
        print(f"\n{sum(1 for x in RESULTS if not x)}/{len(RESULTS)} FAILED")
        sys.exit(1)

    try:
        r = audit(str(db))
    except Exception as e:
        record("审计执行", False, f"{type(e).__name__}: {e}")
        print(f"\n{sum(1 for x in RESULTS if not x)}/{len(RESULTS)} FAILED")
        sys.exit(1)

    # ── ① 基本计数 ─────────────────────────────────────────────────────
    record("总单数 = 5", r["total"] == 5, f"total={r['total']}")
    record("已闭环 4 / 未闭环 1", r["closed"] == 4 and r["open"] == 1,
           f"closed={r['closed']} open={r['open']}")
    record("闭环率 = 0.8", r["closed_rate"] == 0.8, f"rate={r['closed_rate']}")

    # ── ② 超期未闭环识别（真实管理场景的首要整改项）────────────────────
    record("识别出 1 张超期未闭环工单", r["overdue_open"] == 1,
           f"overdue_open={r['overdue_open']}")

    # ── ③ 按期闭环率 ───────────────────────────────────────────────────
    record("按期闭环率 = 100%（4 张均在截止前关闭）", r["ontime_close_rate"] == 1.0,
           f"ontime={r['ontime_close_rate']}")

    # ── ④ 验收留痕率（无签字 = 未真正闭环）────────────────────────────
    record("验收留痕率 = 0%（样本工单都没写验收批注）", r["accept_note_rate"] == 0.0,
           f"accept_note_rate={r['accept_note_rate']}")

    # ── ⑤ 重复隐患识别 ─────────────────────────────────────────────────
    record("识别出 1 组重复隐患（同地点同类 2 次）", r["repeat_pairs"] == 1,
           f"repeat_pairs={r['repeat_pairs']} top={r.get('repeat_top')}")

    # ── ⑥ 重大隐患报备率 ───────────────────────────────────────────────
    record("重大隐患计数 = 1", r["major_total"] == 1, f"major={r['major_total']}")
    record("重大隐患报备率 = 0%（样本未报备，如实报告）",
           r["major_reported_rate"] == 0.0, f"rate={r['major_reported_rate']}")

    # ── ⑦ 空库时如实报告"样本不足"，不编造数字 ─────────────────────────
    empty_db = ISO / f"empty_{int(time.time())}.db"
    try:
        re_ = audit(str(empty_db))
        record("空库时 total=0 并标注样本不足", re_.get("total") == 0 and "note" in re_,
               f"r={re_}")
    except Exception as e:
        record("空库审计不崩", False, f"{type(e).__name__}: {e}")

    # ── ⑧ 结果可落盘为 JSON（供闭环消费）──────────────────────────────
    try:
        p = ISO / "audit.json"
        p.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        reload = json.loads(p.read_text(encoding="utf-8"))
        record("审计结果可序列化为 JSON 并被读回", reload["total"] == 5)
    except Exception as e:
        record("结果序列化", False, f"{type(e).__name__}: {e}")

    shutil.rmtree(str(ISO), ignore_errors=True)
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
