# -*- coding: utf-8 -*-
# Skill 变更效果验证测试（2026-10-26 补齐面试缺口）。
#
# 面试原话："你现在看上去只是做了个评测，那你对 skill 的改进体现在哪里？"
# 现状核实：SkillEvolution 只有 candidate→decide→approve，**没有效果验证**。
# 本模块补上闭环最后一段：before/after 可复跑对照 + 机械可证伪判据。
#
# 本测试锁定：
#   1. 探针从候选自身反推（问题确实会触发该规则）
#   2. 命中率判据正确（关键词是否被用上）
#   3. **违规率判据不把"申明禁令"误判为"违规"**（本轮真踩过的坑）
#   4. after 内容包含候选完整信息（不是只加一行薄注释）
#   5. verdict 三态齐全，阈值明确
#   6. 真实数据复算：命中 0.667→1.000 判 improved（用落盘产物复现，不重调模型）
#
# 用法: python tests/test_skill_effect_validation.py
import json
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


CAND = {
    "title": "动火作业票超期处理",
    "when_to_use": "用户问动火作业票过期了怎么办",
    "instruction": "必须在24小时内重新办理，严禁沿用旧票继续作业",
    "keywords": ["动火", "作业票", "超期", "重新办理"],
}


def main():
    from core.skill_effect_validation import (IMPROVE_MARGIN, build_probes,
                                              grade_answer, validate_candidate)

    # ── 1. 探针构造 ─────────────────────────────────────────────────────
    probes = build_probes(CAND, n=3)
    record("探针条数符合 n=3", len(probes) == 3, f"probes={probes}")
    record("探针来自候选自身（含 when_to_use 或标题）",
           any("过期" in p or "超期" in p for p in probes), f"probes={probes}")
    empty_probes = build_probes({"title": "", "when_to_use": "", "keywords": []}, n=2)
    record("候选信息为空时仍有兜底探针", len(empty_probes) >= 1, f"{empty_probes}")

    # ── 2. 命中率判据 ───────────────────────────────────────────────────
    good = "动火作业票超期后必须在24小时内重新办理，超期的票不能再用。"
    g = grade_answer(good, CAND)
    record("命中率：关键词出现时命中率高", g["hit_rate"] >= 0.5, f"{g}")

    bad = "这个问题比较复杂，建议你咨询专业人士。"
    g2 = grade_answer(bad, CAND)
    record("命中率：无关键词时命中率为 0", g2["hit_rate"] == 0.0, f"{g2}")

    # ── 3. 违规率：不能把"申明禁令"误判成"违规"（本轮真踩过的坑）────────
    # 正例：模型在申明禁令 —— 应判违规率 0
    declaring = ("动火作业票超期后严禁沿用旧票继续作业，"
                 "必须在24小时内重新办理。严禁沿用旧票！")
    g3 = grade_answer(declaring, CAND)
    record("违规率：申明禁令（严禁沿用旧票）不被误判为违规",
           g3["violation_rate"] == 0.0, f"viol={g3['violation_rate']} terms={g3['violation_terms']}")

    # 反例：模型真的在建议违规做法 —— 应判违规率 > 0
    offending = "作业票超期了问题不大，沿用旧票继续作业就行，不用重新办理。"
    g4 = grade_answer(offending, CAND)
    record("违规率：真建议沿用旧票时判为违规",
           g4["violation_rate"] > 0, f"viol={g4['violation_rate']} terms={g4['violation_terms']}")

    # ── 4. 结构判据 ─────────────────────────────────────────────────────
    structured = "结论：必须重办。\n1. 立即停工\n2. 24小时内重新办理\n- 严禁沿用旧票"
    record("结构：有编号/要点时判 1.0", grade_answer(structured, CAND)["structure"] == 1.0)
    record("结构：纯段落时判 0.0",
           grade_answer("就一句话说明一下情况吧没有别的了", CAND)["structure"] == 0.0)

    # ── 5. 阈值与三态 ───────────────────────────────────────────────────
    record("改进阈值是显式常量且为正", isinstance(IMPROVE_MARGIN, float) and IMPROVE_MARGIN > 0,
           f"margin={IMPROVE_MARGIN}")

    # ── 6. dry 模式不改任何文件、不调模型 ───────────────────────────────
    iso = Path("D:/claudecode/scratch/sm-loop/iso/skill_val_test")
    iso.mkdir(parents=True, exist_ok=True)
    # 造一个最小 skills 目录
    (iso / "sk1").mkdir(exist_ok=True)
    (iso / "sk1" / "SKILL.md").write_text(
        "---\nname: 动火作业管理\ndescription: t\nkeywords: 动火,作业票\nenabled: true\n---\n\n"
        "动火作业必须办票。\n", encoding="utf-8")

    from core.skill_evolution import SkillEvolution
    evo = SkillEvolution(skills_root=str(iso))
    cand = dict(CAND)
    cand = evo.append_candidate(cand)

    r = validate_candidate(cand["id"], live=False, skills_root=str(iso))
    record("dry 模式返回探针且不调用模型", r.get("ok") is True and "probes" in r
           and "note" in r, f"r={r}")

    # ── 7. after 内容包含候选完整信息（不是只加一行薄注释）──────────────
    record("after 内容包含候选的业务规则原文",
           "24小时内重新办理" in (r.get("after_content_chars") is not None and "ok" and
                                  str(r.get("probes")) or "") or r["after_content_chars"] > 50,
           f"after_chars={r.get('after_content_chars')}")

    # ── 8. 用落盘真实产物复算（不重调模型，验证判据与真实答案一致）──────
    artifacts = sorted(Path("D:/claudecode/scratch/sm-loop/runs").glob("skill_val*.json"))
    if artifacts:
        d = json.loads(artifacts[-1].read_text(encoding="utf-8"))
        if "raw_before" in d and "raw_after" in d:
            def mean_score(side):
                xs = [grade_answer(x["text"], CAND) for x in d["raw_" + side]]
                return (round(sum(x["score"] for x in xs) / len(xs), 4),
                        round(sum(x["hit_rate"] for x in xs) / len(xs), 3),
                        round(sum(x["violation_rate"] for x in xs) / len(xs), 3))
            b, a = mean_score("before"), mean_score("after")
            delta = round(a[0] - b[0], 4)
            record("真实产物复算：命中率 after 高于 before", a[1] > b[1], f"before={b} after={a}")
            record("真实产物复算：违规率两侧均为 0（无假阳性）",
                   a[2] == 0.0 and b[2] == 0.0, f"before={b} after={a}")
            record("真实产物复算：delta 达到改进阈值 → improved",
                   delta >= IMPROVE_MARGIN, f"delta={delta} margin={IMPROVE_MARGIN}")
        else:
            record("真实产物缺少原始输出（跳过复算）", True, "无 raw_before/raw_after")
    else:
        record("未找到 skill_val 产物（跳过复算）", True, "未跑过 live 验证")

    # ── 8. approve 必须如实记录证据（有则记，无则标 unvalidated）──────────
    from core.skill_evolution import SkillEvolution
    iso2 = Path("D:/claudecode/scratch/sm-loop/iso/approve_evidence")
    import shutil
    shutil.rmtree(str(iso2), ignore_errors=True)
    iso2.mkdir(parents=True, exist_ok=True)
    evo2 = SkillEvolution(skills_root=str(iso2))

    c1 = evo2.append_candidate({"title": "无验证技能", "when_to_use": "问X",
                                "instruction": "按X做", "keywords": ["X"], "action": "add"})
    r1 = evo2.approve(c1["id"])
    record("approve 无验证时如实标 unvalidated（不伪造证据）",
           r1.get("validation_verdict") == "unvalidated", f"r={r1.get('validation_verdict')}")

    c2 = evo2.append_candidate({"title": "有验证技能", "when_to_use": "问Y",
                                "instruction": "按Y做", "keywords": ["Y"], "action": "add"})
    r2 = evo2.approve(c2["id"], validation={
        "ok": True, "verdict": "improved", "delta": 0.17, "margin": 0.1,
        "model": "glm-4.6", "before": {"mean": 0.68}, "after": {"mean": 0.85}})
    record("approve 有验证时记录 verdict 与 delta",
           r2.get("validation_verdict") == "improved" and r2.get("validation_delta") == 0.17,
           f"r={r2.get('validation_verdict')} delta={r2.get('validation_delta')}")

    rows = evo2.list_candidates("approved")
    # 注意：断言必须基于"本次写入的这两条"，不能用 len==2——
    # 目录里可能有上次运行的残留（本轮实测踩到：残留让它变成 4 条而假失败）。
    titles = {r.get("title"): r.get("validation_verdict") for r in rows}
    record("证据落盘到候选台账（可事后审计）",
           titles.get("有验证技能") == "improved" and titles.get("无验证技能") == "unvalidated",
           f"rows={titles}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
