# -*- coding: utf-8 -*-
"""延迟分解：/chat 的端到端时间都花在哪一段。

## 为什么

真实 /chat 实测中位 20~40s（判定类更长）。要优化必须先分清：
  - 非生成段（记忆/意图/RAG/路由/审计）：能靠工程优化压下去；
  - 生成段（模型 TTFT + 解码）：受端点与模型影响。
服务端响应里已有两个字段可用来分账：`latency_ms`（Agent 处理，含生成）与
`total_ms`（全链路，含排队/记忆/意图/RAG/后处理），差额即非生成开销。

## 方法

6 条真实问题（覆盖 fact / grading / writing / emergency / 短问），逐条记录：
wall（客户端实测）、total_ms、latency_ms、差额、正文字数、引用数。
再算：非生成占比 = (total - latency) / total。

## 决策规则

- 非生成占比 ≥ 40% → 先做工程优化（RAG/意图/记忆），并给出分阶段耗时；
- 非生成占比 < 20% → 瓶颈在生成，转做"模型/预算/提示词"侧（不改工程链路）。

用法: <venv python> benchmarks/profile_stage_split.py
产物: benchmarks/results/stage_split_<ts>.json
"""
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

CASES = [
    ("fact", "动火作业票分几个等级？各级有效期多久？依据哪个标准？"),
    ("grading", "安全阀超期未校验，应判定为一般事故隐患还是重大事故隐患？请给出判定依据与理由。"),
    ("writing", "帮我写一条隐患排查记录，内容是一号库房灭火器压力表在红区。"),
    ("emergency", "车间发生氨气泄漏，已经有人中毒晕倒了，现在该怎么办？"),
    ("short", "你好"),
    ("chem", "甲醇的闪点是多少？"),
]


def main():
    from fastapi.testclient import TestClient
    import api.main as m

    rows = []
    with TestClient(m.app) as c:
        c.get("/health")
        # 预热：首次请求含 BGE 冷加载，单独丢弃
        c.post("/chat", json={"message": "预热：动火票有效期", "user_id": "warm"})
        for tag, q in CASES:
            t = time.perf_counter()
            r = c.post("/chat", json={"message": q, "user_id": f"split-{tag}"})
            wall = (time.perf_counter() - t) * 1000
            b = r.json() if r.status_code == 200 else {}
            lat = float(b.get("latency_ms") or 0)
            tot = float(b.get("total_ms") or 0)
            rows.append({
                "case": tag, "wall_ms": round(wall, 1), "total_ms": round(tot, 1),
                "agent_ms": round(lat, 1),
                "overhead_ms": round(max(0.0, tot - lat), 1),
                "chars": len(b.get("response") or ""),
                "citations": len(b.get("citations") or []),
                "intent": b.get("intent"), "code": r.status_code,
            })
            print(f"[{tag:9s}] wall={wall:6.0f} total={tot:6.0f} agent={lat:6.0f} "
                  f"非生成={max(0.0, tot - lat):5.0f} {rows[-1]['chars']}字", flush=True)

    ok = [r for r in rows if r["total_ms"] > 0]
    print("\n=== 延迟分账 ===")
    summary = {}
    if ok:
        tot = [r["total_ms"] for r in ok]
        ovh = [r["overhead_ms"] for r in ok]
        summary = {
            "n": len(ok),
            "total_p50": round(statistics.median(tot), 0),
            "total_max": round(max(tot), 0),
            "overhead_p50": round(statistics.median(ovh), 0),
            "overhead_ratio": round(sum(ovh) / sum(tot), 3),
        }
        print(f"  全链路 p50={summary['total_p50']:.0f}ms，max={summary['total_max']:.0f}ms")
        print(f"  非生成开销 p50={summary['overhead_p50']:.0f}ms，"
              f"占比 {summary['overhead_ratio']:.0%}")
        print(f"  决策：{'先做工程优化' if summary['overhead_ratio'] >= 0.4 else '瓶颈在生成侧'}")

    out = REPO / "benchmarks" / "results" / f"stage_split_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\nwritten {out}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
