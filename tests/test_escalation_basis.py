# -*- coding: utf-8 -*-
# 升级依据（escalation_basis）回归测试（2026-10-29 闭环第 47 轮）。
#
# ## 缺陷（真实对照实验发现）
#
# `/chat` 与 `/chat/stream` 同题对照（benchmarks/eval_stream_parity.py，12 题）：
# **3 题**的 `escalated` 不一致。根因不是链路分叉，而是 `escalated` 把"模型正文里出现
# 『转人工/建议上报』等关键词"也算作升级——生成文本是随机的，同一问题两次可能不同。
# 于是"要不要升级"这件事不稳定，测试也无法断言。
#
# ## 修法
#
# 新增 `escalation_basis`（additive）：urgency / intent / model_hint / safety_net / ""。
# `escalated` 语义不变（三者任一，保持安全性），但下游与测试可以只看确定性部分。
#
# ## 本测试锁定
#
# 1. 分类优先级：urgency > intent > model_hint > ""
# 2. 确定性依据（urgency/intent）不会因 model_hint 变化而改变
# 3. OrchestratorResult 带该字段且默认空串（additive，不破坏旧结构）
# 4. 四个升级出口都写了 basis（源码级防漏）
# 5. ChatResponse 暴露 escalation_basis
#
# 用法: python tests/test_escalation_basis.py
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
    from agents.agent_orchestrator import (IntentCategory, OrchestratorResult, UrgencyLevel,
                                           escalation_basis)

    U, I = UrgencyLevel, IntentCategory
    record("CRITICAL 紧急度 → urgency",
           escalation_basis(U.CRITICAL, I.OTHER, False) == "urgency")
    record("ESCALATION 意图 → intent",
           escalation_basis(U.LOW, I.ESCALATION, False) == "intent")
    record("只有模型关键词 → model_hint",
           escalation_basis(U.LOW, I.OTHER, True) == "model_hint")
    record("都不满足 → 空串",
           escalation_basis(U.LOW, I.OTHER, False) == "")
    record("优先级 urgency > intent > hint",
           escalation_basis(U.CRITICAL, I.ESCALATION, True) == "urgency"
           and escalation_basis(U.LOW, I.ESCALATION, True) == "intent")
    record("确定性依据不随模型提示变化",
           escalation_basis(U.CRITICAL, I.OTHER, True) == escalation_basis(U.CRITICAL, I.OTHER, False)
           and escalation_basis(U.LOW, I.ESCALATION, True)
           == escalation_basis(U.LOW, I.ESCALATION, False))

    r = OrchestratorResult(request_id="x", response="y", agent_type=None,
                           intent=None)
    record("OrchestratorResult 默认带空 basis（additive 不破坏旧结构）",
           getattr(r, "escalation_basis", None) == "", f"got={getattr(r, 'escalation_basis', 'MISSING')}")

    src = (REPO / "agents" / "agent_orchestrator.py").read_text(encoding="utf-8")
    record("CRITICAL 模板出口写 basis=urgency", 'escalation_basis="urgency"' in src)
    record("紧急安全网出口写 basis=safety_net", 'escalation_basis="safety_net"' in src)
    record("主链路与并行链路都用 escalation_basis() 计算",
           src.count("_basis = escalation_basis(") >= 2,
           f"count={src.count('_basis = escalation_basis(')}")

    api = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    record("ChatResponse 暴露 escalation_basis", "escalation_basis: str" in api)
    record("构造响应时透传 basis", 'escalation_basis=getattr(result, "escalation_basis", "")' in api)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
