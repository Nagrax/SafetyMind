# -*- coding: utf-8 -*-
"""公平对照：给强模型同样的原型示例（few-shot），看能不能追上/超过 bge 原型集成。

## 为什么要这个实验（自我纠错）

前一版对比不公平：bge 用了 **213 条领域原型示例**（每类 10+ 条），
而强模型只拿到一行类目定义。这相当于让 bge 带资料考试、让 LLM 裸考。

用户提出的判断"足够强的模型意图识别率是足够的"，很可能在 few-shot 条件下成立。
本实验就是去验证它，而不是继续用不公平对照维护原结论。

## 三档对照

  A. bge 原型集成（213 条原型，零 token）
  B. 强模型 zero-shot（只有类目定义）
  C. 强模型 few-shot（每类 2~3 条原型示例，与 bge 同源）

## 关键观察点

1. few-shot 能否把 87% 拉到 93%+？
2. few-shot 的 prompt token 涨到多少？（决定每次调用的成本）
3. 延迟是否还能接受（意图在关键路径上）？

用法:
    <venv python> benchmarks/intent/eval_fewshot_fair.py --model trae/deepseek-v4.1-flash --sample 200
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
GATEWAY = "http://127.0.0.1:8326/v1"
import sys as _sys, pathlib as _pf
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent))
_sys.path.insert(0, str(_pf.Path(__file__).resolve().parent.parent))
from _creds import judge_key as _judge_key   # 密钥只从环境变量/仓库 .env 读，禁止写死
KEY = _judge_key()
LABEL_DESC = {
    "equipment_alarm": "设备报警、联锁动作、参数超限、异常停机",
    "equipment_maintenance": "设备检维修、保养、备件更换、检修计划",
    "hazard_report": "已发现的隐患或危险源需要上报记录",
    "work_permit": "动火、受限空间、高处作业等作业票/许可证办理与规定",
    "incident_report": "已发生的事故、险情、未遂事件报告",
    "emergency_response": "正在发生或刚发生的紧急情况，需要立即应急处置",
    "chemical_safety": "危化品性质、储存、运输、MSDS、CAS 号查询",
    "training_cert": "安全培训、资格证书、特种作业取证",
    "inspection_audit": "安全检查、专项检查、政府检查、审核",
    "occupational_health": "职业健康、职业病、体检、粉尘噪声危害",
    "hazard_inspection": "隐患排查工作怎么做、排查方法与频次",
    "regulation_query": "法律法规、标准条款的条文查询与解释",
    "ppe_inquiry": "个体防护用品选用与佩戴",
    "safety_document": "要写/要一份文档：台账、方案、预案、报告、记录表",
    "general_consult": "泛泛的安全咨询，不属于以上任何一类",
    "greeting": "打招呼、寒暄、感谢、告别",
    "feedback": "对系统回答的反馈、评价、纠错",
    "escalation": "要求转人工、找专家、升级处理",
    "other": "其他无法归类",
}


def load_items(split="test2"):
    if split == "test2":
        data = json.load(open(os.path.join(HERE, "test2_v4.json"), encoding="utf-8"))["new_items"]
        return [d for d in data if d["text"] not in REMOVED]
    return [d for d in json.load(open(os.path.join(HERE, "intent_eval_v3.1.json"), encoding="utf-8"))["data"]
            if d["split"] in (split, "all")]


def build_fewshot_examples(items, per_class=3, exclude_texts=None):
    """从 dev 集抽原型示例（绝不从被测集抽，避免数据泄漏）。"""
    dev = load_items("dev")
    by = defaultdict(list)
    for d in dev:
        if exclude_texts and d["text"] in exclude_texts:
            continue
        by[d["label"]].append(d["text"])
    shots = {}
    for lab in LABELS:
        shots[lab] = by.get(lab, [])[:per_class]
    return shots


def sys_zero():
    return ("你是安全生产意图分类器。只输出一个 JSON，不要解释。\n\n"
            "类目与定义：\n" + "\n".join(f"- {k}：{v}" for k, v in LABEL_DESC.items()) +
            '\n\n严格输出：{"intent":"<类目名>"}')


def sys_fewshot(shots):
    lines = ["你是安全生产意图分类器。只输出一个 JSON，不要解释。",
             "", "类目、定义与示例："]
    for k, v in LABEL_DESC.items():
        lines.append(f"- {k}：{v}")
        for s in shots.get(k, []):
            lines.append(f'    例：「{s}」')
    lines.append('')
    lines.append('严格输出：{"intent":"<类目名>"}')
    return "\n".join(lines)


async def call(client, model, system, text, sem):
    async with sem:
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                r = await client.post(
                    f"{GATEWAY}/chat/completions",
                    headers={"Authorization": f"Bearer {KEY}"},
                    json={"model": model, "max_tokens": 96, "temperature": 0.0,
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": text}]})
                if r.status_code != 200:
                    if attempt == 2:
                        return {"error": f"HTTP{r.status_code}", "ms": (time.monotonic() - t0) * 1000}
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
                    return {"error": f"{type(e).__name__}", "ms": (time.monotonic() - t0) * 1000}
                await asyncio.sleep(2)


def parse_intent(text):
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        v = (json.loads(t) or {}).get("intent")
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


async def run_scheme(name, model, system, items, concurrency):
    import httpx
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=180.0) as c:
        res = await asyncio.gather(*[call(c, model, system, d["text"], sem) for d in items])
    ok, n, errs = 0, 0, 0
    rec = defaultdict(lambda: [0, 0])
    in_tok, out_tok, lat = [], [], []
    for d, r in zip(items, res):
        if r.get("error"):
            errs += 1
            continue
        p = parse_intent(r.get("text"))
        if p is None:
            errs += 1
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
    return {
        "scheme": name, "model": model, "n": n, "errors": errs,
        "accuracy": round(ok / n, 4) if n else 0,
        "macro_recall": round(sum(a / b for a, b in rec.values() if b) / max(1, len([1 for a, b in rec.values() if b])), 4),
        "prompt_tokens_mean": round(sum(in_tok) / len(in_tok), 1) if in_tok else None,
        "completion_tokens_mean": round(sum(out_tok) / len(out_tok), 1) if out_tok else None,
        "latency_p50_ms": round(sorted(lat)[len(lat) // 2], 1) if lat else None,
    }


def run_bge(items):
    from core.intent_bge import BGEProtoClassifier
    clf = BGEProtoClassifier(temperature=0.03, tau_direct=0.8, use_large=False, device="cpu")
    clf.probs("预热")
    ok, n, rec, lat = 0, 0, defaultdict(lambda: [0, 0]), []
    for d in items:
        t0 = time.monotonic()
        probs = clf.probs(d["text"])
        lat.append((time.monotonic() - t0) * 1000)
        n += 1
        rec[d["label"]][1] += 1
        if max(probs, key=probs.get) == d["label"]:
            ok += 1; rec[d["label"]][0] += 1
    return {"scheme": "A_bge_proto", "model": "bge-base", "n": n, "errors": 0,
            "accuracy": round(ok / n, 4),
            "macro_recall": round(sum(a / b for a, b in rec.values()) / len(rec), 4),
            "prompt_tokens_mean": 0, "completion_tokens_mean": 0,
            "latency_p50_ms": round(sorted(lat)[len(lat) // 2], 1)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="trae/deepseek-v4.1-flash")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--shots", type=int, default=3)
    args = ap.parse_args()

    items = load_items("test2")
    if args.sample > 0:
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
    print(f"test2 n={len(items)}\n", flush=True)

    shots = build_fewshot_examples(items, per_class=args.shots)
    s_zero, s_few = sys_zero(), sys_fewshot(shots)
    print(f"zero-shot system {len(s_zero)} 字符；few-shot({args.shots}/类) system {len(s_few)} 字符\n", flush=True)

    out = []
    print("=== A. bge 原型集成 ===", flush=True)
    a = run_bge(items); out.append(a)
    print(f"acc={a['accuracy']} p50={a['latency_p50_ms']}ms tok=0/0\n", flush=True)

    print(f"=== B. {args.model} zero-shot ===", flush=True)
    b = await run_scheme("B_llm_zero", args.model, s_zero, items, args.concurrency); out.append(b)
    print(f"acc={b['accuracy']} tok={b['prompt_tokens_mean']}+{b['completion_tokens_mean']} "
          f"p50={b['latency_p50_ms']}ms 失败={b['errors']}\n", flush=True)

    print(f"=== C. {args.model} few-shot({args.shots}/类) ===", flush=True)
    c = await run_scheme("C_llm_fewshot", args.model, s_few, items, args.concurrency); out.append(c)
    print(f"acc={c['accuracy']} tok={c['prompt_tokens_mean']}+{c['completion_tokens_mean']} "
          f"p50={c['latency_p50_ms']}ms 失败={c['errors']}\n", flush=True)

    print("=== 汇总 ===")
    print(f"{'方案':24s} {'准确率':>8s} {'prompt':>8s} {'compl':>7s} {'p50ms':>8s}")
    for r in out:
        print(f"{r['scheme']:24s} {r['accuracy']:8.4f} {str(r['prompt_tokens_mean']):>8s} "
              f"{str(r['completion_tokens_mean']):>7s} {str(r['latency_p50_ms']):>8s}")
    print(f"\nfew-shot 相对 zero-shot 提升：{(c['accuracy']-b['accuracy'])*100:+.2f}pp")
    print(f"few-shot 相对 bge 差距：{(c['accuracy']-a['accuracy'])*100:+.2f}pp")

    p = os.path.join(HERE, "results",
                     f"intent_fewshot_fair_{args.model.replace('/','-')}{'_s'+str(args.sample) if args.sample else ''}.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "n": len(items),
               "shots_per_class": args.shots, "schemes": out}, open(p, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"written {p}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
