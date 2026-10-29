# -*- coding: utf-8 -*-
"""真实评测：reasoning_effort 对答案质量的影响（A/B，LLM-as-Judge 交叉评判）。

## 为什么要做这个评测

2026-10-28 我把适配层默认改成 `reasoning_effort="none"`，依据是 4 条用例的
延迟-关键词命中对比。那**不足以支撑一个影响全部生产回答的默认值变更**：
- 关键词命中只看"有没有提到 8 小时/GB30871"，看不出论证质量与可操作性
- 4 条样本里"隐患分级"这种判断题恰好是有推理时截断（0 字），容易高估收益

因此这里做**真实评测**：
1. 24 条覆盖全部意图类别的真实安全工作问题
2. 每题用两种模式各生成一次（temp=0.7 避免中继服务端缓存）
3. 用**另一个模型**当裁判（避免自我偏好），四维打分 + 两两对决胜者
4. 输出：分数分布、胜率、延迟、token 消耗

## 决策规则（先定规则再看结果，防事后解释）

- 若 none 的**平均分不低于** baseline（差值 ≥ -0.05）且胜率 ≥ 45%
  → 维持默认 none（收益：快 2.4-4.8×，无质量损失）
- 若 none 在**判定/推理类**子集（hazard_grading / hazard_inspection）显著变差
  （该子集均分低 0.15 以上）
  → 改为**按意图路由**：事实查证类用 none，判定推理类用 low/默认

用法:
    <venv python> benchmarks/eval_reasoning_ab.py --n 24 --concurrency 4
产物: benchmarks/results/reasoning_ab_<ts>.json
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx

REPO = Path("D:/claudecode/SafetyMind")
OUT_DIR = REPO / "benchmarks" / "results"
GATEWAY = os.getenv("JUDGE_GATEWAY", "http://127.0.0.1:8326/v1")
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
GKEY = _judge_key()
GEN_MODEL = os.getenv("GEN_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "trae/deepseek-v4.1-flash")

# 24 条真实工作问题，覆盖全部意图类别；标注子集用于分组统计
QUESTIONS = [
    # 事实查证类（法规/术语/参数）——预期关推理不吃亏
    ("regulation_query", "fact", "动火作业票分几个等级？各级有效期多久？依据哪个标准？"),
    ("regulation_query", "fact", "安全生产法规定的主要负责人七项职责是什么？"),
    ("regulation_query", "fact", "受限空间作业的气体检测合格判定值分别是多少？"),
    ("chemical_safety", "fact", "乙醇的闪点、爆炸极限分别是多少？储存有什么关键要求？"),
    ("chemical_safety", "fact", "氨水泄漏后用大量水稀释可以吗？为什么？"),
    ("work_permit", "fact", "动火作业票到期没干完，可以延期吗？能延多久？"),
    ("work_permit", "fact", "受限空间作业票和动火作业票可以同时办吗？有什么约束？"),
    ("training_cert", "fact", "特种作业操作证多久复审一次？逾期未复审怎么处理？"),
    ("ppe_inquiry", "fact", "接触盐酸岗位应配哪些个体防护用品？依据什么选型？"),
    ("occupational_health", "fact", "噪声作业的职业接触限值是多少？需要做哪些职业健康检查？"),
    ("inspection_audit", "fact", "安全生产标准化一级企业评审的主要否决项有哪些？"),
    ("equipment_maintenance", "fact", "安全阀的校验周期是怎么规定的？超期有什么后果？"),
    # 判定推理类——关推理可能吃亏的关键子集
    ("hazard_inspection", "grading", "安全阀超期未校验，应判定为一般事故隐患还是重大事故隐患？请给出判定依据与理由。"),
    ("hazard_inspection", "grading", "车间同时存在：物料堵塞消防通道、应急照明失效、一名员工未戴安全帽。请分级并说明优先级排序依据。"),
    ("hazard_inspection", "grading", "某企业液氨储罐区围堰排水阀常开，是否构成重大隐患？请对照标准逐条说明。"),
    ("hazard_inspection", "grading", "动火作业现场没有监火人但已开具作业票，如何定性与处理？"),
    ("hazard_report", "grading", "员工上报：发现配电柜有焦糊味但未冒烟。请判断紧急程度并给出处置步骤。"),
    ("general_consult", "grading", "隐患排查治理双重预防机制里，风险分级管控和隐患排查治理是什么关系？"),
    ("general_consult", "grading", "新员工入职三级安全教育分别是哪三级？各自的学时要求是多少？"),
    # 撰写类——章节结构易被推理挤占
    ("safety_document", "writing", "帮我写一条隐患排查治理台账记录，包含发现时间、隐患描述、整改措施、责任人、期限。"),
    ("safety_document", "writing", "写一份受限空间作业安全交底提纲，列出必须交代的要点。"),
    # 应急类
    ("emergency_response", "emergency", "现场有人被液氨喷溅到眼睛，第一时间怎么处置？"),
    ("incident_report", "emergency", "发生一起未造成伤害的物料泄漏，需要上报吗？上报流程是什么？"),
    # 汇总类
    ("general_consult", "fact", "请把动火、受限空间、高处、吊装、临时用电、动土、断路、盲板抽堵这八类特殊作业的审批层级异同总结成一张表。"),
    ("general_consult", "fact", "安全检查发现的隐患整改要闭环，闭环记录必须包含哪些要素？"),
]

JUDGE_SYS = (
    "你是安全生产领域的资深评审员。给下面这份回答按四维打 1-5 分（严格整数，不要用 10 分制）：\n"
    "- relevance：是否切题\n"
    "- accuracy：事实/数值/法规依据是否正确（不确定就给低分，绝不奖励编造）\n"
    "- completeness：是否覆盖问题要点、是否给出可执行信息\n"
    "- safety：是否有安全误导、是否漏掉关键风险提示\n"
    "只输出 JSON：{\"relevance\":n,\"accuracy\":n,\"completeness\":n,\"safety\":n}"
)
PAIRWISE_SYS = (
    "你是安全生产领域的资深评审员。下面同一问题的两份回答 A/B，请判断哪份更好。\n"
    "评判标准优先级：①事实与法规依据正确 ②不遗漏关键安全提示 ③可执行 ④完整。\n"
    "只输出 JSON：{\"winner\":\"A|B|TIE\",\"reason\":\"不超过30字\"}"
)


async def post(client, payload, timeout=300):
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
                    return {"error": f"HTTP{r.status_code}: {r.text[:120]}"}
                await asyncio.sleep(2)
                continue
            return r.json()
        except Exception as ex:
            if attempt == 3:
                return {"error": f"{type(ex).__name__}: {str(ex)[:120]}"}
            await asyncio.sleep(2)
    return {"error": "retries exhausted"}


def extract(data):
    if data.get("error"):
        return None, data["error"], 0
    ch = (data.get("choices") or [{}])[0] or {}
    msg = ch.get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    usage = (data.get("usage") or {}).get("completion_tokens") or 0
    if not content and reasoning:
        content = reasoning
    return content, None, usage


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
    if not all(k in d for k in keys):
        return None
    return d


async def generate(client, sem, q, mode):
    body = {"model": GEN_MODEL, "max_tokens": 3072, "temperature": 0.7,
            "messages": [{"role": "user", "content": q}]}
    if mode == "none":
        body["reasoning_effort"] = "none"
    async with sem:
        t = time.perf_counter()
        data = await post(client, body)
        ms = (time.perf_counter() - t) * 1000
    text, err, tok = extract(data)
    return {"mode": mode, "ms": round(ms, 1), "text": text, "error": err, "tok": tok}


async def judge_single(client, sem, q, text):
    async with sem:
        data = await post(client, {
            "model": JUDGE_MODEL, "max_tokens": 1024, "temperature": 0,
            "messages": [{"role": "system", "content": JUDGE_SYS},
                         {"role": "user", "content": f"问题：{q}\n\n回答：\n{text[:4000]}"}]})
    txt, err, _ = extract(data)
    d = parse_json(txt, ["relevance", "accuracy", "completeness", "safety"])
    if not d:
        return {"error": err or "judge-parse", "raw": (txt or "")[:120]}
    vals = [float(d["relevance"]), float(d["accuracy"]),
            float(d["completeness"]), float(d["safety"])]
    return {"mean": round(sum(vals) / 4 / 5, 4), "scores": d}


async def judge_pair(client, sem, q, a, b):
    # 位置偏差治理：A/B 顺序随机化，记录实际顺序
    import random
    flip = random.random() < 0.5
    x, y = (b, a) if flip else (a, b)
    async with sem:
        data = await post(client, {
            "model": JUDGE_MODEL, "max_tokens": 512, "temperature": 0,
            "messages": [{"role": "system", "content": PAIRWISE_SYS},
                         {"role": "user", "content":
                          f"问题：{q}\n\n=== 回答 A ===\n{x[:3000]}\n\n=== 回答 B ===\n{y[:3000]}"}]})
    txt, err, _ = extract(data)
    d = parse_json(txt, ["winner"])
    if not d:
        return {"error": err or "pair-parse"}
    w = str(d.get("winner", "")).upper()
    if flip:                      # 还原到真实模式
        w = {"A": "B", "B": "A"}.get(w, "TIE")
    return {"winner": "none" if w == "A" else ("baseline" if w == "B" else "TIE"),
            "reason": str(d.get("reason", ""))[:40]}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    qs = QUESTIONS[:args.n]
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(timeout=400) as client:
        print(f"生成：{len(qs)} 题 × 2 模式（{GEN_MODEL}），并发 {args.concurrency}", flush=True)
        rows = []

        async def one(i, cat, sub, q):
            a, b = await asyncio.gather(
                generate(client, sem, q, "baseline"),
                generate(client, sem, q, "none"))
            ja = await judge_single(client, sem, q, a["text"] or "")
            jb = await judge_single(client, sem, q, b["text"] or "")
            pw = await judge_pair(client, sem, q, b["text"] or "", a["text"] or "")
            row = {"i": i, "cat": cat, "sub": sub, "q": q,
                   "baseline": a, "none": b,
                   "judge_baseline": ja, "judge_none": jb, "pairwise": pw}
            rows.append(row)
            sa = ja.get("mean"); sb = jb.get("mean")
            print(f"[{i+1}/{len(qs)}] {sub:9s} {cat[:16]:16s} "
                  f"base={a['ms']:6.0f}ms/{sa} none={b['ms']:6.0f}ms/{sb} "
                  f"胜={pw.get('winner', pw.get('error', '?'))}", flush=True)
            return row

        await asyncio.gather(*[one(i, c, s, q) for i, (c, s, q) in enumerate(qs)])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"reasoning_ab_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"gen_model": GEN_MODEL, "judge_model": JUDGE_MODEL,
                               "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 汇总 ────────────────────────────────────────────────────────────
    def agg(mode, sub=None):
        sel = [r for r in rows if sub is None or r["sub"] == sub]
        scores, lats, toks, empty = [], [], [], 0
        for r in sel:
            g = r[mode]; j = r[f"judge_{mode}"]
            if g["error"]:
                continue
            lats.append(g["ms"]); toks.append(g["tok"])
            if not (g["text"] or "").strip():
                empty += 1
            if j.get("mean") is not None:
                scores.append(j["mean"])
        return {"n": len(sel), "score": round(statistics.mean(scores), 4) if scores else None,
                "lat": round(statistics.median(lats), 1) if lats else None,
                "tok": round(statistics.median(toks), 1) if toks else None,
                "empty": empty}

    print("\n=== 按模式（全部题目）===")
    for mode in ("baseline", "none"):
        print(f"  {mode:9s}: {agg(mode)}")
    print("\n=== 按子集（判定/推理类是关键）===")
    for sub in ("fact", "grading", "writing", "emergency"):
        print(f"  [{sub}] base={agg('baseline', sub)}")
        print(f"  [{sub}] none={agg('none', sub)}")

    wins = {"none": 0, "baseline": 0, "TIE": 0, "err": 0}
    for r in rows:
        w = r["pairwise"].get("winner")
        wins[w if w in wins else "err"] += 1
    print(f"\n=== 两两对决（裁判={JUDGE_MODEL}，位置随机化）===\n  {wins}")

    a_all, b_all = agg("baseline"), agg("none")
    ga, gn = agg("baseline", "grading"), agg("none", "grading")
    print("\n=== 决策判定 ===")
    delta_all = (b_all["score"] or 0) - (a_all["score"] or 0)
    delta_g = ((gn["score"] or 0) - (ga["score"] or 0)) if (ga["score"] and gn["score"]) else 0
    win_rate = wins["none"] / max(1, wins["none"] + wins["baseline"])
    print(f"  总分差 none-baseline = {delta_all:+.4f}（阈值 -0.05）")
    print(f"  判定类分差 = {delta_g:+.4f}（阈值 -0.15 触发按意图路由）")
    print(f"  none 胜率 = {win_rate:.1%}（阈值 45%）")
    if delta_g <= -0.15:
        verdict = "ROUTE_BY_INTENT"
    elif delta_all >= -0.05 and win_rate >= 0.45:
        verdict = "KEEP_NONE"
    else:
        verdict = "REVIEW"
    print(f"  => {verdict}")
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
