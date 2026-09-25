# 数据集构建与审计说明
# intent_eval_v3.1.json 与 test2_v4.json 为最终产物，直接随仓库发布。
# 本脚本校验数据集完整性并输出统计（不重新生成——生成依赖 GLM 的框架设计与示例撰写，见 docs/BENCHMARK.md 第 2 节）。
import json, os, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
v31 = json.load(open(os.path.join(HERE, "intent_eval_v3.1.json"), encoding="utf-8"))
t2 = json.load(open(os.path.join(HERE, "test2_v4.json"), encoding="utf-8"))

from core.intent_recognizer import IntentCategory
VALID = {c.value for c in IntentCategory}

errs = []
seen = set()
for d in v31["data"] + t2["new_items"]:
    if d["label"] not in VALID:
        errs.append(("非法标签", d["text"]))
    if d["text"] in seen:
        errs.append(("重复", d["text"]))
    seen.add(d["text"])

print(f"v3.1: {len(v31['data'])} 条 (dev/test) | test2: {len(t2['new_items'])} 条")
print("分档:", dict(Counter(d['tier'] for d in v31['data'])))
print("完整性错误:", len(errs))
for e in errs[:5]:
    print(" ", e)
print("审计记录:", v31["meta"].get("audit_note", "-"))
sys.exit(1 if errs else 0)
