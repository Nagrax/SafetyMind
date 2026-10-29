# -*- coding: utf-8 -*-
"""Skill 变更效果验证（2026-10-26 补齐面试暴露的缺口）。

## 面试原话与问题

面试官追问："你现在看上去只是做了个评测，那你对 skill 的改进体现在哪里？"
当时的回答顺序反了：先讲怎么评，最后才讲怎么改，**始终没有说"改完之后凭什么说改好了"**。

现状核实（本轮实测）：`SkillEvolution` 只有 append_candidate / decide_against_skills /
approve / reject，**没有任何"效果验证/晋升/回放评分"能力**。
也就是说：候选被审批写入后，没有任何机制证明它比原来好。

## 本模块补的是"闭环的最后一段"

对每条 candidate 建立 **before / after 可复跑对照**：

    before = 用变更前的 skill 内容组装 prompt → 跑一组探针问答 → 打分
    after  = 用变更后的 skill 内容组装 prompt → 跑同一组探针问答 → 打分
    verdict = after >= before + MARGIN 才判"有改进"

## 判据设计（机械、可证伪、不靠"看起来更好"）

三维加权，全部程序化：

  1. **命中率 hits**：探针问题的关键词是否出现在回答里（业务规则是否被用上）
  2. **违规率 violations**：回答是否包含该 skill 明令禁止的做法（越低越好）
  3. **结构完整 structure**：是否给出了可执行步骤（有编号/要点）

## 为什么不直接跑真实用户流量

因为那需要生产流量与人工标注，本项目没有上线。用**探针集**是当前条件下的
最优近似：探针由 candidate 自身的 instruction 反向构造，因此与变更内容强相关。

## 用法

    # 对某条 pending 候选做 before/after 验证
    <venv python> core/skill_effect_validation.py --cand-id cand-xxx --live

    # 只看探针构造（不调模型，验证探针质量）
    <venv python> core/skill_effect_validation.py --cand-id cand-xxx --dry
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path("D:/claudecode/SafetyMind")
sys.path.insert(0, str(REPO))

# 判定阈值：after 必须比 before 高这么多才算"有改进"（不是"没变差"）
IMPROVE_MARGIN = 0.10
# 单维权重（命中率最重要：它直接反映业务规则是否被用上）
W_HIT, W_VIOLATION, W_STRUCTURE = 0.5, 0.3, 0.2


def build_probes(cand: Dict[str, Any], n: int = 3) -> List[str]:
    """从 candidate 自身反推探针问题。

    设计：候选的 when_to_use 说明何时该用这条规则，instruction 说明该怎么答。
    探针 = 模拟用户在该场景下的提问，确保问题**确实会触发这条规则**。
    """
    when = (cand.get("when_to_use") or "").strip()
    title = (cand.get("title") or "").strip()
    kws = cand.get("keywords") or []

    base = []
    if when:
        # when_to_use 通常写成"当用户问…时"，直接转成问句
        base.append(when if when.endswith(("？", "?")) else when + "？")
    if title:
        base.append(f"{title}的具体要求是什么？")
    for k in kws[:2]:
        base.append(f"关于{k}，有什么规定？")

    # 去重保序，截到 n 条
    seen, out = set(), []
    for q in base:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
        if len(out) >= n:
            break
    return out or ["安全相关的常见问题有哪些？"]


def grade_answer(text: str, cand: Dict[str, Any]) -> Dict[str, Any]:
    """机械可证伪的三维打分。"""
    t = text or ""
    kws = [k for k in (cand.get("keywords") or []) if k]
    instr = (cand.get("instruction") or "").strip()

    # ① 命中率：关键词出现在回答里
    hit = sum(1 for k in kws if k in t)
    hit_rate = hit / len(kws) if kws else 0.0

    # ② 违规率：instruction 里若有"禁止/不得/严禁"的表述，回答中出现这些被禁做法即违规
    #
    # 修正（本轮实测发现的判据缺陷，必须记录防重犯）：
    #   第一版只做"被禁短语 in 回答"的字符串包含判断，结果 after 版本因为
    #   **正确地反复强调"严禁沿用旧票"**，被误判成违规率 0.667（before 是 0）。
    #   于是"命中率 0.667→1.000 的真实改善"被违规率的假阳性抵消，最终判 no_change。
    #   根因：被禁行为短语前面若紧跟"严禁/禁止/不得"，说明模型是在**申明禁令**，不是在做。
    #   → 修正为：只有在"没有否定词紧邻前缀"的情况下，才算真违规。
    viol = 0
    viol_terms = []
    neg_markers = ("严禁", "禁止", "不得", "不能", "不可", "杜绝", "避免", "切勿", "不要")
    total_checks = 0
    for kw in ("严禁", "禁止", "不得", "不能", "不可"):
        if kw not in instr:
            continue
        total_checks += 1
        idx = instr.index(kw)
        frag = instr[idx + len(kw):idx + len(kw) + 12].strip("，。、 ：: ")
        # 取被禁行为的前 4 个字做特征（太短会误命中）
        frag = frag[:6]
        if len(frag) < 2 or frag not in t:
            continue
        # 关键：检查该短语在回答中的出现位置前后是否有否定词。
        # 若紧邻 -8 字符内出现"严禁/禁止/…"，说明模型在申明禁令 → 不算违规。
        pos = t.find(frag)
        window = t[max(0, pos - 8):pos]
        declared_as_ban = any(nm in window for nm in neg_markers)
        if not declared_as_ban:
            viol += 1
            viol_terms.append(frag)
    viol_rate = viol / total_checks if total_checks else 0.0

    # ③ 结构完整：有可执行步骤
    structure = 1.0 if any(k in t for k in ("\n1", "\n-", "步骤", "①", "首先")) else 0.0

    score = (W_HIT * hit_rate + W_VIOLATION * (1.0 - viol_rate) * 0.5
             + W_STRUCTURE * structure)
    return {
        "hit_rate": round(hit_rate, 3),
        "violation_rate": round(viol_rate, 3),
        "violation_terms": viol_terms,
        "structure": structure,
        "score": round(score, 4),
        "len": len(t),
    }


async def run_side(system_prompt: str, probes: List[str], model: str,
                   base_url: str, api_key: str) -> List[Dict[str, Any]]:
    """用给定 system prompt 跑一遍探针，返回每条的打分。"""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=120, max_retries=0)
    out = []
    for q in probes:
        t0 = time.monotonic()
        try:
            r = await client.messages.create(
                model=model, max_tokens=800, temperature=0.0, system=system_prompt,
                messages=[{"role": "user", "content": q}],
                extra_body={"thinking": {"type": "disabled"}},
            )
            txt = "".join(getattr(b, "text", "") for b in r.content
                          if getattr(b, "type", "") == "text")
            out.append({"q": q, "text": txt[:600],
                        "ms": round((time.monotonic() - t0) * 1000, 1), "error": None})
        except Exception as e:
            out.append({"q": q, "text": "", "ms": round((time.monotonic() - t0) * 1000, 1),
                        "error": f"{type(e).__name__}: {str(e)[:120]}"})
    return out


def skill_prompt_from(content: str) -> str:
    """把 SKILL.md 正文包成 system prompt（模拟 SkillManager 注入形态）。"""
    return ("你是安全生产助手。以下是你的业务技能规范，必须遵守：\n\n"
            + content.strip()[:4000])


def validate_candidate(cand_id: str, live: bool = False,
                       skills_root: Optional[str] = None) -> Dict[str, Any]:
    """对一条 candidate 做 before/after 对照，返回 verdict。"""
    from core.skill_evolution import SkillEvolution
    from core.skill_loader import SkillManager

    root = skills_root or str(REPO / "skills")
    evo = SkillEvolution(skills_root=root)
    cand = next((c for c in evo.list_candidates() if c.get("id") == cand_id), None)
    if cand is None:
        return {"ok": False, "error": f"候选不存在: {cand_id}"}

    probes = build_probes(cand)

    # before：现有 skill 内容（若 merge，取目标 skill；若 add，取最相似 skill 或空）
    sm = SkillManager(root_dir=root, max_prompt_chars=5000)
    sm.load()
    target_name = cand.get("target_skill") or ""
    before_content = ""
    for s in sm.skills:
        if target_name and s.name == target_name:
            before_content = s.content
            break
    if not before_content:
        # add 场景：before = 没有任何该领域技能
        before_content = "（无相关业务技能）"

    # after：现有内容 + 候选修订（与 _merge_into_skill 同构）
    #
    # 修正（本轮实测发现的公平性问题）：
    #   add 场景下 before 是"（无相关业务技能）"只有 9 字符，而 after 只加了一段
    #   68 字符的修订——两边都太薄，测出来的差异其实是"噪声 vs 噪声"，
    #   不能证明候选有价值。正确的对照是：
    #     before = 现有技能（可能为空）
    #     after  = 现有技能 + **候选的完整内容**（标题/适用场景/业务规则/关键词）
    #   这样 after 才真正代表"采纳这条候选后系统会发生什么"。
    addition = (
        f"\n\n## 运营修订\n"
        f"- 技能标题：{cand.get('title', '')}\n"
        f"- 适用：{cand.get('when_to_use', '')}\n"
        f"- 业务规则：{cand.get('instruction', '')}\n"
        f"- 关键词：{', '.join(cand.get('keywords') or [])}\n"
    )
    after_content = before_content + addition

    result: Dict[str, Any] = {
        "ok": True, "cand_id": cand_id, "title": cand.get("title"),
        "action": cand.get("action"), "probes": probes,
        "before_content_chars": len(before_content),
        "after_content_chars": len(after_content),
    }

    if not live:
        result["note"] = "dry 模式：只构造探针，未调用模型"
        return result

    base_url = os.getenv("ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    model = os.getenv("ANTHROPIC_MODEL", "glm-4.6")
    if not api_key:
        result["ok"] = False
        result["error"] = "缺少 ANTHROPIC_API_KEY"
        return result

    import asyncio
    before_runs, after_runs = asyncio.run(_both_sides(
        skill_prompt_from(before_content), skill_prompt_from(after_content),
        probes, model, base_url, api_key))

    b_scores = [grade_answer(r["text"], cand) for r in before_runs]
    a_scores = [grade_answer(r["text"], cand) for r in after_runs]

    def mean(xs: List[float]) -> float:
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    b_total = mean([s["score"] for s in b_scores])
    a_total = mean([s["score"] for s in a_scores])
    delta = round(a_total - b_total, 4)

    result.update({
        "model": model,
        "before": {"scores": b_scores, "mean": b_total,
                   "hit_mean": mean([s["hit_rate"] for s in b_scores]),
                   "violation_mean": mean([s["violation_rate"] for s in b_scores]),
                   "structure_mean": mean([s["structure"] for s in b_scores])},
        "after": {"scores": a_scores, "mean": a_total,
                  "hit_mean": mean([s["hit_rate"] for s in a_scores]),
                  "violation_mean": mean([s["violation_rate"] for s in a_scores]),
                  "structure_mean": mean([s["structure"] for s in a_scores])},
        "delta": delta,
        "verdict": ("improved" if delta >= IMPROVE_MARGIN
                    else "regressed" if delta <= -IMPROVE_MARGIN
                    else "no_change"),
        "margin": IMPROVE_MARGIN,
        "raw_before": before_runs, "raw_after": after_runs,
    })
    return result


async def _both_sides(before_prompt: str, after_prompt: str, probes: List[str],
                      model: str, base_url: str, api_key: str):
    import asyncio
    # 两侧并发跑，省一半时间（两次独立 system prompt，无共享状态）
    return await asyncio.gather(
        run_side(before_prompt, probes, model, base_url, api_key),
        run_side(after_prompt, probes, model, base_url, api_key),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cand-id", required=True)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--skills-root", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    r = validate_candidate(args.cand_id, live=args.live,
                           skills_root=args.skills_root or None)
    print(json.dumps(r, ensure_ascii=False, indent=2))

    if r.get("ok") and args.live:
        print(f"\n=== 判定：{r['verdict']}（delta={r['delta']:+.4f}，阈值 ±{r['margin']}）===")
        print(f"  命中率  {r['before']['hit_mean']:.3f} -> {r['after']['hit_mean']:.3f}")
        print(f"  违规率  {r['before']['violation_mean']:.3f} -> {r['after']['violation_mean']:.3f}")
        print(f"  结构    {r['before']['structure_mean']:.3f} -> {r['after']['structure_mean']:.3f}")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten {args.json}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
