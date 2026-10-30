# -*- coding: utf-8 -*-
"""真实评测：知识库扩容（7 篇 → 7 + 1698 条真实隐患检查数据）对召回的影响。

## 为什么

闭环早前测到：24 条通用安全问答里 **38% 的题在 7 篇演示库里没有任何相关片段**，
候选池 oracle@2 只有 35.7%——这是召回/覆盖的结构性上限，不是排序问题。
仓库里已有 `data/knowledge_seed/xlsx_docs.json`（1698 条用户自有隐患检查数据），
问题变成：**把它们灌进知识库，能救回多少召回？**

## 方法

- 两个**隔离**库（都建在 scratch 下，绝不碰桌面端 data/kb_chroma）：
  - `demo`：仅 7 篇默认规章
  - `full`：7 篇默认规章 + 1698 条隐患检查数据
- 18 条问题：12 条通用安全问答（既有 QUESTIONS）+ 6 条隐患/依据类
- 每条取 top-8 候选，交同一裁判（关推理）逐片段判相关
- 指标：池内相关片段数、precision@2、oracle@2、无解题占比

## 决策规则（先定再看数据）

- 通用问答 oracle@2 提升 ≥ 10pp → 扩容值得默认开启（并给导入脚本）
- 通用问答提升 < 5pp 而隐患类提升明显 → 只对"依据/检查"类问题启用扩展库
- 两类都没有提升 → 记录"扩容不解决通用覆盖"，转向真实制度库（外部条件）

用法: <venv python> benchmarks/eval_kb_expansion.py --n-general 12
产物: benchmarks/results/kb_expansion_<ts>.json
"""
import argparse
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
SEED = REPO / "data" / "knowledge_seed" / "xlsx_docs.json"

GATEWAY = os.getenv("JUDGE_GATEWAY", "http://127.0.0.1:8326/v1")
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
GKEY = _judge_key()
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")

from eval_reasoning_ab import QUESTIONS  # noqa: E402

INSPECTION_QS = [
    "油库区储罐未设置静电接地，按什么规范判定隐患？",
    "车间疏散通道宽度不足，依据哪条标准判定？",
    "甲类厂房与明火点的防火间距要求是多少，依据哪本规范？",
    "高处作业平台缺少防护栏杆，属于什么隐患级别？",
    "液化石油气瓶库房与明火间距要求是多少？",
    "配电室的门应向哪个方向开启，依据哪条规范？",
]

SYS = ("你是检索质量评审员。给定问题与若干编号资料片段，判断**每个片段是否与问题直接相关**"
       "（能用于回答该问题）。不要评价片段本身写得好不好。\n"
       "只输出 JSON：{\"rel\":{\"1\":0或1,...}}，键覆盖每一个给定片段编号。")


async def judge(client, q, blocks):
    body = "\n\n".join(f"片段 {k+1}:\n{str(b)[:600]}" for k, b in enumerate(blocks))
    for _ in range(3):
        try:
            r = await client.post(GATEWAY + "/chat/completions",
                                  headers={"Authorization": "Bearer " + GKEY},
                                  json={"model": JUDGE_MODEL, "max_tokens": 900, "temperature": 0,
                                        "reasoning_effort": "none",
                                        "messages": [{"role": "system", "content": SYS},
                                                     {"role": "user",
                                                      "content": f"问题：{q}\n\n{body}"}]},
                                  timeout=300)
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            m = re.search(r"\{[\s\S]*\}", txt)
            if not m:
                continue
            d = json.loads(m.group(0))
            rel = d.get("rel") or d
            out = []
            for i in range(1, len(blocks) + 1):
                v = rel.get(str(i), rel.get(i))
                if v is None:
                    return None
                out.append(1 if float(v) >= 0.5 else 0)
            return out
        except Exception:
            await asyncio.sleep(2)
    return None


def build_kb(name: str, with_seed: bool, seed_limit: int = 0):
    from mcp.knowledge_base import KnowledgeBase
    path = ISO / name / "kb_chroma"
    path.mkdir(parents=True, exist_ok=True)
    kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=9, chroma_path=str(path))
    if with_seed:
        docs = json.loads(SEED.read_text(encoding="utf-8"))
        if seed_limit > 0:
            docs = docs[:seed_limit]
        payload = [{"title": str(d.get("title") or ""), "content": str(d.get("content") or "")}
                   for d in docs if str(d.get("content") or "").strip()]
        t = time.perf_counter()
        added = kb.add_documents(payload)
        print(f"  [{name}] 灌入 {added} 条（{time.perf_counter()-t:.0f}s，"
              f"库内子块 {kb._collection.count()}）", flush=True)
    return kb


