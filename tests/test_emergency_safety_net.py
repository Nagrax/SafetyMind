# -*- coding: utf-8 -*-
# 紧急安全网测试（2026-10-28 并发轴实测发现）。
#
# ## 缺陷一：预检词表缺口（安全网漏检）
#
# "车间氨气泄漏已经有人晕倒，现在该怎么办？" 是教科书级严重紧急，但
# `emergency.precheck()` 返回 None：CRITICAL 词表只有"中毒/有人受伤/大量泄漏"，
# 而实际消息写的是"晕倒"；"泄漏"只出现在"泄漏+扩散"组合里。
# 结果：零 LLM 快速通道漏检，消息落入完整链路。
#
# ## 缺陷二：负载下紧急问句没有应急步骤（安全底线被击穿）
#
# 并发实测 5/32 紧急问答返回"工具调用未在预算内完成，未获得可用的最终回答。"
# 链路：预检漏检 → 完整链路 → 负载下意图识别退化（LLM 超时→BGE 兜底）→
# urgency 不再是 CRITICAL → CRITICAL 模板被跳过 → 工具回路预算耗尽 → 预算文案。
#
# ## 修法
#   1. emergency.py 补 `_CRITICAL_EXTRA`（晕倒/昏迷/窒息…）+ 泄漏×伤亡组合规则
#      （不动共享意图词表，避免影响意图基准）
#   2. 编排器加"紧急安全网"：用同一份本地预检兜底，命中即模板化，
#      绝不落到工具预算文案（SAFETYMIND_EMERGENCY_SAFETY_NET=0 可关）
#
# 用法: python tests/test_emergency_safety_net.py
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
    from core import emergency

    # ── 1. 词表缺口：严重紧急必须命中 ────────────────────────────────────
    must_hit = [
        "车间氨气泄漏已经有人晕倒，现在该怎么办？",
        "装置着火了，有人受伤，第一步干什么？",
        "管线刺漏有人中毒了",
        "现场有人昏迷不醒",
        "反应釜泄漏有人窒息",
    ]
    for m in must_hit:
        sig = emergency.precheck(m)
        record(f"严重紧急命中：{m[:16]}…", sig is not None,
               f"got={sig}")

    # ── 2. 误报防护不得弱化（否定/演练/撰写）────────────────────────────
    must_miss = [
        "没有泄漏，只是例行巡检",
        "演练时有人晕倒怎么办",
        "氨泄漏应急预案怎么编制",
        "讨论一下中毒的定义",
        "上次演练有人晕倒的记录怎么写",
    ]
    for m in must_miss:
        sig = emergency.precheck(m)
        record(f"误报抑制保持：{m[:16]}…", sig is None, f"got={sig and sig.get('reason')}")

    # ── 3. 组合规则标注可审计 ───────────────────────────────────────────
    sig = emergency.precheck("车间氨气泄漏已经有人晕倒，现在该怎么办？")
    record("组合命中带可审计原因",
           sig is not None and "组合规则" in sig.get("reason", "")
           and "泄漏" in sig.get("reason", ""),
           f"reason={sig and sig.get('reason')}")

    # ── 4. 编排器安全网存在且默认开启 ───────────────────────────────────
    src = Path(__file__).resolve().parents[1].joinpath(
        "agents/agent_orchestrator.py").read_text(encoding="utf-8")
    record("编排器含紧急安全网", "SAFETYMIND_EMERGENCY_SAFETY_NET" in src)
    record("安全网用同一份本地预检", "from core import emergency as _emg" in src
           and "_emg.precheck(req.message)" in src)
    record("安全网先于澄清追问",
           src.index("SAFETYMIND_EMERGENCY_SAFETY_NET")
           < src.index("def _needs_clarification") if "_needs_clarification" in src else True)

    # ── 5. 端到端：紧急消息在死 LLM 下仍拿到应急步骤（模板零依赖）────────
    #    用编排器直接跑，避免起整个 FastAPI（快且只测该层）
    import asyncio
    from agents.agent_orchestrator import AgentOrchestrator as Orchestrator, Request
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    async def run_one(msg, urgency):
        orch = Orchestrator(api_key="dummy", base_url="http://127.0.0.1:9",
                            model="dummy")
        req = Request(request_id="test-sn", message=msg, user_id="u", conv_id="c",
                      context="", memory_context=None, knowledge="", history=[],
                      entities={}, intent=IntentCategory.EMERGENCY_RESPONSE,
                      intent_group="emergency", urgency=urgency,
                      intent_confidence=0.5)
        return await orch.run(req)

    # urgency 非 CRITICAL（模拟负载下意图退化）但消息是紧急 → 安全网必须兜住
    res = asyncio.run(run_one("车间氨气泄漏已经有人晕倒，现在该怎么办？",
                              UrgencyLevel.LOW))
    resp = res.response or ""
    record("意图退化（urgency=LOW）时安全网仍给应急步骤",
           any(k in resp for k in ("立即", "撤离", "报警", "疏散", "应急")),
           f"resp={resp[:80]}")
    record("安全网响应标注可审计原因",
           "紧急安全网" in (res.routing_reason or "") or "模板化" in (res.routing_reason or ""),
           f"reason={res.routing_reason}")

    # ── 6. 非紧急消息不得被安全网误伤 ───────────────────────────────────
    res2 = asyncio.run(run_one("动火作业票的有效期是怎么规定的？", UrgencyLevel.LOW))
    record("非紧急消息不被安全网误伤",
           "紧急安全网" not in (res2.routing_reason or ""),
           f"reason={res2.routing_reason}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
