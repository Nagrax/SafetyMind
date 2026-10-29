# -*- coding: utf-8 -*-
# 作答风格旋钮回归测试（2026-10-29 闭环第 37 轮）。
#
# 背景：延迟分账显示非生成开销仅 8%，瓶颈在生成侧；输出长度与耗时近似线性，
# 所以"答案更短"是唯一不换模型就能降延迟的杠杆——但它必须由质量 A/B 定价。
# 本测试只锁定旋钮本身：默认**绝不改变**产品行为，只有显式设置才追加约束。
#
# 用法: python tests/test_answer_style.py
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
    from core.llm_params import answer_style_suffix
    from agents.agent_orchestrator import SafetyCoordinationAgent

    saved = os.environ.get("SAFETYMIND_ANSWER_STYLE")

    def setenv(v):
        if v is None:
            os.environ.pop("SAFETYMIND_ANSWER_STYLE", None)
        else:
            os.environ["SAFETYMIND_ANSWER_STYLE"] = str(v)

    try:
        # ── 1. 默认不改变任何东西 ────────────────────────────────────────
        setenv(None)
        record("默认后缀为空串", answer_style_suffix() == "")
        agent = SafetyCoordinationAgent(None, "dummy")
        record("默认 system prompt 与基础提示词逐字一致",
               agent._build_system_prompt("动火票有效期") == agent.system_prompt)

        # ── 2. brief 生效且只追加、不改原文 ──────────────────────────────
        setenv("brief")
        suffix = answer_style_suffix()
        record("brief 返回非空约束", bool(suffix) and "作答风格" in suffix)
        p = agent._build_system_prompt("动火票有效期")
        record("brief 时原提示词仍是前缀（不覆盖角色设定）", p.startswith(agent.system_prompt))
        record("brief 时追加了限长与结构要求",
               "450" in p and "先给结论" in p and "最多 5 条" in p)

        # ── 3. 非法值回退为空（不误伤）──────────────────────────────────
        for bad in ("", "default", "verbose", "1"):
            setenv(bad)
            record(f"取值 {bad!r} 不加任何约束", answer_style_suffix() == "",
                   f"got={answer_style_suffix()[:20]!r}")
    finally:
        setenv(saved)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
