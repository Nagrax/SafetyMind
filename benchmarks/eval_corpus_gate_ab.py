# -*- coding: utf-8 -*-
"""真实评测：corpus 注入闸门开/关的**答案质量 A/B**（不是只看引用精度）。

## 为什么要 A/B

闸门把 corpus 引用从 126 条砍到 3 条（实测 0 条相关）。但"引用被裁判判为无关"
不等于"答案没有用到它"——裁判可能错判，也可能某条弱相关片段恰好补上了答案细节。
所以落地前必须验证**答案质量没有下降**。

## 方法

- 12 条真实问题（fact / grading / emergency / writing 四类各取若干），走真生产链路 POST /chat
- 两种口径：`SAFETYMIND_CORPUS_MIN_TERM_HITS=2`（闸门开，新默认）vs `=0`（闸门关，旧行为）
- 关闭回复缓存（SAFETYMIND_RESPONSE_CACHE=0），否则第二口径会命中缓存
- 同一裁判按四维 1-5 打分（准确性 / 依据 / 具体可执行 / 安全边界），逐题配对比较

## 决策规则（先定规则再看数据）

- 闸门开的后三位均值（准确性/依据/安全边界）任一项下降 ≥ 0.3 分 → 回滚闸门
- 均值差 < 0.3 分 → 保留闸门（同时披露样本量小）

用法: <venv python> benchmarks/eval_corpus_gate_ab.py --n 12
产物: benchmarks/results/corpus_gate_ab_<ts>.json
"""
import argparse
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
GATEWAY = os.getenv("JUDGE_GATEWAY", "http://127.0.0.1:8326/v1")
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
GKEY = _judge_key()
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")

from eval_reasoning_ab import QUESTIONS  # noqa: E402

SYS = ("你是安全生产领域的答案评审员。给定问题与答案，按四个维度各打 1-5 分：\n"
       "accuracy=事实/数值/条款是否准确；evidence=是否给出可核对的依据或来源；\n"
       "concrete=是否给出可执行的具体步骤或数值；safety=不确定时是否提示核实而非编造。\n"
       "只输出 JSON：{\"accuracy\":n,\"evidence\":n,\"concrete\":n,\"safety\":n,\"note\":\"一句话\"}")

DIMS = ("accuracy", "evidence", "concrete", "safety")


