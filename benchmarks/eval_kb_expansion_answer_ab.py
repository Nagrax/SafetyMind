# -*- coding: utf-8 -*-
"""真实评测：知识库要不要扩容到 1707 子块（答案质量口径 A/B）。

## 为什么还要做一轮

召回实验（`eval_kb_expansion.py`）显示：全量扩容后隐患类 oracle@2 0.25→0.75、
无解题 66.7%→0%，而通用问答基本持平。但**召回指标不等于答案质量**——多注入 1698 条
检查项也可能给通用问答带来噪声（corpus 通道那次就是 0% 相关的教训）。

## 方法

- 同一批 18 题（12 通用 + 6 隐患），两臂：
  - `demo`：隔离的 9 子块库（仅 7 篇默认规章）
  - `full`：隔离的 1707 子块库（+1698 条隐患检查数据）
- 每臂单独起停应用（知识库目录由 `CHROMA_KB_PERSIST_DIRECTORY` 指定），走完整 /chat
- 控温 `SAFETYMIND_TEMPERATURE=0`、关回复缓存
- 同一裁判（关推理）双采样打四维分

## 决策规则（先定）

采纳扩容需**同时**满足：
  1. 配对 accuracy 与 safety 都不低于 −0.2
  2. 隐患类子集 accuracy 不低于现役（扩容的主要受益方不能变差）
  3. 通用问答子集 accuracy 不低于 −0.3
否则只把扩容做成"隐患类专用通道"，不设为默认。

用法: <venv python> benchmarks/eval_kb_expansion_answer_ab.py
产物: benchmarks/results/kb_expansion_ab_<ts>.json
"""
import asyncio
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "benchmarks"))
OUT_DIR = REPO / "benchmarks" / "results"
ISO = Path("D:/claudecode/scratch/sm-loop/iso/kb_exp")
GATEWAY = os.getenv("JUDGE_GATEWAY", "http://127.0.0.1:8326/v1")
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
GKEY = _judge_key()
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")

from eval_reasoning_ab import QUESTIONS  # noqa: E402
from eval_kb_expansion import INSPECTION_QS  # noqa: E402

SYS = ("你是安全生产领域的答案评审员。按四维各打 1-5 分：accuracy=事实/数值/条款准确性；"
       "evidence=可核对依据；concrete=可执行程度；safety=不确定时是否提示核实。\n"
       "只输出 JSON：{\"accuracy\":n,\"evidence\":n,\"concrete\":n,\"safety\":n}")

DIMS = ("accuracy", "evidence", "concrete", "safety")
ARMS = {
    "demo": str(ISO / "demo" / "kb_chroma"),
    "full": str(ISO / "full" / "kb_chroma"),
}


def judge(client, q, ans):
    for _ in range(3):
        try:
            r = client.post(GATEWAY + "/chat/completions",
                            headers={"Authorization": "Bearer " + GKEY},
                            json={"model": JUDGE_MODEL, "max_tokens": 800, "temperature": 0,
                                  "reasoning_effort": "none",
                                  "messages": [{"role": "system", "content": SYS},
                                               {"role": "user",
                                                "content": f"问题：{q}\n\n答案：{ans[:4000]}"}]},
                            timeout=300)
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            m = re.search(r"\{[\s\S]*\}", txt)
            if m:
                return json.loads(m.group(0))
        except Exception:
            time.sleep(2)
    return None


def run_arm(arm, path, questions):
    os.environ["CHROMA_KB_PERSIST_DIRECTORY"] = path
    os.environ["SAFETYMIND_TEMPERATURE"] = "0"
    os.environ["SAFETYMIND_RESPONSE_CACHE"] = "0"
    import importlib
    import api.main as m
    importlib.reload(m)
    rows = []
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        c.get("/health")
        for i, (tag, q) in enumerate(questions):
            t = time.perf_counter()
            r = c.post("/chat", json={"message": q, "user_id": f"kbab-{arm}-{i}"})
            ms = (time.perf_counter() - t) * 1000
            b = r.json() if r.status_code == 200 else {}
            rows.append({"arm": arm, "tag": tag, "q": q,
                         "ans": b.get("response") or "", "ms": round(ms, 1),
                         "cites": len(b.get("citations") or []), "code": r.status_code})
            print(f"[{arm:4s} {tag:10s}] {ms:6.0f}ms {len(rows[-1]['ans']):5d}字 "
                  f"cites={rows[-1]['cites']}", flush=True)
    return rows


def main():
    for name, p in ARMS.items():
        if not Path(p).exists():
            print(f"缺少隔离库 {name}: {p}（先跑 eval_kb_expansion.py）")
            return 2
    questions = ([("general", q) for _, _, q in QUESTIONS[:12]]
                 + [("inspection", q) for q in INSPECTION_QS])

    rows = []
    for arm in ("demo", "full"):
        rows.extend(run_arm(arm, ARMS[arm], questions))

    with httpx.Client(timeout=300) as jc:
        for r in rows:
            if not r["ans"]:
                continue
            got = [judge(jc, r["q"], r["ans"]) for _ in range(2)]
            got = [g for g in got if g]
            if got:
                r["score"] = {d: round(statistics.mean([float(g.get(d) or 0) for g in got]), 3)
                              for d in DIMS}
            print(f"  judge[{r['arm']:4s}] {r['tag']:10s} {r.get('score') or 'FAIL'}", flush=True)

    print("\n=== 扩容 A/B（答案质量）===")
    summary = {}
    for arm in ("demo", "full"):
        for tag in ("general", "inspection"):
            grp = [r for r in rows if r["arm"] == arm and r["tag"] == tag and r.get("score")]
            if not grp:
                continue
            summary[f"{arm}_{tag}"] = {
                "n": len(grp),
                **{d: round(statistics.mean([r["score"][d] for r in grp]), 3) for d in DIMS},
                "ms_p50": round(statistics.median([r["ms"] for r in grp]), 0),
                "cites_mean": round(statistics.mean([r["cites"] for r in grp]), 2),
            }
            print(f"  {arm:4s} {tag:10s} {summary[f'{arm}_{tag}']}")

    pairs = []
    for i, (tag, q) in enumerate(questions):
        a = next((r for r in rows if r["arm"] == "full" and r["q"] == q and r.get("score")), None)
        b = next((r for r in rows if r["arm"] == "demo" and r["q"] == q and r.get("score")), None)
        if a and b:
            pairs.append((tag, a, b))

    verdict = "insufficient"
    if pairs:
        diff = {d: round(statistics.mean([a["score"][d] - b["score"][d] for _, a, b in pairs]), 3)
                for d in DIMS}
        def sub(tag):
            ps = [(a, b) for t, a, b in pairs if t == tag]
            if not ps:
                return None
            return round(statistics.mean([a["score"]["accuracy"] - b["score"]["accuracy"]
                                          for a, b in ps]), 3)
        print(f"  配对 {len(pairs)} 题：四维差(full-demo) {diff}")
        print(f"  子集 accuracy 差：通用 {sub('general')}、隐患 {sub('inspection')}")
        ok = (diff["accuracy"] >= -0.2 and diff["safety"] >= -0.2
              and (sub("inspection") or 0) >= 0 and (sub("general") or 0) >= -0.3)
        verdict = "adopt_full" if ok else "inspection_only"
        print(f"  决策：{verdict}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"kb_expansion_ab_{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps({"verdict": verdict, "summary": summary, "rows": rows},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {p}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
