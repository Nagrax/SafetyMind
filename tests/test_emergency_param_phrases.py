# -*- coding: utf-8 -*-
# 紧急预检"参数名误判"回归测试（2026-10-29 闭环第 49 轮）。
#
# ## 缺陷（真实评测发现）
#
# 事实稳定性评测里，"甲醇的闪点和爆炸极限分别是多少？" 5 次全部**没有给出任何数值**。
# 查真实答案发现它被当成紧急事态：
#   【紧急事态·立即行动】您的上报已触发应急响应…【已建单】应急工单 #83
# 根因：CRITICAL 词表含"爆炸"，而问题里的"爆炸极限"是**物性参数名**。
# 后果有两层：①用户拿不到闪点/爆炸极限数值；②凭空建了一张应急工单污染值班队列。
#
# ## 修法
#
# 关键词的**全部出现**都只是参数名的一部分（爆炸极限/爆炸下限/爆炸上限/爆炸浓度/
# 爆炸范围/爆炸性…）时，不计为现场证据；只要有一处不是（"正在爆炸，有人受伤"），
# 仍按紧急处理。
#
# 用法: python tests/test_emergency_param_phrases.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    from core.emergency import precheck

    # ── 1. 参数名不再被判为紧急 ────────────────────────────────────────
    params = [
        "甲醇的闪点和爆炸极限分别是多少？",
        "甲烷的爆炸下限是多少？",
        "氢气爆炸上限怎么查？",
        "这个物质的爆炸范围是多少",
        "爆炸极限检测仪怎么校准",
        "汽油的爆炸性参数有哪些",
    ]
    for q in params:
        got = precheck(q)
        record(f"参数名不触发紧急分流：{q[:16]}…", got is None, f"got={got}")

    # ── 2. 真的现场事件仍必须触发 ──────────────────────────────────────
    live = [
        "车间发生爆炸了，有人受伤",
        "储罐正在爆炸",
        "现场已经爆炸，两名工人被困",
        "反应釜泄漏，已经有人中毒晕倒",
    ]
    for q in live:
        got = precheck(q)
        record(f"现场事件仍触发紧急分流：{q[:16]}…", got is not None, f"got={got}")

    # ── 3. 参数名与现场事件同现 → 仍按紧急处理 ────────────────────────
    mixed = "爆炸极限是多少？现场已经爆炸了，有人受伤"
    got = precheck(mixed)
    record("参数名 + 现场事件同现时仍触发", got is not None, f"got={got}")

    # ── 4. 既有防护不被破坏 ───────────────────────────────────────────
    record("否定语境仍被抑制（没有泄漏）", precheck("现场没有泄漏") is None)
    record("演练语境仍被抑制（正在演练泄漏处置）",
           precheck("正在开展泄漏应急演练") is None)
    record("讨论词仍被抑制（泄漏这两个字怎么写）",
           precheck("泄漏这两个字怎么写") is None)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
