# 盲测工具包生成器：从审计基准分层抽取 60 条，标签隐藏
# 用法: python make_blind_test.py
# 产出: blind_sample.json（作答用，无标签）/ blind_answer_key.json（评分用，勿看）
# 作答后运行: python score_blind.py 你的作答.json
import os, sys, json, random

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, PARENT)
from core.intent_recognizer import IntentCategory

CLASS_MENU = "\n".join(f"- `{c.value}`" for c in IntentCategory)

def main():
    random.seed(20260925)
    v31 = json.load(open(os.path.join(HERE, "intent_eval_v3.1.json"), encoding="utf-8"))["data"]
    t2 = [d for d in json.load(open(os.path.join(HERE, "test2_v4.json"), encoding="utf-8"))["new_items"]]
    by_key = {}
    for src, d in [("v31", x) for x in v31] + [("t2", x) for x in t2]:
        by_key.setdefault((d["tier"], src), []).append(d)

    plan = [("canonical", "v31", 12), ("canonical", "t2", 6),
            ("paraphrase", "v31", 10), ("paraphrase", "t2", 5),
            ("near_miss", "v31", 17), ("near_miss", "t2", 10)]
    used, sample, key = set(), [], []
    for tier, src, n in plan:
        pool = [x for x in by_key.get((tier, src), []) if x["text"] not in used]
        random.shuffle(pool)
        for d in pool[:n]:
            used.add(d["text"])
            sample.append({"text": d["text"], "tier": tier, "src": src})
            key.append({"text": d["text"], "label": d["label"]})
    random.shuffle(sample)

    json.dump(sample, open(os.path.join(HERE, "blind_sample.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(key, open(os.path.join(HERE, "blind_answer_key.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    md = ["# 人工盲测作答表", "",
          f"共 {len(sample)} 条。请把每条的 `?` 替换为下方 19 个意图值之一。",
          "完成后另存为 `blind_answers.json`（格式与样本一致，把 tier 字段换成 answer 字段即可，或直接逐条告诉我）。", "",
          "## 意图菜单", CLASS_MENU, ""]
    for n, s in enumerate(sample, 1):
        md.append(f"{n}. `{s['text']}` → 答案: `?`")
    open(os.path.join(HERE, "blind_worksheet.md"), "w", encoding="utf-8").write("\n".join(md))
    print(f"盲测包已生成: 样本 {len(sample)} 条 → blind_sample.json / blind_worksheet.md")
    print(f"答案 key（评分用，作答时勿看）: blind_answer_key.json")

if __name__ == "__main__":
    main()
