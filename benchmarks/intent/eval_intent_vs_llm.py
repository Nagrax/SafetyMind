# -*- coding: utf-8 -*-
"""强模型意图识别对照（真实前沿模型版，2026-10-25 修订）

## 上一版的错误（必须记录，避免重犯）

上一版用 `glm-4.6`（智谱 2025 年的模型）当作"强模型"，得出"bge 胜过强模型"的结论。
**这个前提是错的** —— glm-4.6 不是当前前沿模型，用户当场指出。
用过期模型当对照组，等于自己给自己造了一个有利结论，属于方法论错误，不是数据错误。

本版改用实际可用的前沿模型：
  - trae/deepseek-v4.1-flash（DeepSeek V4.1 Flash，x0.06）
  - trae/glm-5.3-flash（GLM-5.3 Flash，x0.08）
  - zcode/GLM-5.3-Flash
  - qwen3.8-max / kimi-k3 等（可扩展）

## 本版的核心改进：测"方案"而不是测"模型"

面试官真正问的是：**有了强模型，还需不需要意图识别这一层？**
所以本版测三个方案，不是三个模型：

  P0 bge 原型集成（现方案）：本地嵌入 + 213 条原型，零 LLM
  P1 单次 LLM 直接分类：一次调用，system 里给 19 类定义 → 输出类目
  P2 LLM 一次调用直出答案（跳过意图）：不做意图，直接回答用户问题

对 P1/P2 都实测 **prompt tokens / completion tokens / 延迟 / 准确率**，
从而回答"意图识别到底花多少 token"这个具体问题。

## 用法

    <venv python> benchmarks/intent/eval_intent_vs_llm.py --model trae/deepseek-v4.1-flash
    <venv python> benchmarks/intent/eval_intent_vs_llm.py --model all --sample 100

产物：benchmarks/intent/results/intent_vs_llm_<model>.json
"""
import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
from core.intent_recognizer import IntentCategory

REMOVED = {
    ".中控室人员疏散集合点的设置和引导怎么做？", ".中控室人员疏散集合点的设置和引导咋做？",
    ".仓库人员疏散集合点的设置和引导怎么做？", ".车间人员疏散集合点的设置和引导怎么做？",
    ".装卸区人员疏散集合点的设置和引导怎么做？",
    "政府部门联合检查和专项检查的区别是什么？",
    "政府部门联合看和专项检查的区别是什么呗？",
}

LABELS = [c.value for c in IntentCategory]

# 每个类目一句定义（P1 用）：给 LLM 判定依据，不是给 bge 的原型
LABEL_DESC = {
    "equipment_alarm": "设备报警、联锁动作、参数超限、异常停机等技术报警类",
    "equipment_maintenance": "设备检维修、保养、备件更换、检修计划类",
    "hazard_report": "已发现的隐患或危险源需要上报记录类",
    "work_permit": "动火、受限空间、高处作业等作业票/许可证办理与规定类",
    "incident_report": "已经发生的事故、险情、未遂事件的报告类",
    "emergency_response": "正在发生或刚发生的紧急情况，需要立即应急处置步骤",
    "chemical_safety": "危化品性质、储存、运输、MSDS、CAS 号查询类",
    "training_cert": "安全培训、资格证书、特种作业取证类",
    "inspection_audit": "安全检查、专项检查、政府部门检查、审核类",
    "occupational_health": "职业健康、职业病、体检、粉尘噪声危害类",
    "hazard_inspection": "隐患排查工作怎么做、排查方法与频次类",
    "regulation_query": "法律法规、标准条款的条文查询与解释类",
    "ppe_inquiry": "个体防护用品（安全帽、口罩、防护服）选用与佩戴类",
    "safety_document": "要写/要一份文档：台账、方案、预案、报告、记录表类",
    "general_consult": "泛泛的安全咨询，不属于以上任何一类",
    "greeting": "打招呼、寒暄、感谢、告别",
    "feedback": "对系统回答的反馈、评价、纠错",
    "escalation": "要求转人工、找专家、升级处理",
    "other": "其他无法归类的输入",
}

CLASSIFY_SYSTEM = (
    "你是安全生产意图分类器。只输出一个 JSON，不要解释、不要 markdown。\n\n"
    "类目与定义：\n" + "\n".join(f"- {k}：{v}" for k, v in LABEL_DESC.items()) +
    '\n\n严格输出：{"intent":"<类目名>"}'
)

ANSWER_SYSTEM = (
    "你是 SafetyMind 安全生产助手，回答专业、具体、可执行。"
    "先给结论再给步骤；涉及法规时给出名称；不确定的内容明确说明不确定。"
)

