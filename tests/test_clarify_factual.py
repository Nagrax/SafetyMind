# -*- coding: utf-8 -*-
# 事实型问题跳过澄清的回归测试（2026-10-29 闭环第 53 轮）。
#
# ## 背景
#
# 事实稳定性评测里，"噪声作业的职业接触限值是多少？依据哪个标准？" 5 次全部只拿到
# A/B 澄清提示、一次数值都没给——用户要数字，反问让他多点一次。
# 控温 A/B（6 题×3 次）：澄清率 17%→0%、事实覆盖 0.56→0.70、配对 accuracy +0.133。
#
# ## 本测试锁定
#
# 1. `is_factual_question` 对"多少/依据/标准/是什么/怎么规定"等问法判 True
# 2. 对寒暄、操作请求（"帮我登记隐患"）、开放咨询判 False（不误伤澄清机制）
# 3. 默认开启跳过、可用 SAFETYMIND_CLARIFY_SKIP_FACTUAL=0 关闭
# 4. 澄清分支确实读了这个开关（源码级防漏）
#
# 用法: python tests/test_clarify_factual.py
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    from core.intent_recognizer import is_factual_question

    facts = [
        "噪声作业的职业接触限值是多少？依据哪个标准？",
        "动火作业票分几个等级？各级有效期多久？",
        "受限空间作业的气体检测合格判定值分别是多少？",
        "安全阀的校验周期是怎么规定的？",
        "高处作业分几级？",
        "甲醇的闪点是什么？",
    ]
    for q in facts:
        record(f"事实型问题判 True：{q[:14]}…", is_factual_question(q) is True)

    non_facts = [
        "你好",
        "帮我登记一条隐患排查记录，位置一号库房",
        "我想了解安全生产管理应该怎么做",   # 开放咨询，不含数值/条款问法
        "刚才那个回答再详细说说",
    ]
    for q in non_facts:
        record(f"非事实型不误伤：{q[:14]}…", is_factual_question(q) is False)

    record("空串不判事实型", is_factual_question("") is False)

    # 源码接线：默认开启 + 可关
    src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    record("默认开启跳过（SAFETYMIND_CLARIFY_SKIP_FACTUAL 默认 1）",
           'os.getenv("SAFETYMIND_CLARIFY_SKIP_FACTUAL", "1")' in src)
    record("澄清分支确实消费该开关",
           "and not (_skip_factual and _is_factual(req.message))" in src)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
