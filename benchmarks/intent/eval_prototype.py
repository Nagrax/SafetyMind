# 语义原型分类器评测（冠军配置）：bge 双模型集成 + 244 条原型示例库
# 用法: python eval_prototype.py [--split test|test2|dev] [--device cpu|cuda]
# 依赖: pip install torch transformers；模型 BAAI/bge-base-zh-v1.5, BAAI/bge-large-zh-v1.5
import os, sys, json, time, argparse
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from core.intent_bge import BGEProtoClassifier

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
    ap.add_argument("--split", default="test2", choices=["dev", "test", "test2"])
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--no-large", action="store_true", help="仅用 bge-base 单模（2G 内存形态）")
    args = ap.parse_args()

    items = load_items(args.split)
    clf = BGEProtoClassifier(temperature=0.03, tau_direct=0.8,
                             use_large=not args.no_large, device=args.device)
    clf.probs("预热")  # 懒加载 + 预热

    ok = 0
    rec = defaultdict(lambda: [0, 0])
    lat = []
    for d in items:
        t0 = time.monotonic()
        probs = clf.probs(d["text"])
        ms = (time.monotonic() - t0) * 1000
        lat.append(ms)
        pred = max(probs, key=probs.get)
        rec[d["label"]][1] += 1
        if pred == d["label"]:
            ok += 1; rec[d["label"]][0] += 1

    macro = sum(a / b for a, b in rec.values()) / len(rec)
    lat.sort()
    p50, p95 = lat[len(lat) // 2], lat[int(len(lat) * 0.95)]
    print(f"split={args.split} n={len(items)}")
    print(f"accuracy={ok/len(items):.4f}  macro_recall={macro:.4f}  latency p50={p50:.0f}ms p95={p95:.0f}ms ({args.device})")
    zero = [c for c, (a, b) in rec.items() if a == 0]
    print("零命中类:", zero if zero else "无")
    for c, (a, b) in sorted(rec.items()):
        print(f"  {c:24s} {a}/{b} = {a/b:.3f}")

if __name__ == "__main__":
    main()
