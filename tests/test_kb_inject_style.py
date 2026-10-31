# -*- coding: utf-8 -*-
# 知识库注入口径（条款依据 vs 检查要点）回归测试（2026-10-30 闭环第 58 轮）。
#
# ## 背景
#
# 1698 条隐患**检查项**灌库后：召回 oracle@2 0.25→0.75、无解题 66.7%→0%，
# 但答案质量反而下降（accuracy −0.278、safety −0.528）。机制假设是这些检查项被当成
# "法规条款依据"注入 → 答案变成片段拼接、更爱凑依据、更少"需核实"边界。
# 本开关用于验证"换注入口径能否把召回收益拿回来"。
#
# ## 本测试锁定
#
# 1. 默认口径不变（"[知识库检索结果]" + 要求标注【依据N】）
# 2. checklist 口径：表头改"[检查要点参考]"，**禁止**标注【依据N】，要求说明需核实
# 3. 非法/空取值回落默认
# 4. 注入函数被 `_build_knowledge_context` 真正调用（源码级防漏）
#
# 用法: python tests/test_kb_inject_style.py
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
    import api.main as m

    saved = os.environ.get("SAFETYMIND_KB_INJECT_STYLE")

    def setenv(v):
        if v is None:
            os.environ.pop("SAFETYMIND_KB_INJECT_STYLE", None)
        else:
            os.environ["SAFETYMIND_KB_INJECT_STYLE"] = v

    try:
        setenv(None)
        record("默认表头为知识库检索结果", m._kb_inject_header() == "[知识库检索结果]")
        record("默认要求标注【依据N】", "【依据N】" in m._kb_inject_instruction())
        record("默认允许把命中当依据（不含'不得当作法规条款'）",
               "不得当作法规条款" not in m._kb_inject_instruction())

        setenv("checklist")
        record("checklist 表头为检查要点参考",
               m._kb_inject_header() == "[检查要点参考]",
               m._kb_inject_header())
        ins = m._kb_inject_instruction()
        record("checklist 禁止标注【依据N】", "不得标注为【依据N】" in ins, ins[:40])
        record("checklist 要求说明需核实", "需核实" in ins)
        record("checklist 明确'不得当作法规条款'", "不得当作法规条款" in ins)

        record("取值大小写不敏感（CHECKLIST 视为 checklist）",
               (setenv("CHECKLIST"), m._kb_inject_header() == "[检查要点参考]")[1])
        for bad in ("", "  ", "foo", "verbose"):
            setenv(bad)
            record(f"取值 {bad!r} 回落默认口径",
                   m._kb_inject_header() == "[知识库检索结果]")
    finally:
        setenv(saved)

    src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    record("_build_knowledge_context 使用 _kb_inject_header()",
           "parts.append(_kb_inject_header())" in src)
    record("_build_knowledge_context 使用 _kb_inject_instruction()",
           "parts.append(_kb_inject_instruction())" in src)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
