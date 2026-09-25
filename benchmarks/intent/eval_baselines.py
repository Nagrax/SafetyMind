# 基线评测：pattern 关键词（零依赖，不调 LLM）；Laya 与 LLM 融合基线见 docs/BENCHMARK.md 第 3、5 节
# 用法: python eval_baselines.py --split test
import os, sys, json, argparse
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from core.intent_recognizer import IntentRecognizer

HERE = os.path.dirname(os.path.abspath(__file__))

def load_items(split):
    if split == "test2":
        data = json.load(open(f"{HERE}\\test2_v4.json", encoding="utf-8"))["new_items"]
        removed = {
            ".中控室人员疏散集合点的设置和引导怎么做？", ".中控室人员疏散集合点的设置和引导咋做？",
            ".仓库人员疏散集合点的设置和引导怎么做？", ".车间人员疏散集合点的设置和引导怎么做？",
            ".装卸区人员疏散集合点的设置和引导怎么做？",
            "政府部门联合检查和专项检查的区别是什么？", "政府部门联合看和专项检查的区别是什么呗？",
        }
        return [d for d in data if d["text"] not in removed]
    return [d for d in json.load(open(f"{HERE}\\intent_eval_v3.1.json", encoding="utf-8"))["data"]
            if d["split"] in (split, "all")]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test")
    args = ap.parse_args()
    items = load_items(args.split)

    # pattern 关键词基线：真实 IntentRecognizer 实例（dummy API 参数，pattern 不调 LLM）
    rec_obj = IntentRecognizer(api_key="dummy", base_url=None, model="dummy")
    ok = 0
    rec = defaultdict(lambda: [0, 0])
    for d in items:
        p = rec_obj._pattern_recognize(d["text"])
        iv = p.get("intent")
        iv = iv.value if hasattr(iv, "value") else str(iv)
        rec[d["label"]][1] += 1
        if iv == d["label"]:
            ok += 1; rec[d["label"]][0] += 1
    macro = sum(a / b for a, b in rec.values()) / len(rec)
    print(f"[pattern 关键词] split={args.split} n={len(items)} accuracy={ok/len(items):.4f} macro_recall={macro:.4f}")
    print("Laya 与 LLM 融合基线的实现与结果记录见 docs/BENCHMARK.md 第 3、5 节")

if __name__ == "__main__":
    main()
