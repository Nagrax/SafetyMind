# 盲测评分器：对比人工作答与隐藏答案 key
# 用法: python score_blind.py 你的作答.json
# 作答文件格式: [{"text": "...", "answer": "<意图值>"}, ...]
import os, sys, json
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))

def main(path):
    key = {r["text"]: r["label"] for r in json.load(open(os.path.join(HERE, "blind_answer_key.json"), encoding="utf-8"))}
    answers = json.load(open(path, encoding="utf-8"))
    ok = 0; n = 0; missing = 0
    conf = defaultdict(lambda: defaultdict(int))
    by_tier = defaultdict(lambda: [0, 0])
    tier_of = {r["text"]: r.get("tier", "") for r in json.load(open(os.path.join(HERE, "blind_sample.json"), encoding="utf-8"))}
    for a in answers:
        lab = key.get(a["text"])
        if lab is None:
            missing += 1; continue
        n += 1
        t = tier_of.get(a["text"], "")
        by_tier[t][1] += 1
        if a.get("answer") == lab:
            ok += 1; by_tier[t][0] += 1
        else:
            conf[(lab, a.get("answer"))] += 1
    print(f"人工盲测: n={n}（缺失/无效 {missing}）")
    print(f"一致率: {ok/max(1,n):.4f}")
    for t, (a, b) in sorted(by_tier.items()):
        print(f"  {t:12s} {a}/{b} = {a/b:.3f}")
    print("主要分歧（真实 → 人工判定）:")
    for (l, p), c in sorted(conf.items(), key=lambda x: -x[1])[:8]:
        print(f"  {l} → {p}: {c}")
    print("\n结论口径: 一致率即'人工可复现的标签比例'；分歧条目建议逐条复核后定稿。")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "blind_answers.json")
