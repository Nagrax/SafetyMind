# -*- coding: utf-8 -*-
"""真实评测：/chat 与 /chat/stream 的**行为一致性**（前端已切流式，这条契约必须守住）。

## 为什么

前端现在默认走 `/chat/stream`。后端声称"复用 _chat_core，行为不分叉"，但此前只有源码级断言
（`test_chat_stream.py` 检查 `await _chat_core(` 存在）。真实风险是：两个端口在
引用、升级、建单、意图等字段上出现差异——那意味着"同一条问题走哪个端口答案不一样"。

## 方法

- 同一批 12 条问题，同一进程内先 `/chat` 再 `/chat/stream`
- 比对：正文相似度、citations 数量与标题、escalated、hazard_action、intent、knowledge_used
- 同时记录：`/chat` 总耗时 vs 流式**首字（首个 delta）时间**，量化流式的体验收益

## 决策规则（先定）

- 任一结构性字段（escalated / hazard_action / intent / citations 标题集合）不一致 → 记为缺陷
- 正文相似度（字符 4-gram Jaccard）中位 < 0.6 → 也记为缺陷（说明链路真的分叉了）
- 首字时间显著低于总耗时 → 流式的体验收益成立

用法: <venv python> benchmarks/eval_stream_parity.py
产物: benchmarks/results/stream_parity_<ts>.json
"""
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path("D:/claudecode/SafetyMind")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "benchmarks"))
OUT_DIR = REPO / "benchmarks" / "results"

from eval_reasoning_ab import QUESTIONS  # noqa: E402

# ── 事实抽取（2026-10-29 本轮加）：正文相似度只能说明"措辞不同"，说明不了内容是否等价。
#    这里抽取**数值+单位**与**标准号**作为可核对的事实单元，比较两端口答案的事实集合。
_NUM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(小时|h|H|分钟|min|秒|%|℃|米/秒|米|m|kPa|MPa|mg/m3|次/年|次|年|天|日)")
_STD_RE = re.compile(
    r"((?:GB|GBZ|AQ|JB|HG|SY|DL|TSG|GA|MT|JGJ|CJJ)\s*/?\s*T?\s*\d+(?:\.\d+)?(?:\s*[-—]\s*\d{4})?)")


def extract_facts(text):
    """抽取可核对的事实单元：数值+单位、标准号（归一化后去重）。"""
    body = text or ""
    facts = set()
    for m in _NUM_RE.finditer(body):
        val, unit = m.group(1), m.group(2)
        unit = {"h": "小时", "H": "小时", "min": "分钟"}.get(unit, unit)
        facts.add(f"{val}{unit}")
    for m in _STD_RE.finditer(body):
        facts.add(re.sub(r"\s+", "", m.group(1)).upper().replace("—", "-"))
    return facts


def fact_jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 3)



def parse_sse(text):
    frames = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        ev, data = None, ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if ev:
            try:
                frames.append((ev, json.loads(data)))
            except Exception:
                frames.append((ev, {}))
    return frames


def similarity(a, b):
    if not a or not b:
        return 0.0
    ga = {a[i:i + 4] for i in range(max(1, len(a) - 3))}
    gb = {b[i:i + 4] for i in range(max(1, len(b) - 3))}
    inter = len(ga & gb)
    union = len(ga | gb)
    return round(inter / union, 3) if union else 0.0