def judge(client, q, ans):
    payload = {"model": JUDGE_MODEL, "max_tokens": 900, "temperature": 0,
               "messages": [{"role": "system", "content": SYS},
                            {"role": "user", "content": f"问题：{q}\n\n答案：{ans[:4000]}"}]}
    for attempt in range(3):
        try:
            r = client.post(GATEWAY + "/chat/completions",
                            headers={"Authorization": "Bearer " + GKEY},
                            json=payload, timeout=300)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1) ** 2)
                continue
            if r.status_code >= 400:
                time.sleep(2)
                continue
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            m = re.search(r"\{[\s\S]*\}", txt)
            if not m:
                continue
            d = json.loads(m.group(0))
            return {k: float(d.get(k) or 0) for k in DIMS} | {"note": str(d.get("note", ""))[:120]}
        except Exception:
            time.sleep(2)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--reuse", type=str, default="",
                    help="复用既有产物里的答案（只重跑裁判，不再发 20 分钟对话请求）")
    ap.add_argument("--samples", type=int, default=2,
                    help="每条答案的裁判采样次数（单次采样噪声大，默认 2 次取均值）")
    ap.add_argument("--interleave", action="store_true",
                    help="交错取样：同一进程内每题交替两口径，抵消时段漂移（加强版 A/B）")
    args = ap.parse_args()

    from fastapi.testclient import TestClient
    import api.main as m

    # 取题：n>12 时直接用前 n 题（要统计功效，就不再按子类轮转限流）
    pool = QUESTIONS[:24]
    picked, seen = [], {}
    for q in pool:
        cat, sub, text = q
        if args.n <= 12 and seen.get(sub, 0) >= 3:
            continue
        seen[sub] = seen.get(sub, 0) + 1
        picked.append((sub, text))
        if len(picked) >= args.n:
            break

    rows = []
    if args.reuse:
        rows = json.loads(Path(args.reuse).read_text(encoding="utf-8"))["rows"]
        for r in rows:
            r.pop("score", None)
        print(f"复用 {args.reuse} 的 {len(rows)} 条答案，只重跑裁判", flush=True)
    with httpx.Client(timeout=300) as jc:
        if not args.reuse:
            if args.interleave:
                # 交错取样：同一进程内每题交替 gate=2 / gate=0（顺序按题号奇偶翻转），
                # 把"时段/端点负载漂移"从两口径的差里抵掉（上一版分时段各跑一遍的
                # 混淆就是靠这个消除的）。
                with TestClient(m.app) as c:
                    c.get("/health")
                    for i, (sub, q) in enumerate(picked):
                        order = ("2", "0") if i % 2 == 0 else ("0", "2")
                        for gate in order:
                            os.environ["SAFETYMIND_CORPUS_MIN_TERM_HITS"] = gate
                            t = time.perf_counter()
                            r = c.post("/chat", json={"message": q,
                                                       "user_id": f"ab-i-{gate}-{i}",
                                                       "conv_id": f"ab-i-{gate}-{i}"})
                            ms = (time.perf_counter() - t) * 1000
                            ans = (r.json() or {}).get("response", "") if r.status_code == 200 else ""
                            rows.append({"gate": gate, "i": i, "sub": sub, "q": q,
                                         "ans": ans, "ms": round(ms, 1),
                                         "status": r.status_code, "order": list(order)})
                            print(f"[i={i+1} gate={gate}] {sub:9s} {ms:.0f}ms {len(ans)}字",
                                  flush=True)
            else:
                for gate in ("0", "2"):
                    os.environ["SAFETYMIND_CORPUS_MIN_TERM_HITS"] = gate
                    with TestClient(m.app) as c:
                        c.get("/health")
                        for i, (sub, q) in enumerate(picked):
                            t = time.perf_counter()
                            r = c.post("/chat", json={"message": q, "user_id": f"ab-{gate}-{i}"})
                            ms = (time.perf_counter() - t) * 1000
                            ans = (r.json() or {}).get("response", "") if r.status_code == 200 else ""
                            rows.append({"gate": gate, "i": i, "sub": sub, "q": q,
                                         "ans": ans, "ms": round(ms, 1),
                                         "status": r.status_code})
                            print(f"[gate={gate}] {i+1}/{len(picked)} {sub:9s} {ms:.0f}ms "
                                  f"{len(ans)}字", flush=True)
                    # 每个口径单独起停应用，避免库实例/缓存串味

    # 裁判单独一个 client：对话用的 client 在 with 块结束时已关闭（此前把已关闭的
    # client 传给裁判，导致 20 条全部 FAIL —— 这也是"评测脚本自身的 bug"，
    # 记在产物里避免下次重犯）
    # 双采样 + 取均值：同一裁判单次采样噪声极大（闭环早期实测同一裁判两次给出相反结论），
    # 单样本对比会得出假结论，故每题每口径跑 args.samples 次取均值。
    with httpx.Client(timeout=300) as jc2:
        for r in rows:
            if not r["ans"]:
                continue
            scores = []
            for _ in range(args.samples):
                got = judge(jc2, r["q"], r["ans"])
                for _retry in range(2):
                    if got:
                        break
                    time.sleep(3)
                    got = judge(jc2, r["q"], r["ans"])
                if got:
                    scores.append(got)
            r["scores"] = scores
            if scores:
                r["score"] = {d: round(statistics.mean([s[d] for s in scores]), 3)
                              for d in DIMS}
            print(f"  judge[{r['gate']}] {r['sub']:9s} n={len(scores)} "
                  f"{ {k: r['score'][k] for k in DIMS} if scores else 'FAIL'}", flush=True)

    on = [r for r in rows if r["gate"] == "2" and r.get("score")]
    off = [r for r in rows if r["gate"] == "0" and r.get("score")]
    print("\n=== 闸门 A/B（四维 1-5，双采样均值）===")
    res = {}
    for name, grp in (("gate_on", on), ("gate_off", off)):
        if not grp:
            continue
        res[name] = {d: round(statistics.mean([r["score"][d] for r in grp]), 3) for d in DIMS}
        res[name]["ms_p50"] = round(statistics.median([r["ms"] for r in grp]), 1)
        res[name]["n"] = len(grp)
        print(f"  {name}: {res[name]}")

    # 配对分析：只比两侧都评成功的同一题（避免"缺样本造成的假差异"）
    pairs = []
    for i in range(len(picked)):
        a = next((r for r in rows if r["i"] == i and r["gate"] == "2" and r.get("score")), None)
        b = next((r for r in rows if r["i"] == i and r["gate"] == "0" and r.get("score")), None)
        if a and b:
            pairs.append((a, b))
    if pairs:
        diff = {d: round(statistics.mean([a["score"][d] - b["score"][d] for a, b in pairs]), 3)
                for d in DIMS}
        overall_a = statistics.mean([statistics.mean([a["score"][d] for d in DIMS])
                                     for a, b in pairs])
        overall_b = statistics.mean([statistics.mean([b["score"][d] for d in DIMS])
                                     for a, b in pairs])
        wins = sum(1 for a, b in pairs
                   if statistics.mean([a["score"][d] for d in DIMS])
                   > statistics.mean([b["score"][d] for d in DIMS]))
        print(f"  配对 {len(pairs)} 题：四维差 {diff}；总分 {overall_a:.3f} vs {overall_b:.3f}；"
              f"开闸门胜 {wins}/{len(pairs)}")
        res["paired"] = {"n": len(pairs), "diff": diff,
                         "overall_on": round(overall_a, 3),
                         "overall_off": round(overall_b, 3), "wins_on": wins}
    if pairs:
        verdict = "keep"
        for d in ("accuracy", "evidence", "safety"):
            if diff[d] <= -0.3:
                verdict = "rollback"
        print(f"  决策：{'回滚闸门' if verdict == 'rollback' else '保留闸门'} "
              f"（规则：配对差里准确/依据/安全任一 ≤ -0.3 才回滚）")
    else:
        verdict = "inconclusive"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"corpus_gate_ab_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"verdict": verdict, "summary": res, "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    os.environ.setdefault("SAFETYMIND_RESPONSE_CACHE", "0")
    raise SystemExit(main())
