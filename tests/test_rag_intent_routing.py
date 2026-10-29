# -*- coding: utf-8 -*-
# 检索按意图分流测试（2026-10-26 闭环第 7 轮）。
#
# 修复依据（实测，见 docs/PERF_AND_RAG_DECISIONS.md）：
#   RAG 平均快 36%（模型读资料比回忆更快），唯一变慢的是**创作类任务**：
#   "帮我写一条隐患排查记录" A 9484ms → B 16266ms，慢 6.8s。
#   原因：创作不需要检索，注入资料反而增加上下文负担。
#
# 本测试锁定分流规则：
#   1. 明确"写/生成/起草"某文档 → 跳过检索
#   2. 只要"模板/清单/表格/样例" → 仍检索（模板本身在库里）
#   3. 同时含写与模板 → 保守选择检索（宁可多检索也不让模型缺资料）
#   4. 其它业务意图行为不变（回归保护）
#   5. 开关 SAFETYMIND_SKIP_RAG_FOR_WRITING=0 可回退
#
# 用法: python tests/test_rag_intent_routing.py
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
    from core.intent_recognizer import IntentCategory as IC
    # 延迟导入：api.main 会触发大量初始化，但 _should_use_knowledge 是纯函数
    import importlib
    api = importlib.import_module("api.main")
    should = api._should_use_knowledge

    # ── 1. 创作类跳过检索 ────────────────────────────────────────────────
    writing = [
        "帮我写一条隐患排查记录，内容是一号库房灭火器压力表在红区",
        "生成一份动火作业安全交底",
        "起草一个受限空间作业方案",
        "帮我做一份隐患整改报告",
        "编制年度安全培训计划",
    ]
    for m in writing:
        record(f"创作类跳过检索：{m[:16]}…",
               should(m, IC.SAFETY_DOCUMENT) is False,
               f"got={should(m, IC.SAFETY_DOCUMENT)}")

    # ── 2. 要模板仍检索（模板在库里）──────────────────────────────────────
    templates = [
        "给我一份隐患排查记录表模板",
        "要一份动火作业票的样例",
        "安全检查表清单有哪些",
        "有没有台账表格",
    ]
    for m in templates:
        record(f"要模板仍检索：{m[:16]}…",
               should(m, IC.SAFETY_DOCUMENT) is True,
               f"got={should(m, IC.SAFETY_DOCUMENT)}")

    # ── 3. 写+模板同时出现 → 保守检索 ────────────────────────────────────
    record("写+模板同时出现时保守选择检索",
           should("帮我写一份隐患排查记录模板", IC.SAFETY_DOCUMENT) is True,
           f"got={should('帮我写一份隐患排查记录模板', IC.SAFETY_DOCUMENT)}")

    # ── 4. 其它业务意图行为不变（回归保护）────────────────────────────────
    keep = [
        ("动火作业票有效期怎么规定的", IC.WORK_PERMIT),
        ("反应釜超温报警怎么处理", IC.EQUIPMENT_ALARM),
        ("甲醇的闪点是多少", IC.CHEMICAL_SAFETY),
        ("安全生产法主要负责人职责", IC.REGULATION_QUERY),
        ("车间噪声88分贝怎么防护", IC.OCC_HEALTH),
    ]
    for m, it in keep:
        record(f"业务意图仍检索：{m[:16]}…", should(m, it) is True,
               f"got={should(m, it)}")

    # ── 5. 寒暄/反馈等仍不检索 ──────────────────────────────────────────
    for it in (IC.GREETING, IC.FEEDBACK, IC.ESCALATION, IC.OTHER):
        record(f"{it.value} 仍不检索", should("你好", it) is False,
               f"got={should('你好', it)}")

    # ── 6. 开关可回退 ───────────────────────────────────────────────────
    old = os.environ.get("SAFETYMIND_SKIP_RAG_FOR_WRITING")
    try:
        os.environ["SAFETYMIND_SKIP_RAG_FOR_WRITING"] = "0"
        m = "帮我写一条隐患排查记录"
        record("开关=0 时创作类仍检索（回退旧行为）",
               should(m, IC.SAFETY_DOCUMENT) is True,
               f"got={should(m, IC.SAFETY_DOCUMENT)}")
    finally:
        if old is None:
            os.environ.pop("SAFETYMIND_SKIP_RAG_FOR_WRITING", None)
        else:
            os.environ["SAFETYMIND_SKIP_RAG_FOR_WRITING"] = old

    # ── 7. 空消息边界 ───────────────────────────────────────────────────
    record("空消息不检索", should("", IC.SAFETY_DOCUMENT) is False)
    record("纯空白不检索", should("   ", IC.SAFETY_DOCUMENT) is False)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
