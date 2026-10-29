# -*- coding: utf-8 -*-
# 模型盲判工具测试（2026-10-26 闭环第 6 轮）。
#
# 背景：`blind_sample.json` 50 条 + 答案 key 早已备好，但 `blind_answers.json`
# 从未产生——标签始终只有 AI 自标注，没有独立核验。这是面试官追问
# "96.42% 怎么测的""标签是谁标的"时的硬伤。
#
# 真人工盲测需真人完成，无法代替；`blind_model_annotate.py` 做次优且可审计的一步：
# 让一个**从未见过 key** 的前沿模型独立判定，得到"模型盲判一致率"。
#
# 本测试锁定的是**工具的纪律**，不是那个 92% 的数字（数字随模型变化）：
#   1. 判定 prompt 里绝不出现答案 key（否则盲判毫无意义）
#   2. 样本只取 text 字段，不带 tier/label 等可能泄漏的元数据
#   3. 结果结构含 by_tier 与 disagreements（可用于优先复核）
#   4. 口径标注必须写明"不是人工盲测"
#   5. 19 类菜单完整
#
# 用法: python tests/test_blind_annotate.py
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []
HERE = Path(__file__).resolve().parents[1] / "benchmarks" / "intent"


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    src = (HERE / "blind_model_annotate.py").read_text(encoding="utf-8")

    # ── 1. 纪律：prompt 不含 key ──────────────────────────────────────────
    record("工具源码里 SYSTEM prompt 不引用 blind_answer_key",
           "blind_answer_key" not in src.split("def main")[0],
           "SYSTEM 段不得出现答案文件引用")

    # 关键实现细节：发给模型的 messages[1].content 必须就是纯文本
    record("发给模型的 user content 只用 text 字段",
           '"content": text' in src or "content\": text" in src,
           "不得把 tier/label/整条样本塞进 prompt")

    # ── 2. 样本与 key 数据完整性 ──────────────────────────────────────────
    sample = json.load(open(HERE / "blind_sample.json", encoding="utf-8"))
    key = json.load(open(HERE / "blind_answer_key.json", encoding="utf-8"))
    record("样本与答案 key 条数一致（50）", len(sample) == len(key) == 50,
           f"sample={len(sample)} key={len(key)}")
    texts = {s["text"] for s in sample}
    keymap = {r["text"]: r["label"] for r in key}
    record("key 覆盖全部样本文本（无遗漏）", texts == set(keymap.keys()),
           f"缺={texts - set(keymap.keys())} 多={set(keymap.keys()) - texts}")

    # ── 3. 样本结构：作答用字段不含 label ─────────────────────────────────
    leak = [s for s in sample if "label" in s]
    record("样本文件里不含 label 字段（作答者看不到答案）", not leak,
           f"泄漏 {len(leak)} 条")

    tiers = {s.get("tier") for s in sample}
    record("样本覆盖三档难度", {"canonical", "paraphrase", "near_miss"} <= tiers,
           f"tiers={tiers}")

    # ── 4. 19 类菜单完整 ─────────────────────────────────────────────────
    from core.intent_recognizer import IntentCategory
    labels = [c.value for c in IntentCategory]
    record("19 类意图菜单完整", len(labels) == 19, f"len={len(labels)}")
    record("工具里用 IntentCategory 生成菜单（不手写副本，防漂移）",
           "from core.intent_recognizer import IntentCategory" in src)

    # ── 5. 口径诚实：必须写明不是人工盲测 ────────────────────────────────
    record("口径标注写明不是人工盲测（防止冒充）",
           "不是人工盲测" in src or "model-as-blind-annotator" in src.lower(),
           "必须明确区分模型盲判与真人盲测")

    # ── 6. 产出的结果文件结构（若已跑过）──────────────────────────────────
    res_files = sorted((HERE / "results").glob("blind_model_*.json")) if (HERE / "results").exists() else []
    if res_files:
        d = json.load(open(res_files[-1], encoding="utf-8"))
        record("结果含 agreement 字段", "agreement" in d, f"keys={list(d.keys())[:8]}")
        record("结果含 by_tier 分档一致率", "by_tier" in d and len(d["by_tier"]) >= 3,
               f"by_tier={list(d.get('by_tier', {}).keys())}")
        record("结果含 disagreements 清单（供人工优先复核）",
               "disagreements" in d and isinstance(d["disagreements"], list))
        record("结果含逐条原始输出（可审计）",
               "rows" in d and len(d["rows"]) == 50, f"rows={len(d.get('rows', []))}")
        record("结果标注了方法口径", "method" in d and "不是人工盲测" in str(d.get("method", "")),
               f"method={d.get('method')}")
        rate = d.get("agreement")
        record("一致率在合理区间（0.7~1.0，不是明显坏掉）",
               isinstance(rate, (int, float)) and 0.7 <= rate <= 1.0, f"rate={rate}")
        # 不一致条目必须都带 key 与 pred（便于复核）
        bad = [r for r in d.get("disagreements", []) if not r.get("key") or not r.get("pred")]
        record("不一致条目都带 key 与 pred（可直接复核）", not bad, f"{len(bad)} 条缺字段")
    else:
        record("尚未产出盲判结果（跳过结构检查）", True, "未运行过 blind_model_annotate")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
