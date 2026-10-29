# -*- coding: utf-8 -*-
# 专家上下文隔离回归测试（2026-10-26 闭环第 4 轮）。
#
# 修复的缺陷：run_parallel 把 assemble_task 的**同一份** messages 传给全部专家，
# 所谓"隔离上下文"并不存在，实际是「同上下文 + 不同 system_prompt」。
#
# 本测试锁定三条契约：
#   1. 不同领域的专家拿到**不同**背景材料（真隔离）
#   2. 设备专家保留设备证据、合规专家保留法规证据（切得对，不是随机切）
#   3. 用户原始问题在所有专家那里**完整保留**（切背景绝不能切掉题意）
#   4. SAFETYMIND_EXPERT_ISOLATION=0 可回退旧行为（同上下文），保证向后兼容
#   5. 段落太少/全不相关时退回完整背景（宁可多给，不能让专家看不到材料）
#
# 用法: python tests/test_expert_context_isolation.py
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


MIXED_BG = (
    "设备报警记录：反应釜 3 号超温跳车，联锁动作正常；泵 P-102 振动 11.2mm/s 超标。\n\n"
    "法规依据：GB 30871 规定动火作业须办理动火证，一级动火由安环部审批，作业票有效期 8 小时。\n\n"
    "设备台账：换热器 E-201 上次检修 2026-03，压缩机 C-301 轴承温度偏高。\n\n"
    "作业票记录：DH-2026-0087 动火票已审批，受限空间票 SX-2026-0031 待办。\n\n"
    "培训记录：2026-10-15 动火作业专项培训 18 人合格。\n\n"
    "个人防护：安全帽 200 顶库存，防毒面具 40 套。"
)


def make_req(Request, IntentCategory, UrgencyLevel, **kw):
    base = dict(message="反应釜超温跳车需要动火检修，动火票怎么办？", user_id="u", conv_id="c",
                intent=IntentCategory.EQUIPMENT_ALARM, intent_confidence=0.9,
                urgency=UrgencyLevel.MEDIUM, entities={}, context=MIXED_BG, knowledge="")
    base.update(kw)
    return Request(**base)


def main():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType
    from agents.agent_orchestrator import Request as Req
    from core.intent_recognizer import IntentCategory, UrgencyLevel

    orch = AgentOrchestrator(api_key="dummy", base_url="http://127.0.0.1:9", model="dummy")
    emp = orch._employee

    def bg_of(task):
        for m in task.messages:
            if isinstance(m.get("content"), str) and m["content"].startswith("[背景信息]"):
                return m["content"]
        return ""

    req = make_req(Req, IntentCategory, UrgencyLevel)
    t_eq = emp.assemble_task_for(req, AgentType.EQUIPMENT)
    t_cp = emp.assemble_task_for(req, AgentType.COMPLIANCE)
    eq, cp = bg_of(t_eq), bg_of(t_cp)

    # ── 1. 真隔离：两专家背景不同 ───────────────────────────────────────
    record("设备专家与合规专家拿到不同背景材料（真隔离）", eq != cp and eq and cp,
           f"eq_len={len(eq)} cp_len={len(cp)} same={eq == cp}")

    # ── 2. 切得对：各自保留本领域证据 ───────────────────────────────────
    record("设备专家背景含设备证据（反应釜/振动/联锁）",
           any(k in eq for k in ("反应釜", "振动", "联锁")), f"eq={eq[:120]}")
    record("合规专家背景含法规证据（GB 30871/动火证/作业票）",
           any(k in cp for k in ("GB 30871", "动火证", "作业票")), f"cp={cp[:120]}")

    # 设备专家不该主要拿到法规段落（允许少量交叉，但不应完整保留全部法律条文段）
    record("设备专家背景不是合规背景的复制", eq != cp)

    # ── 3. 用户问题在所有专家处完整保留 ─────────────────────────────────
    record("用户原始问题在设备专家消息末尾完整保留",
           t_eq.messages[-1]["content"] == req.message, f"last={t_eq.messages[-1]['content'][:50]}")
    record("用户原始问题在合规专家消息末尾完整保留",
           t_cp.messages[-1]["content"] == req.message)
    record("两专家的问题消息完全一致（题意不得因切分而漂移）",
           t_eq.messages[-1] == t_cp.messages[-1])

    # ── 4. 结构化实体也完整保留 ─────────────────────────────────────────
    req2 = make_req(Req, IntentCategory, UrgencyLevel,
                    entities={"equipment_id": ["R-301"], "chemical": ["甲醇"]})
    t2 = emp.assemble_task_for(req2, AgentType.COMPLIANCE)
    ent_msgs = [m for m in t2.messages if isinstance(m.get("content"), str)
                and m["content"].startswith("[结构化实体]")]
    record("结构化实体在所有专家处完整保留",
           len(ent_msgs) == 1 and "R-301" in ent_msgs[0]["content"] and "甲醇" in ent_msgs[0]["content"],
           f"ent={ent_msgs}")

    # ── 5. 开关可回退旧行为（向后兼容）──────────────────────────────────
    old = os.environ.get("SAFETYMIND_EXPERT_ISOLATION")
    try:
        os.environ["SAFETYMIND_EXPERT_ISOLATION"] = "0"
        t_eq_off = emp.assemble_task_for(req, AgentType.EQUIPMENT)
        t_cp_off = emp.assemble_task_for(req, AgentType.COMPLIANCE)
        record("SAFETYMIND_EXPERT_ISOLATION=0 时两专家背景相同（回退旧行为）",
               bg_of(t_eq_off) == bg_of(t_cp_off), "off 模式下应共享同一份")
    finally:
        if old is None:
            os.environ.pop("SAFETYMIND_EXPERT_ISOLATION", None)
        else:
            os.environ["SAFETYMIND_EXPERT_ISOLATION"] = old

    # ── 6. 边界：段落太少 / 全不相关时退回完整背景 ───────────────────────
    short_bg = "只有一段很短的材料：设备正常运行。"
    req_short = make_req(Req, IntentCategory, UrgencyLevel, context=short_bg, knowledge="")
    t_short = emp.assemble_task_for(req_short, AgentType.EQUIPMENT)
    record("背景段落过少时退回完整背景（不硬切丢信息）",
           short_bg in bg_of(t_short), f"bg={bg_of(t_short)[:80]}")

    neutral = "今天天气晴。\n\n会议改到周五。\n\n食堂装修完成。"
    req_neutral = make_req(Req, IntentCategory, UrgencyLevel, context=neutral, knowledge="")
    t_neu = emp.assemble_task_for(req_neutral, AgentType.EQUIPMENT)
    record("背景与领域全不相关时退回完整背景",
           "今天天气晴" in bg_of(t_neu) and "食堂装修" in bg_of(t_neu),
           f"bg={bg_of(t_neu)[:100]}")

    # ── 7. 空背景不崩 ───────────────────────────────────────────────────
    req_empty = make_req(Req, IntentCategory, UrgencyLevel, context="", knowledge="")
    t_empty = emp.assemble_task_for(req_empty, AgentType.EQUIPMENT)
    record("空背景时只保留用户问题，不抛异常",
           bg_of(t_empty) == "" and t_empty.messages[-1]["content"] == req_empty.message)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
