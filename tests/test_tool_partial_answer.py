# -*- coding: utf-8 -*-
# 工具预算耗尽时的"部分证据交付"测试（2026-10-28 负载实测发现）。
#
# ## 缺陷
#
# live 链路在 CPU 被外部任务抢占时实测：normal 用例 44.2s 后返回
# "工具调用未在预算内完成，未获得可用的最终回答。"——用户等了 44 秒，**什么都没有**。
# 而实际上工具（knowledge_search / resource_search）已经返回了真实资料。
# 根因：模型"慢"（不是"失败"）时工具轮次预算被烧光，last_text 为空，
# `_ensure_text` 直接回固定文案，把已经取到的证据全丢了。
#
# ## 修法
#
# 预算耗尽且无终答时，改由 `_partial_evidence_answer` 交出**未整理但有用**的证据：
#   1. 如实标注"未能完成最终整理"（不假装成稿）
#   2. 只摘录成功结果（is_error 的降级说明不是证据，不得混入）
#   3. 长度封顶
# 无证据时仍回原固定文案（如实说没有），不造假。
#
# 用法: python tests/test_tool_partial_answer.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    from core.tool_execution import ToolExecutor

    mk = ToolExecutor._partial_evidence_answer

    # ── 1. 有成功证据 → 交出资料并如实标注未整理 ─────────────────────────
    good = [{"type": "tool_result", "tool_use_id": "t1", "is_error": False,
             "content": "动火作业票有效期：特殊动火不超过 8 小时"}]
    out = mk(good)
    record("有证据时交付原始资料", "特殊动火不超过 8 小时" in out, f"out={out[:80]}")
    record("如实标注未完成最终整理", "未能完成最终整理" in out)
    record("声明未经改写（不冒充成稿）", "未经过改写" in out)

    # ── 2. 只有错误结果 → 不得把降级说明当证据 ──────────────────────────
    only_err = [{"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                 "content": "检索服务不可用"}]
    record("仅错误结果时不出资料（不造假证据）", mk(only_err) == "",
           f"out={mk(only_err)!r}")

    # ── 3. 无工具结果 → 回落固定文案 ────────────────────────────────────
    record("无结果返回空串（交回调用方兜底）", mk([]) == "")
    record("固定文案仍在（无证据时如实说没有）",
           ToolExecutor._ensure_text("") == "工具调用未在预算内完成，未获得可用的最终回答。")

    # ── 4. 混合：只取成功项 ─────────────────────────────────────────────
    mixed = [
        {"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": "ERR"},
        {"type": "tool_result", "tool_use_id": "b", "is_error": False, "content": "真实依据A"},
    ]
    om = mk(mixed)
    record("混合结果只摘录成功项", "真实依据A" in om and "ERR" not in om,
           f"out={om[:80]}")

    # ── 5. 内容块为数组形态也能处理（Anthropic 块数组）──────────────────
    arr = [{"type": "tool_result", "tool_use_id": "c", "is_error": False,
            "content": [{"type": "text", "text": "数组形态证据"}]}]
    record("块数组形态可解析", "数组形态证据" in mk(arr))

    # ── 6. 长度封顶（不把原始 JSON 灌给用户）────────────────────────────
    long_ev = [{"type": "tool_result", "tool_use_id": "d", "is_error": False,
                "content": "证据" * 2000}]
    ol = mk(long_ev)
    record("输出长度封顶（<=1800 字）", len(ol) <= 1800, f"len={len(ol)}")

    # ── 7. 契约未破坏：terminated 语义与 degraded 标记保留 ──────────────
    src = Path(__file__).resolve().parents[1].joinpath(
        "core/tool_execution.py").read_text(encoding="utf-8")
    record("budget_rounds/calls 语义与 degraded 保留",
           'terminated="budget_calls", degraded=True' in src
           and 'terminated="budget_rounds", degraded=True' in src)
    record("仅在无终答时才用部分证据（有终答优先用终答）",
           "self._ensure_text(last_text) if last_text" in src)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
