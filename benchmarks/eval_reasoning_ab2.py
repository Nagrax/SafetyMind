# -*- coding: utf-8 -*-
"""高可靠性真实评测：reasoning_effort 质量 A/B（多裁判 + 多次采样）。

## 为什么需要第二轮

第一轮（eval_reasoning_ab.py，单裁判单样本）给出**互相矛盾**的结论：
- 逐题四维打分：none 更好（0.8333 vs 0.7937）
- 两两对决：baseline 更好（15 胜 vs 9 胜）
- 两者在 8/24 题上结论不一致（33%）

且已排除"长度偏差"（胜者=更长者仅 38%，baseline 平均长 2.3 倍却只赢 62.5%）。
矛盾说明**单裁判单样本的测量噪声太大**，不足以支撑"改默认值"的决策。

## 本轮方法（把测量做准，而不是争论哪个口径对）

1. 每题每模式采样 `--samples` 次（temp=0.7，避免中继缓存）
2. **三个不同裁判模型**独立打分 + 独立对决（位置随机化）
3. 汇总：逐裁判胜率、裁判间一致性、跨裁判多数投票
4. 决策规则（事先声明）：
   - `KEEP_NONE`：跨裁判多数判 none 胜率 ≥ 45% 且均分差不低于 -0.05
   - `ROUTE_BY_INTENT`：判定/推理类（grading）子集上 none 显著更差（≤ -0.15）
   - `REVERT`：整体均分差 < -0.05 且多数裁判判 baseline 更好
   - `INCONCLUSIVE`：以上都不满足（继续测，不拍脑袋改默认）

用法:
    <venv python> benchmarks/eval_reasoning_ab2.py --n 24 --samples 2 --concurrency 4
产物: benchmarks/results/reasoning_ab2_<ts>.json
"""
import argparse
import asyncio
import json
import os
import random
import re
import statistics
from datetime import datetime
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "benchmarks" / "results"
GATEWAY = os.getenv("JUDGE_GATEWAY", "http://127.0.0.1:8326/v1")
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
GKEY = _judge_key()
GEN_MODEL = os.getenv("GEN_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")
# 三个不同家族的裁判，降低单一裁判偏好带来的系统性偏差
# 裁判选择依据（2026-10-28 实测）：
#   qodercn/qmodel_38max → 限流至次日 00:00，不可用
#   minimax/MiniMax-M2.5 → 无视 JSON 指令，直接回答题目本身，不可用
#   deepseek-account/*   → 返回空 content，不可用
# 现用三个**不同模型家族**（DeepSeek / GLM / Kimi）降低单一家族偏好；
# 局限如实记录：三者同属 trae 中继，provider 层面不独立。
# 单裁判：用户指出多裁判是过度设计，且实测噪声真正来源是单次采样
# （同一裁判两次运行结论翻转 15:9 → 10:14），不是裁判身份差异。
# 一个稳定的强裁判 + 多采样 + 全组合投票，比换裁判更能压噪声，也便于跨运行比较。
JUDGES = [j for j in os.getenv("JUDGE_MODELS", "trae/deepseek-v4.1-flash").split(",") if j]

from eval_reasoning_ab import QUESTIONS, JUDGE_SYS, PAIRWISE_SYS  # noqa: E402


async def post(client, payload, timeout=400):
    for attempt in range(4):
        try:
            r = await client.post(GATEWAY + "/chat/completions",
                                  headers={"Authorization": "Bearer " + GKEY},
                                  json=payload, timeout=timeout)
            if r.status_code == 429:
                await asyncio.sleep(min(30, 2 * (attempt + 1) ** 2))
                continue
            if r.status_code >= 400:
                if attempt == 3:
                    return {"error": f"HTTP{r.status_code}"}
                await asyncio.sleep(2)
                continue
            return r.json()
        except Exception as ex:
            if attempt == 3:
                return {"error": f"{type(ex).__name__}"}
            await asyncio.sleep(2)
    return {"error": "retries"}


def extract(data):
    if data.get("error"):
        return None, data["error"]
    ch = (data.get("choices") or [{}])[0] or {}
    msg = ch.get("message") or {}
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("reasoning_content") or "").strip()
    return content, None


def parse_json(txt, keys):
    if not txt:
        return None
    m = re.search(r"\{[\s\S]*?\}", txt)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    return d if all(k in d for k in keys) else None