GATEWAY = "http://127.0.0.1:8326/v1"
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
GATEWAY_KEY = _judge_key()
def load_items(split="test2"):
    if split == "test2":
        data = json.load(open(os.path.join(HERE, "test2_v4.json"), encoding="utf-8"))["new_items"]
        return [d for d in data if d["text"] not in REMOVED]
    return [d for d in json.load(open(os.path.join(HERE, "intent_eval_v3.1.json"), encoding="utf-8"))["data"]
            if d["split"] in (split, "all")]


async def call_llm(client, model, system, user, max_tokens=256, sem=None):
    """调网关（OpenAI 协议）。返回 text + usage + 延迟。"""
    async def _inner():
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                r = await client.post(
                    f"{GATEWAY}/chat/completions",
                    headers={"Authorization": f"Bearer {GATEWAY_KEY}"},
                    json={"model": model, "max_tokens": max_tokens, "temperature": 0.0,
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": user}]},
                )
                if r.status_code != 200:
                    if attempt == 2:
                        return {"error": f"HTTP {r.status_code}: {r.text[:150]}",
                                "ms": (time.monotonic() - t0) * 1000}
                    await asyncio.sleep(2 * (attempt + 1))
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
                    return {"error": f"{type(e).__name__}: {str(e)[:120]}",
                            "ms": (time.monotonic() - t0) * 1000}
                await asyncio.sleep(2 * (attempt + 1))

    if sem is not None:
        async with sem:
            return await _inner()
    return await _inner()


def parse_intent(text: str):
    if not text:
        return None
    t = text.strip()
    # 去掉 markdown 围栏
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        obj = json.loads(t)
        v = obj.get("intent")
        if isinstance(v, str):
            v = v.strip().strip('"').lower()
            if v in LABELS:
                return v
    except Exception:
        pass
    for v in LABELS:
        if v in t:
            return v
    return None


async def eval_p1(model, items, concurrency):
    """P1：单次 LLM 直接分类。"""
    import httpx
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=120.0) as c:
        tasks = [call_llm(c, model, CLASSIFY_SYSTEM, d["text"], 128, sem) for d in items]
        res = await asyncio.gather(*tasks)
    ok, n, errs = 0, 0, 0
    rec = defaultdict(lambda: [0, 0])
    in_tok, out_tok, lat = [], [], []
    preds = []
    for d, r in zip(items, res):
        if r.get("error") or not r.get("text"):
            errs += 1
            preds.append({"text": d["text"], "label": d["label"], "pred": None, "error": r.get("error")})
            continue
        p = parse_intent(r["text"])
        if p is None:
            errs += 1
            preds.append({"text": d["text"], "label": d["label"], "pred": None,
                          "raw": r["text"][:100], "error": "解析失败"})
            continue
        n += 1
        rec[d["label"]][1] += 1
        if p == d["label"]:
            ok += 1
            rec[d["label"]][0] += 1
        if r.get("in_tok"):
            in_tok.append(r["in_tok"])
        if r.get("out_tok"):
            out_tok.append(r["out_tok"])
        lat.append(r["ms"])
        preds.append({"text": d["text"], "label": d["label"], "pred": p, "raw": r["text"][:80], "ms": r["ms"]})
    return {
        "scheme": "P1_llm_classify", "model": model, "n": n, "errors": errs,
        "accuracy": round(ok / n, 4) if n else 0,
        "macro_recall": round(sum(a / b for a, b in rec.values() if b) / max(1, len([1 for a, b in rec.values() if b])), 4),
        "prompt_tokens_mean": round(sum(in_tok) / len(in_tok), 1) if in_tok else None,
        "completion_tokens_mean": round(sum(out_tok) / len(out_tok), 1) if out_tok else None,
        "latency_p50_ms": round(sorted(lat)[len(lat) // 2], 1) if lat else None,
        "per_class": {c: {"ok": a, "n": b, "recall": round(a / b, 3)} for c, (a, b) in sorted(rec.items())},
        "predictions": preds,
    }


async def eval_p2(model, items, concurrency):
    """P2：不做意图识别，直接让强模型回答（测它的 token 与延迟，用于成本对比）。

    只跑子集：回答问题比分类贵得多，全量 410 题成本过高。
    """
    import httpx
    sub = items[:30]
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=180.0) as c:
        tasks = [call_llm(c, model, ANSWER_SYSTEM, d["text"], 512, sem) for d in sub]
        res = await asyncio.gather(*tasks)
    in_tok, out_tok, lat, empty = [], [], [], 0
    for r in res:
        if r.get("error"):
            empty += 1
            continue
        if not r.get("text"):
            empty += 1
            continue
        if r.get("in_tok"):
            in_tok.append(r["in_tok"])
        if r.get("out_tok"):
            out_tok.append(r["out_tok"])
        lat.append(r["ms"])
    return {
        "scheme": "P2_direct_answer", "model": model, "n": len(sub), "empty_or_error": empty,
        "prompt_tokens_mean": round(sum(in_tok) / len(in_tok), 1) if in_tok else None,
        "completion_tokens_mean": round(sum(out_tok) / len(out_tok), 1) if out_tok else None,
        "latency_p50_ms": round(sorted(lat)[len(lat) // 2], 1) if lat else None,
        "latency_max_ms": round(max(lat), 1) if lat else None,
    }


def eval_p0(items):
    """P0：bge 原型集成（现方案）。"""
    from core.intent_bge import BGEProtoClassifier
    clf = BGEProtoClassifier(temperature=0.03, tau_direct=0.8, use_large=False, device="cpu")
    clf.probs("预热")
    ok, n, rec, lat = 0, 0, defaultdict(lambda: [0, 0]), []
    for d in items:
        t0 = time.monotonic()
        probs = clf.probs(d["text"])
        lat.append((time.monotonic() - t0) * 1000)
        pred = max(probs, key=probs.get)
        n += 1
        rec[d["label"]][1] += 1
        if pred == d["label"]:
            ok += 1
            rec[d["label"]][0] += 1
    return {
        "scheme": "P0_bge_proto_single", "model": "bge-base-zh-v1.5", "n": n, "errors": 0,
        "accuracy": round(ok / n, 4), "macro_recall": round(sum(a / b for a, b in rec.values()) / len(rec), 4),
        "prompt_tokens_mean": 0, "completion_tokens_mean": 0,
        "latency_p50_ms": round(sorted(lat)[len(lat) // 2], 1),
        "per_class": {c: {"ok": a, "n": b, "recall": round(a / b, 3)} for c, (a, b) in sorted(rec.items())},
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="trae/deepseek-v4.1-flash")
    ap.add_argument("--models", default="", help="逗号分隔多模型")
    ap.add_argument("--sample", type=int, default=0, help="0=全量 410")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--skip-p0", action="store_true")
    ap.add_argument("--skip-p2", action="store_true")
    args = ap.parse_args()

    items = load_items("test2")
    if args.sample > 0:
        # 分层抽样保证每类出现
        by = defaultdict(list)
        for d in items:
            by[d["label"]].append(d)
        picked, i = [], 0
        while len(picked) < min(args.sample, len(items)):
            added = False
            for lab in sorted(by):
                if i < len(by[lab]) and len(picked) < args.sample:
                    picked.append(by[lab][i]); added = True
            if not added:
                break
            i += 1
        items = picked
    print(f"数据集 test2 n={len(items)}（剔除 {len(REMOVED)} 条，与 bge 评测同口径）\n", flush=True)

    results = []
    if not args.skip_p0:
        print("=== P0 bge 原型集成（现方案）===", flush=True)
        p0 = eval_p0(items)
        results.append(p0)
        print(f"accuracy={p0['accuracy']} macro={p0['macro_recall']} "
              f"p50={p0['latency_p50_ms']}ms tokens=0/0\n", flush=True)

    models = [m.strip() for m in (args.models or args.model).split(",") if m.strip()]
    for model in models:
        print(f"=== P1 单次 LLM 分类：{model} ===", flush=True)
        p1 = await eval_p1(model, items, args.concurrency)
        results.append(p1)
        print(f"accuracy={p1['accuracy']} macro={p1['macro_recall']} n={p1['n']} 失败={p1['errors']} "
              f"prompt_tok={p1['prompt_tokens_mean']} compl_tok={p1['completion_tokens_mean']} "
              f"p50={p1['latency_p50_ms']}ms\n", flush=True)
        if not args.skip_p2:
            print(f"=== P2 不做意图直接回答：{model}（30 条子集）===", flush=True)
            p2 = await eval_p2(model, items, args.concurrency)
            results.append(p2)
            print(f"prompt_tok={p2['prompt_tokens_mean']} compl_tok={p2['completion_tokens_mean']} "
                  f"p50={p2['latency_p50_ms']}ms max={p2['latency_max_ms']}ms 空/错={p2['empty_or_error']}\n", flush=True)

    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    safe = "_".join(m.replace("/", "-") for m in models[:2])
    path = os.path.join(HERE, "results", f"intent_vs_llm_{safe}{'_s'+str(args.sample) if args.sample else ''}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "n": len(items),
                   "schemes": results}, f, ensure_ascii=False, indent=2)
    print(f"written {path}")

    # 汇总表
    print("\n=== 汇总 ===")
    print(f"{'方案':30s} {'准确率':>8s} {'prompt_tok':>11s} {'compl_tok':>10s} {'p50ms':>8s}")
    for r in results:
        print(f"{r['scheme']+' '+(r['model'][:16]):30s} {r.get('accuracy',0):8.4f} "
              f"{str(r.get('prompt_tokens_mean')):>11s} {str(r.get('completion_tokens_mean')):>10s} "
              f"{str(r.get('latency_p50_ms')):>8s}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
