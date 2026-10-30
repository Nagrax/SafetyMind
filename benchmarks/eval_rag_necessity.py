# -*- coding: utf-8 -*-
"""三个关键问题的对照实验（2026-10-26 用户提出的产品级疑问）

Q1 意图识别改用大模型会不会更好？
   测：bge 原型 vs 强模型 few-shot 的准确率差是否值得那 2 秒

Q2 RAG 是否还有必要？现在模型能力是否足以覆盖？
   两种配置 x 六个真实安全管理工作任务：
     A  纯模型（无 RAG）
     B  RAG（知识库检索注入）
   判据：是否引用依据、是否诚实说明缺口、是否有"无依据的精确数值"（最危险信号）、延迟

纪律：每格 n>=3 报中位数；质量判据机械可证伪；原始输出落盘可复核。

用法:
    <venv python> benchmarks/eval_rag_necessity.py --which all --repeats 3
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

OUT_DIR = REPO / "benchmarks" / "results"
GATEWAY = "http://127.0.0.1:8326/v1"
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
GKEY = _judge_key()
WORK_TASKS = [
    ("查规程", "动火作业票的有效期和审批层级是怎么规定的？"),
    ("辨等级", "反应釜安全阀超期未校验，这属于一般隐患还是重大隐患？依据是什么？"),
    ("写台账", "帮我写一条隐患排查记录，内容是一号库房灭火器压力表在红区。"),
    ("应急处置", "车间闻到刺鼻气味，怀疑是氨泄漏，第一步该做什么？"),
    ("查化学品", "甲醇的闪点和爆炸极限是多少？储存要注意什么？"),
    ("法规查询", "安全生产法规定的主要负责人七项职责是什么？"),
]

SYS_PLAIN = (
    "你是安全生产助手。回答专业、可执行、简洁。"
    "涉及具体数值时，若你不确定就明确说不确定，不要编造。"
)
SYS_WITH_KB = (
    "你是安全生产助手。下面给你企业内部知识库检索到的资料，"
    "优先依据这些资料回答，并注明依据编号。"
    "资料没覆盖的内容可以补充通用知识，但必须明确说明这是非库内资料。"
)


def grade(text, has_kb):
    t = text or ""
    return {
        "len": len(t),
        "empty": len(t.strip()) < 20,
        "has_structure": any(k in t for k in ("\n-", "\n1", "**", "##")),
        "cites_basis": bool(re.search(r"GB\s*\d{4,5}|第[一二三四五六七八九十]+条|《[^》]+》", t)),
        "cites_kb": bool(re.search(r"依据\s*\d|&【依据|库内", t)) if has_kb else False,
        "honest_gap": any(k in t for k in ("库内", "未收录", "暂无", "需核实", "以官方", "非库内")),
        "risky_number": bool(re.search(r"\d+\s*(℃|mg/m3|小时|天|%)", t))
                        and not re.search(r"GB\s*\d{4,5}|第[一二三四五六七八九十]+条|依据", t),
    }


async def call(client, model, system, user, max_tokens=900, sem=None):
    async def _inner():
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                r = await client.post(
                    GATEWAY + "/chat/completions",
                    headers={"Authorization": "Bearer " + GKEY},
                    json={"model": model, "max_tokens": max_tokens, "temperature": 0.0,
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": user}]})
                if r.status_code != 200:
                    if attempt == 2:
                        return {"error": "HTTP" + str(r.status_code)}
                    await asyncio.sleep(2)
                    continue
                d = r.json()
                ch = (d.get("choices") or [{}])[0].get("message") or {}
                txt = (ch.get("content") or "").strip()
                u = d.get("usage") or {}
                if not txt and attempt < 2:
                    await asyncio.sleep(1)
                    continue
                return {"text": txt, "in_tok": u.get("prompt_tokens"),
                        "out_tok": u.get("completion_tokens"),
                        "ms": (time.monotonic() - t0) * 1000}
            except Exception as e:
                if attempt == 2:
                    return {"error": type(e).__name__}
                await asyncio.sleep(2)
    if sem is not None:
        async with sem:
            return await _inner()
    return await _inner()


async def q2_rag_necessity(model, repeats):
    import httpx

    kb_hits = {}
    try:
        iso = Path("D:/claudecode/scratch/sm-loop/iso/rag_eval")
        iso.mkdir(parents=True, exist_ok=True)
        from mcp.knowledge_base import KnowledgeBase
        kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=9,
                           chroma_path=str(iso / "kb"))
        for tag, q in WORK_TASKS:
            try:
                hits = await kb.search_async(q, top_k=3)
                kb_hits[tag] = [{"title": h.get("title"),
                                 "content": (h.get("content") or "")[:600]}
                                for h in (hits or [])]
            except Exception as ex:
                kb_hits[tag] = [{"error": type(ex).__name__ + ": " + str(ex)[:100]}]
    except Exception as ex:
        print("[warn] 知识库检索不可用: " + str(ex)[:200], flush=True)

    sem = asyncio.Semaphore(4)
    rows = []
    async with httpx.AsyncClient(timeout=180.0) as client:
        for tag, q in WORK_TASKS:
            hits = kb_hits.get(tag) or []
            kb_text = "\n\n".join(
                "【依据" + str(i + 1) + "】" + (h.get("title") or "") + "\n" + (h.get("content") or "")
                for i, h in enumerate(hits) if h.get("content"))
            has_kb = bool(kb_text)
            for rep in range(repeats):
                a = await call(client, model, SYS_PLAIN, q, sem=sem)
                b_user = ("参考资料：\n" + kb_text + "\n\n问题：" + q) if has_kb else q
                b = await call(client, model, SYS_WITH_KB if has_kb else SYS_PLAIN,
                               b_user, sem=sem)
                for path, r in (("A_plain", a), ("B_rag", b)):
                    g = grade(r.get("text", ""), has_kb)
                    rows.append({"task": tag, "q": q, "path": path, "rep": rep,
                                 "ms": round(r.get("ms", 0), 1),
                                 "in_tok": r.get("in_tok"), "out_tok": r.get("out_tok"),
                                 "error": r.get("error"), "kb_hit_count": len(hits),
                                 "text": (r.get("text") or "")[:900], **g})
            print("[" + tag + "] 完成（KB 命中 " + str(len(hits)) + " 条）", flush=True)

    def agg(path):
        sub = [r for r in rows if r["path"] == path and not r.get("error")]
        if not sub:
            return {}
        toks = [r["in_tok"] for r in sub if r["in_tok"]]
        return {
            "n": len(sub),
            "latency_median_ms": round(statistics.median([r["ms"] for r in sub]), 1),
            "len_median": round(statistics.median([r["len"] for r in sub]), 1),
            "cites_basis": sum(1 for r in sub if r["cites_basis"]),
            "honest_gap": sum(1 for r in sub if r["honest_gap"]),
            "risky_number": sum(1 for r in sub if r["risky_number"]),
            "empty": sum(1 for r in sub if r["empty"]),
            "in_tok_median": round(statistics.median(toks), 0) if toks else None,
        }

    return {"model": model, "repeats": repeats, "kb": kb_hits,
            "summary": {"A_plain": agg("A_plain"), "B_rag": agg("B_rag")}, "rows": rows}


def q1_intent_llm():
    res_dir = REPO / "benchmarks" / "intent" / "results"
    out = {"note": "从已有评测产物汇总，避免重复调用", "entries": []}
    for f in sorted(res_dir.glob("intent_fewshot_fair_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for s in d.get("schemes", []):
            out["entries"].append({"file": f.name, "scheme": s["scheme"],
                                   "accuracy": s.get("accuracy"),
                                   "prompt_tokens": s.get("prompt_tokens_mean"),
                                   "p50_ms": s.get("latency_p50_ms"), "n": s.get("n")})
    for f in sorted(res_dir.glob("strong_llm_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        out["entries"].append({"file": f.name,
                               "scheme": "P1_" + str(d.get("model")),
                               "accuracy": d.get("accuracy"), "prompt_tokens": None,
                               "p50_ms": d.get("latency_p50_ms"), "n": d.get("n")})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--which", default="all", choices=["q1", "q2", "all"])
    ap.add_argument("--model", default="trae/deepseek-v4.1-flash")
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    result = {}

    if args.which in ("q1", "all"):
        print("=== Q1 意图识别：bge vs 大模型 ===")
        result["q1"] = q1_intent_llm()
        for e in result["q1"]["entries"]:
            print("  " + str(e["scheme"]).ljust(28) + " acc=" + str(e["accuracy"])
                  + " tok=" + str(e["prompt_tokens"]) + " p50=" + str(e["p50_ms"]) + "ms")

    if args.which in ("q2", "all"):
        print("\n=== Q2 RAG 必要性（model=" + args.model + ", n=" + str(args.repeats) + "）===")
        result["q2"] = asyncio.run(q2_rag_necessity(args.model, args.repeats))
        for k, v in result["q2"]["summary"].items():
            print("  " + k + ": " + json.dumps(v, ensure_ascii=False))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    p = OUT_DIR / ("rag_necessity_" + stamp + ".json")
    p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwritten " + str(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