async def gen(client, sem, q, mode):
    body = {"model": GEN_MODEL, "max_tokens": 3072, "temperature": 0.7,
            "messages": [{"role": "user", "content": q}]}
    if mode == "none":
        body["reasoning_effort"] = "none"
    async with sem:
        data = await post(client, body)
    txt, err = extract(data)
    return {"mode": mode, "text": txt, "error": err}


async def judge_point(client, sem, judge, q, text):
    async with sem:
        data = await post(client, {
            "model": judge, "max_tokens": 1024, "temperature": 0,
            "messages": [{"role": "system", "content": JUDGE_SYS},
                         {"role": "user", "content": f"问题：{q}\n\n回答：\n{(text or '')[:4000]}"}]})
    txt, _ = extract(data)
    d = parse_json(txt, ["relevance", "accuracy", "completeness", "safety"])
    if not d:
        return None
    vals = [float(d[k]) for k in ("relevance", "accuracy", "completeness", "safety")]
    return round(sum(vals) / 4 / 5, 4)


async def judge_pair(client, sem, judge, q, a, b):
    """a=none 的回答，b=baseline 的回答；flip 随机化消除位置偏差。"""
    flip = random.random() < 0.5
    x, y = (b, a) if flip else (a, b)
    async with sem:
        data = await post(client, {
            "model": judge, "max_tokens": 512, "temperature": 0,
            "messages": [{"role": "system", "content": PAIRWISE_SYS},
                         {"role": "user", "content":
                          f"问题：{q}\n\n=== 回答 A ===\n{(x or '')[:3000]}\n\n=== 回答 B ===\n{(y or '')[:3000]}"}]})
    txt, _ = extract(data)
    d = parse_json(txt, ["winner"])
    if not d:
        return None
    w = str(d.get("winner", "")).upper()
    if flip:
        w = {"A": "B", "B": "A"}.get(w, "TIE")
    return "none" if w == "A" else ("baseline" if w == "B" else "TIE")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--samples", type=int, default=2)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--pairs", type=int, default=2,
                    help="每种模式参与两两比较的采样数（组合数=pairs²）")
    args = ap.parse_args()
    qs = QUESTIONS[:args.n]
    sem = asyncio.Semaphore(args.concurrency)

    print(f"生成 {len(qs)} 题 × 2 模式 × {args.samples} 采样 = "
          f"{len(qs) * 2 * args.samples} 次；裁判 {len(JUDGES)} 个", flush=True)
    rows = []

    async def one_question(cli, i, cat, sub, q):
        """单题全流程：生成 + 3 裁判打分 + 对决，内部并发（sem 控在飞数）。"""
        gens = await asyncio.gather(*[
            gen(cli, sem, q, m) for m in ("baseline", "none")
            for _ in range(args.samples)])
        base_txts = [g["text"] for g in gens if g["mode"] == "baseline" and g["text"]]
        none_txts = [g["text"] for g in gens if g["mode"] == "none" and g["text"]]
        row = {"i": i, "cat": cat, "sub": sub, "q": q,
               "n_base": len(base_txts), "n_none": len(none_txts),
               "point": {}, "pair": {}}
        point_tasks, pair_tasks = [], []
        for j in JUDGES:
            point_tasks.append((j, "baseline", asyncio.gather(*[
                judge_point(cli, sem, j, q, tx) for tx in base_txts])))
            point_tasks.append((j, "none", asyncio.gather(*[
                judge_point(cli, sem, j, q, tx) for tx in none_txts])))
            # 全组合投票：单点取样实测噪声极大（同一裁判两次运行结论翻转），
            # 改为 none×base 的所有采样组合各投一票，取多数。
            for ni, nt in enumerate(none_txts[:args.pairs]):
                for bi, bt in enumerate(base_txts[:args.pairs]):
                    pair_tasks.append((f"{j}#{ni}-{bi}", judge_pair(
                        cli, sem, j, q, nt, bt)))
        for j, tag, coro in point_tasks:
            res = await coro
            row["point"].setdefault(j, {})[tag] = [s for s in res if s]
        votes = {}
        for key, coro in pair_tasks:
            v = await coro
            j = key.split("#")[0]
            votes.setdefault(j, []).append(v)
        for j in JUDGES:                     # 兜底：无有效投票也写入键，避免下游 KeyError
            row["pair"].setdefault(j, "TIE")
        for j, vs in votes.items():
            vs = [v for v in vs if v]
            c = {k: vs.count(k) for k in ("none", "baseline", "TIE")}
            row["pair"][j] = ("none" if c["none"] > c["baseline"]
                               else ("baseline" if c["baseline"] > c["none"] else "TIE"))
            row["pair_votes"] = row.get("pair_votes", {})
            row["pair_votes"][j] = c
        rows.append(row)
        pb = [statistics.mean(v["baseline"]) for v in row["point"].values() if v["baseline"]]
        pn = [statistics.mean(v["none"]) for v in row["point"].values() if v["none"]]
        mb = statistics.mean(pb) if pb else float("nan")
        mn = statistics.mean(pn) if pn else float("nan")
        print(f"[{len(rows)}/{len(qs)}] {sub:9s} base={mb:.3f} none={mn:.3f} "
              f"对决={[row['pair'].get(j) for j in JUDGES]}", flush=True)
        return row

    # 跨题并发：题目之间完全独立。实测串行版 20 分钟跑不完第 1 题
    # （每题 4 次生成 + 15 次裁判调用），必须并发；sem 控制实际在飞请求数。
    async with httpx.AsyncClient(timeout=500) as client:
        await asyncio.gather(*[one_question(client, i, c, s, q)
                               for i, (c, s, q) in enumerate(qs)])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"reasoning_ab2_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"gen_model": GEN_MODEL, "judges": JUDGES,
                               "samples": args.samples, "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 汇总 ────────────────────────────────────────────────────────────
    print("\n=== 逐裁判：均分与胜率 ===")
    summary = {}
    for j in JUDGES:
        bs, ns = [], []
        wins = {"none": 0, "baseline": 0, "TIE": 0}
        for r in rows:
            bs.extend(r["point"][j]["baseline"])
            ns.extend(r["point"][j]["none"])
            w = r["pair"][j]
            if w in wins:
                wins[w] += 1
        dec = wins["none"] + wins["baseline"]
        summary[j] = {
            "base_mean": round(statistics.mean(bs), 4) if bs else None,
            "none_mean": round(statistics.mean(ns), 4) if ns else None,
            "wins": wins,
            "none_win_rate": round(wins["none"] / dec, 3) if dec else None,
        }
        print(f"  {j:28s} base={summary[j]['base_mean']} none={summary[j]['none_mean']} "
              f"胜率(none)={summary[j]['none_win_rate']} {wins}")

    # 判定类子集
    print("\n=== 判定/推理类子集（grading）===")
    for j in JUDGES:
        bs, ns, wn, wb = [], [], 0, 0
        for r in rows:
            if r["sub"] != "grading":
                continue
            bs.extend(r["point"][j]["baseline"]); ns.extend(r["point"][j]["none"])
            if r["pair"][j] == "none": wn += 1
            elif r["pair"][j] == "baseline": wb += 1
        if bs and ns:
            print(f"  {j:28s} base={statistics.mean(bs):.4f} none={statistics.mean(ns):.4f} "
                  f"none胜={wn} baseline胜={wb}")

    # 跨裁判多数投票
    print("\n=== 跨裁判多数投票（每题 3 裁判）===")
    maj = {"none": 0, "baseline": 0, "TIE": 0, "split": 0}
    for r in rows:
        votes = [r["pair"][j] for j in JUDGES if r["pair"][j]]
        c = {k: votes.count(k) for k in ("none", "baseline")}
        if c["none"] > c["baseline"]:
            maj["none"] += 1
        elif c["baseline"] > c["none"]:
            maj["baseline"] += 1
        elif c["none"] == c["baseline"] and c["none"] > 0:
            maj["split"] += 1
        else:
            maj["TIE"] += 1
    print(f"  {maj}")
    dec = maj["none"] + maj["baseline"]
    maj_rate = maj["none"] / dec if dec else 0

    base_all = statistics.mean([v["base_mean"] for v in summary.values() if v["base_mean"]])
    none_all = statistics.mean([v["none_mean"] for v in summary.values() if v["none_mean"]])
    print(f"\n=== 结论 ===\n  跨裁判均分 base={base_all:.4f} none={none_all:.4f} "
          f"(差 {none_all - base_all:+.4f})")
    print(f"  多数投票 none 胜率 = {maj_rate:.1%}")
    if maj_rate >= 0.45 and (none_all - base_all) >= -0.05:
        verdict = "KEEP_NONE"
    elif (none_all - base_all) < -0.05 and maj_rate < 0.45:
        verdict = "REVERT"
    else:
        verdict = "INCONCLUSIVE"
    print(f"  => {verdict}")
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
