# -*- coding: utf-8 -*-
# 意图模型与生成模型分离的回归测试（2026-10-29 闭环第 40 轮）。
#
# 依据（真实评测，test2 n=150、BGE+低置信回落同口径）：
#   raccoon/deepseek-v4-1-flash ：准确率 0.980，低置信回落 14652ms
#   aggregate/qwen3-8-flash     ：准确率 0.980，低置信回落  2347ms（−84%）
# 低置信回落覆盖约 42.7% 请求 → 这是应用层能拿到的最大延迟收益。
# 而"生成"仍用强模型（换生成模型会掉质量：accuracy −0.375 / safety −0.625，见
# benchmarks/eval_model_swap_ab.py）。
#
# 本测试锁定：
#   1. 未设置 SAFETYMIND_INTENT_MODEL 时沿用生成模型（行为不变）
#   2. 设置后意图模型切换、生成模型不受影响
#   3. 空串/空白按未设置处理（不把空模型名传给客户端）
#   4. 启动代码确实用该函数选择意图模型（防止回退成硬编码）
#
# 用法: python tests/test_intent_model_split.py
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    import api.main as m

    saved = os.environ.get("SAFETYMIND_INTENT_MODEL")

    def setenv(v):
        if v is None:
            os.environ.pop("SAFETYMIND_INTENT_MODEL", None)
        else:
            os.environ["SAFETYMIND_INTENT_MODEL"] = str(v)

    try:
        GEN = "raccoon/sn-deepseek-v4-1-flash-1017"
        FAST = "aggregate/qwen3-8-flash"

        setenv(None)
        record("未设置时沿用生成模型", m._intent_model_name(GEN) == GEN,
               f"got={m._intent_model_name(GEN)}")

        setenv(FAST)
        record("设置后意图模型切换", m._intent_model_name(GEN) == FAST,
               f"got={m._intent_model_name(GEN)}")
        record("生成模型不受影响（函数只决定意图模型）", GEN == GEN)

        for blank in ("", "   "):
            setenv(blank)
            record(f"空值 {blank!r} 按未设置处理", m._intent_model_name(GEN) == GEN,
                   f"got={m._intent_model_name(GEN)}")

        # ── 启动代码确实使用该函数（防回退成硬编码 cfg["model"]）──────────
        src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
        record("启动代码用 _intent_model_name 选择意图模型",
               "_intent_model = _intent_model_name(cfg[\"model\"])" in src)
        record("意图识别器用分离后的模型构造",
               "IntentRecognizer(" in src and "model=_intent_model" in src)
        record("意图模型与生成模型不同时会记日志（可观测）",
               "意图模型与生成模型分离" in src)

        # ── 真正的 /chat 路径也要用同一选择函数 ─────────────────────────
        # （2026-10-29 踩坑：只改 api/main.py 的实例时，实测意图仍用生成模型——
        #   跑在 orchestrator 里的那个实例才是 /chat 真正用的）
        orch_src = (REPO / "agents" / "agent_orchestrator.py").read_text(encoding="utf-8")
        record("orchestrator 的意图识别器也用 intent_model_name",
               "intent_model_name(model)" in orch_src,
               "orchestrator 未使用共享选择函数 → /chat 实际仍走生成模型")
        core = (REPO / "core" / "llm_params.py").read_text(encoding="utf-8")
        record("选择逻辑集中在 core/llm_params（单一实现）",
               "def intent_model_name" in core)
    finally:
        setenv(saved)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
