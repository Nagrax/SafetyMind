# -*- coding: utf-8 -*-
"""真实评测：同一批问题下，中继上哪些模型**又快又能用**（选型依据）。

## 为什么

延迟分账显示非生成开销只占 8%，也验证了"缩短答案"只降 13% 延迟（未达门槛）——
剩下的唯一大杠杆就是**换生成模型**。但换模型不能只看速度：判定类问题上一旦模型
思考失控（如 zcode/GLM-5.3-Flash 正文 0 字），体验会更差。

## 方法

同一组问题（1 条事实类 + 1 条判定类），直连中继、`temperature=0`、
`reasoning_effort=none`、`max_tokens=2048`，逐模型记录：
耗时 / 正文字数 / reasoning 字数 / finish_reason / completion tokens，
再用同一裁判给四维分（单采样，仅作筛选，选定后再做双采样确认）。

## 决策规则

候选模型须同时满足：
  1. 延迟 p50 比现役（raccoon/sn-deepseek-v4-1-flash-1017）快 ≥ 30%
  2. accuracy 不低于现役 − 0.5
  3. 正文非空（不出现"思考吃光预算、正文 0 字"）
否则保持现役。

用法: <venv python> benchmarks/eval_model_latency.py
产物: benchmarks/results/model_latency_<ts>.json
"""
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
OUT_DIR = REPO / "benchmarks" / "results"

BASE, KEY = "http://127.0.0.1:8326/v1", ""
for line in open(REPO / ".env", encoding="utf-8", errors="ignore"):
    line = line.strip()
    if line.startswith("ANTHROPIC_API_KEY=") and not KEY:
        KEY = line.split("=", 1)[1].strip()
    if line.startswith("ANTHROPIC_BASE_URL="):
        BASE = line.split("=", 1)[1].strip().rstrip("/")

CURRENT = "raccoon/sn-deepseek-v4-1-flash-1017"
CANDIDATES = [
    CURRENT,
    "aggregate/deepseek-v4-flash",
    "aggregate/kimi-k2-5",
    "aggregate/qwen3-8-flash",
    "aggregate/glm-5-3-flash",
    "minimax/MiniMax-M3.1-Flash",
]

QUESTIONS = [
    "动火作业票分几个等级？各级有效期多久？依据哪个标准？",
    "安全阀超期未校验，应判定为一般事故隐患还是重大事故隐患？请给出判定依据与理由。",
]

SYS = ("你是安全生产领域的答案评审员。按四维各打 1-5 分：accuracy=事实/数值/条款准确性；"
       "evidence=可核对依据；concrete=可执行程度；safety=不确定时是否提示核实。\n"
       "只输出 JSON：{\"accuracy\":n,\"evidence\":n,\"concrete\":n,\"safety\":n}")


def ask(client, model, q):
    t = time.perf_counter()
    r = client.post(BASE + "/chat/completions",
                    headers={"Authorization": "Bearer " + KEY},
                    json={"model": model, "max_tokens": 2048, "temperature": 0,
                          "reasoning_effort": "none",
                          "messages": [{"role": "user", "content": q}]},
                    timeout=300)
    ms = (time.perf_counter() - t) * 1000
    if r.status_code >= 400:
        return {"error": f"HTTP{r.status_code}", "ms": round(ms, 0)}
    d = r.json()
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = msg.get("content") or ""
    return {"ms": round(ms, 0), "content": content,
            "chars": len(content),
            "reasoning_chars": len(msg.get("reasoning_content") or ""),
            "finish": ch.get("finish_reason"),
            "completion": (d.get("usage") or {}).get("completion_tokens")}


def judge(client, q, ans):
    try:
        r = client.post(BASE + "/chat/completions",
                        headers={"Authorization": "Bearer " + KEY},
                        json={"model": CURRENT, "max_tokens": 800, "temperature": 0,
                              # 裁判同样要关推理：中继默认会先产 reasoning_content，
                              # 预算被吃光时正文为空 → 裁判静默返回 None（本轮实测踩到）
                              "reasoning_effort": "none",
                              "messages": [{"role": "system", "content": SYS},
                                           {"role": "user",
                                            "content": f"问题：{q}\n\n答案：{ans[:4000]}"}]},
                        timeout=300)
        if r.status_code >= 400:
            print(f"    judge HTTP{r.status_code}: {r.text[:90]}", flush=True)
            return None
        txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{[\s\S]*\}", txt)
        if not m:
            print(f"    judge 无 JSON: {txt[:80]!r}", flush=True)
            return None
        return json.loads(m.group(0))
    except Exception as ex:
        print(f"    judge EXC {type(ex).__name__}: {ex}", flush=True)
        return None


def main():
    rows = []
    with httpx.Client(timeout=300) as c:
        for model in CANDIDATES:
            for q in QUESTIONS:
                got = ask(c, model, q)
                got["model"] = model
                got["q"] = q
                if "content" in got:
                    got["score"] = judge(c, q, got["content"])
                rows.append(got)
                sc = got.get("score") or {}
                print(f"{model:34s} {got.get('ms'):>7}ms chars={got.get('chars')} "
                      f"reason={got.get('reasoning_chars')} finish={got.get('finish')} "
                      f"acc={sc.get('accuracy')}", flush=True)

    print("\n=== 模型筛选 ===")
    summary = {}
    for model in CANDIDATES:
        grp = [r for r in rows if r["model"] == model and "chars" in r]
        if not grp:
            summary[model] = {"ok": False}
            print(f"  {model:34s} 全部失败")
            continue
        accs = [(r.get("score") or {}).get("accuracy") for r in grp]
        accs = [a for a in accs if a]
        summary[model] = {
            "ok": True,
            "ms_p50": round(statistics.median([r["ms"] for r in grp]), 0),
            "chars_p50": round(statistics.median([r["chars"] for r in grp]), 0),
            "empty": sum(1 for r in grp if r["chars"] == 0),
            "acc": round(statistics.mean(accs), 2) if accs else None,
        }
        print(f"  {model:34s} p50={summary[model]['ms_p50']:.0f}ms "
              f"chars={summary[model]['chars_p50']:.0f} 空答={summary[model]['empty']} "
              f"acc={summary[model]['acc']}")

    base = summary.get(CURRENT, {})
    pick = CURRENT
    if base.get("ok"):
        for model, s in summary.items():
            if model == CURRENT or not s.get("ok"):
                continue
            if s["empty"]:
                continue
            faster = s["ms_p50"] <= base["ms_p50"] * 0.70
            quality = (s["acc"] is not None and base["acc"] is not None
                       and s["acc"] >= base["acc"] - 0.5)
            if faster and quality:
                pick = model
                print(f"  候选胜出：{model}（p50 {s['ms_p50']:.0f}ms vs 现役 {base['ms_p50']:.0f}ms）")
                break
    print(f"  决策：{'保持现役' if pick == CURRENT else '切换候选需要双采样确认：' + pick}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"model_latency_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"pick": pick, "summary": summary,
                               "rows": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\nwritten {out}")


if __name__ == "__main__":
    main()