async def run_kb(kb, questions):
    rows = []
    async with httpx.AsyncClient(timeout=300) as client:
        for tag, q in questions:
            res = await kb.search_async(q, top_k=8)
            items = [r for r in res if isinstance(r, dict)]
            texts = [str(it.get("snippet") or it.get("content") or "") for it in items]
            if not items:
                rows.append({"tag": tag, "q": q, "n": 0, "rel": None, "pool": 0,
                             "p2": 0.0, "oracle": 0.0, "titles": []})
                print(f"  [{tag}] 无命中 {q[:22]}", flush=True)
                continue
            rel = await judge(client, q, texts)
            if rel is None:
                rows.append({"tag": tag, "q": q, "n": len(items), "rel": None,
                             "pool": 0, "p2": 0.0, "oracle": 0.0,
                             "titles": [str(it.get("title"))[:30] for it in items]})
                print(f"  [{tag}] 裁判失败 {q[:22]}", flush=True)
                continue
            pool = sum(rel)
            p2 = sum(rel[:2]) / 2
            oracle = min(pool, 2) / 2
            rows.append({"tag": tag, "q": q, "n": len(items), "rel": rel, "pool": pool,
                         "p2": round(p2, 3), "oracle": round(oracle, 3),
                         "titles": [str(it.get("title"))[:30] for it in items]})
            print(f"  [{tag}] n={len(items)} 池内相关={pool} p@2={p2:.2f} "
                  f"oracle={oracle:.2f} {q[:20]}", flush=True)
    return rows


def summarize(rows):
    ok = [r for r in rows if r["rel"] is not None]
    if not ok:
        return {"n": 0}
    return {
        "n": len(ok),
        "pool_mean": round(statistics.mean([r["pool"] for r in ok]), 2),
        "p2": round(statistics.mean([r["p2"] for r in ok]), 3),
        "oracle": round(statistics.mean([r["oracle"] for r in ok]), 3),
        "zero": sum(1 for r in ok if r["pool"] == 0),
        "zero_ratio": round(sum(1 for r in ok if r["pool"] == 0) / len(ok), 3),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-general", type=int, default=12)
    ap.add_argument("--seed-limit", type=int, default=0,
                    help="只用前 N 条隐患数据（本机 bge 嵌入约 1s/条，全量 1698 要 ~28 分钟，"
                         "子集实验先回答'扩容有没有用'这个问题）")
    args = ap.parse_args()

    questions = ([("general", q) for _, _, q in QUESTIONS[:args.n_general]]
                 + [("inspection", q) for q in INSPECTION_QS])

    os.environ.setdefault("SAFETYMIND_EMBEDDING", "bge")
    print("构建隔离库…", flush=True)
    demo = build_kb("demo", with_seed=False)
    print(f"  [demo] 子块 {demo._collection.count()}", flush=True)
    full = build_kb("full", with_seed=True, seed_limit=args.seed_limit)

    print("\n=== demo（7 篇）===", flush=True)
    demo_rows = await run_kb(demo, questions)
    print("\n=== full（+1698 条隐患数据）===", flush=True)
    full_rows = await run_kb(full, questions)

    print("\n=== 扩容对比 ===")
    out = {}
    for name, rows in (("demo", demo_rows), ("full", full_rows)):
        for tag in ("general", "inspection"):
            s = summarize([r for r in rows if r["tag"] == tag])
            out[f"{name}_{tag}"] = s
            print(f"  {name:5s} {tag:10s} {s}")

    dg = out.get("demo_general", {})
    fg = out.get("full_general", {})
    di = out.get("demo_inspection", {})
    fi = out.get("full_inspection", {})
    verdict = "keep_demo"
    if dg.get("n") and fg.get("n"):
        lift_g = fg["oracle"] - dg["oracle"]
        lift_i = (fi.get("oracle", 0) - di.get("oracle", 0)) if di.get("n") else 0
        print(f"  通用问答 oracle 提升 {lift_g:+.3f}；隐患类 oracle 提升 {lift_i:+.3f}")
        if lift_g >= 0.10:
            verdict = "adopt_default"
        elif lift_i >= 0.10:
            verdict = "adopt_for_inspection_only"
        print(f"  决策：{verdict}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"kb_expansion_{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps({"verdict": verdict, "summary": out,
                             "demo_rows": demo_rows, "full_rows": full_rows},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {p}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    asyncio.run(main())