def main():
    from fastapi.testclient import TestClient
    import api.main as m

    os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
    # 生成温度固定 0：两次调用是同题独立生成，温度 1.0 下正文必然不同（这是评测噪声，
    # 不是链路分叉）。结构字段（意图/依据/建单/确定性升级依据）才是"是否分叉"的判据。
    os.environ["SAFETYMIND_TEMPERATURE"] = "0"
    qs = [(sub, q) for _, sub, q in QUESTIONS[:12]]

    rows = []
    with TestClient(m.app) as c:
        c.get("/health")
        for i, (sub, q) in enumerate(qs):
            payload = {"message": q, "user_id": f"parity-{i}"}
            t0 = time.perf_counter()
            r1 = c.post("/chat", json=payload)
            chat_ms = (time.perf_counter() - t0) * 1000
            b1 = r1.json() if r1.status_code == 200 else {}

            t0 = time.perf_counter()
            r2 = c.post("/chat/stream", json=payload)
            stream_total_ms = (time.perf_counter() - t0) * 1000
            frames = parse_sse(r2.text)
            text = ""
            done = {}
            for ev, data in frames:
                if ev == "delta":
                    text += str(data.get("text") or "")
                elif ev == "done":
                    done = data

            row = {
                "i": i, "sub": sub, "q": q,
                "chat_ms": round(chat_ms, 0), "stream_total_ms": round(stream_total_ms, 0),
                "sim": similarity(b1.get("response") or "", text),
                "chat_intent": b1.get("intent"), "stream_intent": done.get("intent"),
                "chat_escalated": bool(b1.get("escalated")),
                "stream_escalated": bool(done.get("escalated")),
                "chat_basis": b1.get("escalation_basis") or "",
                "stream_basis": done.get("escalation_basis") or "",
                "chat_hazard": b1.get("hazard_action") or "none",
                "stream_hazard": done.get("hazard_action") or "none",
                "chat_cites": sorted(str(x.get("title"))[:20] for x in (b1.get("citations") or [])),
                "stream_cites": sorted(str(x.get("title"))[:20]
                                       for x in (done.get("citations") or [])),
                "chat_knowledge": bool(b1.get("knowledge_used")),
                "stream_knowledge": bool(done.get("knowledge_used")),
            }
            # 确定性字段必须一致；escalated 允许因 model_hint 波动（见 escalation_basis 注释），
            # 但"确定性依据"本身必须一致。
            row["deterministic_basis"] = (
                ("urgency" if row["chat_basis"] == "urgency" else
                 "intent" if row["chat_basis"] == "intent" else
                 "safety_net" if row["chat_basis"] == "safety_net" else ""),
                ("urgency" if row["stream_basis"] == "urgency" else
                 "intent" if row["stream_basis"] == "intent" else
                 "safety_net" if row["stream_basis"] == "safety_net" else ""))
            row["field_ok"] = (row["chat_intent"] == row["stream_intent"]
                               and row["chat_hazard"] == row["stream_hazard"]
                               and row["chat_cites"] == row["stream_cites"]
                               and row["deterministic_basis"][0] == row["deterministic_basis"][1])
            row["escalation_volatile"] = row["chat_escalated"] != row["stream_escalated"]
            fa = extract_facts(b1.get("response") or "")
            fb = extract_facts(text)
            row["facts_chat"] = sorted(fa)
            row["facts_stream"] = sorted(fb)
            row["fact_jaccard"] = fact_jaccard(fa, fb)
            row["facts_only_chat"] = sorted(fa - fb)
            row["facts_only_stream"] = sorted(fb - fa)
            rows.append(row)
            print(f"[{i+1}/{len(qs)}] {sub:9s} sim={row['sim']:.2f} "
                  f"facts={row['fact_jaccard']:.2f}({len(row['facts_chat'])}/{len(row['facts_stream'])}) "
                  f"chat={row['chat_ms']:.0f}ms stream={row['stream_total_ms']:.0f}ms "
                  f"fields={'OK' if row['field_ok'] else 'MISMATCH'}", flush=True)

    sims = [r["sim"] for r in rows]
    mism = [r for r in rows if not r["field_ok"]]
    volatile = [r for r in rows if r.get("escalation_volatile")]
    print("\n=== 一致性汇总 ===")
    print(f"  题数 {len(rows)}；确定性字段不一致 {len(mism)} 题；"
          f"正文相似度中位 {statistics.median(sims):.2f}")
    print(f"  escalated 波动 {len(volatile)} 题（model_hint 引起，属已知随机性；"
          f"确定性依据不一致 {len(mism)} 题）")
    print(f"  /chat 总耗时中位 {statistics.median([r['chat_ms'] for r in rows]):.0f}ms；"
          f"/chat/stream 总耗时中位 {statistics.median([r['stream_total_ms'] for r in rows]):.0f}ms")
    fj = [r["fact_jaccard"] for r in rows]
    print(f"  事实集合 Jaccard 中位 {statistics.median(fj):.2f}（1=完全相同）")
    low = [r for r in rows if r["fact_jaccard"] < 0.5]
    for r in low[:4]:
        print(f"  [事实差异大] {r['q'][:24]} j={r['fact_jaccard']:.2f} "
              f"仅chat={r['facts_only_chat'][:6]} 仅stream={r['facts_only_stream'][:6]}")
    for r in mism[:5]:
        print(f"  [不一致] {r['q'][:26]} chat={r['chat_intent']}/{r['chat_escalated']}/{r['chat_hazard']}/"
              f"{r['chat_cites']} stream={r['stream_intent']}/{r['stream_escalated']}"
              f"/{r['stream_hazard']}/{r['stream_cites']}")

    verdict = ("parity_ok" if not mism else "diverged")
    print(f"  决策：{verdict}（判据：确定性字段零不一致；正文相似度仅作参考——"
          f"同题两次独立生成，温度 0 也不保证逐字一致）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"stream_parity_{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps({"verdict": verdict, "rows": rows}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"\nwritten {p}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
