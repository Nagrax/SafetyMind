# -*- coding: utf-8 -*-
# 事实抽取与对比的单元测试（2026-10-29 闭环第 48 轮）。
#
# 用途：/chat 与 /chat/stream 的同题对照里，正文相似度只有 0.12~0.17（同题两次独立生成，
# 措辞必然不同），说明不了"内容是否等价"。于是用**可核对的事实单元**（数值+单位、标准号）
# 做第二判据：实测 Jaccard 中位 0.82，比词面相似度有解释力。
#
# 本测试锁定抽取口径（口径漂移会让判据失真）：
#   1. 数值+单位（h/min 归一化为 小时/分钟）
#   2. 标准号（去空格、破折号统一、大写）
#   3. 去重与空集语义（两边都空 → 1.0；一边空 → 0.0）
#
# 用法: python tests/test_fact_extraction.py
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "benchmarks"))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    from eval_stream_parity import extract_facts, fact_jaccard

    f = extract_facts("特殊动火不超过8小时，一级不超过24小时，二级不超过72小时")
    record("抽取 小时 类数值", f == {"8小时", "24小时", "72小时"}, f"got={sorted(f)}")

    f = extract_facts("作业前30分钟内取样，氧含量 19.5%~21%")
    record("抽取 分钟/百分比", {"30分钟", "19.5%", "21%"} <= f, f"got={sorted(f)}")
    record("30min / 8h 归一化为中文单位",
           extract_facts("30min 内完成，有效期 8h") == {"30分钟", "8小时"},
           f"got={sorted(extract_facts('30min 内完成，有效期 8h'))}")

    f = extract_facts("依据 GB 30871-2022 与 GBZ/T 229.4")
    record("抽取标准号（去空格、统一大写）",
           {"GB30871-2022", "GBZ/T229.4"} <= f, f"got={sorted(f)}")

    f = extract_facts("见 GB/T 1234—2020")
    record("破折号统一为连字符", "GB/T1234-2020" in f, f"got={sorted(f)}")

    record("重复事实只保留一份",
           extract_facts("8小时 8小时 8h") == {"8小时"},
           f"got={sorted(extract_facts('8小时 8小时 8h'))}")

    record("无事实文本 → 空集合", extract_facts("好的，我明白了。") == set())

    record("完全相同集合 → 1.0", fact_jaccard({"8小时"}, {"8小时"}) == 1.0)
    record("完全不相交 → 0.0", fact_jaccard({"8小时"}, {"72小时"}) == 0.0)
    record("两边都空 → 1.0（不惩罚无事实的短答）",
           fact_jaccard(set(), set()) == 1.0)
    record("部分重叠按 Jaccard", fact_jaccard({"a", "b"}, {"b", "c"}) == round(1 / 3, 3))

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
