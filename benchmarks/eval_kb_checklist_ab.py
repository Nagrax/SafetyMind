# -*- coding: utf-8 -*-
"""真实评测：注入口径（条款依据 vs 检查要点）能否救回扩容的答案质量。

## 背景

1698 条隐患检查项灌库后（`full`）：隐患类召回 oracle@2 0.25→0.75、无解 66.7%→0%，
但答案质量下降（accuracy −0.278、safety −0.528）。假设：检查项被当成"法规条款依据"注入。
本实验在同一 `full` 库上换注入口径（`SAFETYMIND_KB_INJECT_STYLE=checklist`），
看能否把召回收益拿回来。

## 方法（三臂，**每臂独立进程**）

  A. demo（9 子块，默认口径）—— 当前线上基线
  B. full（1707 子块，默认口径）—— 已知答案变差
  C. full（1707 子块，checklist 口径）—— 本实验主角

> 为什么独立进程：上一版用 `importlib.reload(api.main)` 切臂，实测整臂答案退化成
> 20 字左右的错误文案（判分全 FAIL），实验作废。独立进程最稳。

## 决策规则（先定）

采纳 checklist 需同时满足：
  1. 子集加权 accuracy 不低于 A − 0.1，safety 不低于 A − 0.1
  2. 隐患子集 accuracy 不低于 A 的隐患子集（扩容的主要受益方不能变差）
  3. 相对 B 有改善（accuracy(C) > accuracy(B)）
否则维持 A（不扩容）。

用法: <venv python> benchmarks/eval_kb_checklist_ab.py
产物: benchmarks/results/kb_checklist_ab_<ts>.json
"""
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "benchmarks"))
OUT_DIR = REPO / "benchmarks" / "results"
ISO = Path("D:/claudecode/scratch/sm-loop/iso/kb_style")
TMP = Path("D:/claudecode/scratch/sm-loop/style_arms")
PY = REPO / ".venv" / "Scripts" / "python.exe"

from _creds import judge_key  # noqa: E402

GATEWAY = os.getenv("JUDGE_GATEWAY", "http://127.0.0.1:8326/v1")
GKEY = judge_key()
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "raccoon/sn-deepseek-v4-1-flash-1017")
SYS = ("你是安全生产领域的答案评审员。按四维各打 1-5 分：accuracy=事实/数值/条款准确性；"
       "evidence=可核对依据；concrete=可执行程度；safety=不确定时是否提示核实。\n"
       "只输出 JSON：{\"accuracy\":n,\"evidence\":n,\"concrete\":n,\"safety\":n}")
DIMS = ("accuracy", "evidence", "concrete", "safety")

ARMS = [
    ("demo", ISO / "demo" / "kb_chroma", ""),
    ("full_default", ISO / "full" / "kb_chroma", ""),
    ("full_checklist", ISO / "full" / "kb_chroma", "checklist"),
]


def judge(client, q, ans):
    """裁判调用：429 指数退避 + 节奏控制，避免整批判分被限流打成全 FAIL。

    2026-10-30 实测教训：36 次对话刚跑完立刻连打 72 次判分，中继限流 →
    3 次重试全撞 429 → 36/36 判分 FAIL，整轮实验作废（答案本身是有效的）。
    """
    for attempt in range(4):
        try:
            r = client.post(GATEWAY + "/chat/completions",
                            headers={"Authorization": "Bearer " + GKEY},
                            json={"model": JUDGE_MODEL, "max_tokens": 800, "temperature": 0,
                                  "reasoning_effort": "none",
                                  "messages": [{"role": "system", "content": SYS},
                                               {"role": "user",
                                                "content": f"问题：{q}\n\n答案：{ans[:4000]}"}]},
                            timeout=300)
            if r.status_code == 429:
                time.sleep(min(20, 3 * (attempt + 1) ** 2))
                continue
            if r.status_code >= 400:
                print(f"    judge HTTP{r.status_code}: {r.text[:80]}", flush=True)
                time.sleep(2)
                continue
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            m = re.search(r"\{[\s\S]*\}", txt)
            if m:
                time.sleep(0.4)          # 给中继留余量
                return json.loads(m.group(0))
        except Exception:
            time.sleep(2)
    return None


def run_arm(name, kb_path, style):
    TMP.mkdir(parents=True, exist_ok=True)
    out = TMP / f"{name}.json"
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "HF_HOME": "D:/hf_cache",
                "PROMETHEUS_PORT": "0", "SAFETYMIND_LLM_PROTOCOL": "openai"})
    cmd = [str(PY), str(REPO / "benchmarks" / "_kb_style_worker.py"),
           "--kb-path", str(kb_path), "--style", style, "--out", str(out)]
    print(f"→ 起臂 {name}（style={style or 'default'}）", flush=True)
    r = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore", timeout=1800)
    tail = (r.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        print("   " + line, flush=True)
    if not out.exists():
        print(f"   ✖ 臂 {name} 未产出（stderr 尾部）：{(r.stderr or '')[-300:]}", flush=True)
        return []
    return json.loads(out.read_text(encoding="utf-8"))


def main():
    rejudge = "--rejudge" in sys.argv      # 只对已保存的答案重跑裁判（不重新对话）

    if rejudge:
        rows = []
        for name, _, _ in ARMS:
            p = TMP / f"{name}.json"
            if not p.exists():
                print(f"缺少已保存的臂数据: {p}")
                return 2
            arm_rows = json.loads(p.read_text(encoding="utf-8"))
            # 臂名以文件名为准（worker 里 arm 字段可能是 style 名，demo/full_default 会撞车）
            for r in arm_rows:
                r["arm"] = name
            rows.extend(arm_rows)
        print(f"复用 {len(rows)} 条已保存答案，只重跑裁判", flush=True)
    else:
        for name, path, _ in ARMS:
            if not Path(path).exists():
                print(f"缺少隔离库 {name}: {path}（先跑 scratch/sm-loop/build_kb_style.py）")
                return 2
        rows = []
        for name, path, style in ARMS:
            rows.extend(run_arm(name, path, style))

    judged = [r for r in rows if r.get("ans")]
    print(f"\n有效答案 {len(judged)}/{len(rows)}", flush=True)
    with httpx.Client(timeout=300) as jc:
        for r in judged:
            got = [judge(jc, r["q"], r["ans"]) for _ in range(2)]
            got = [g for g in got if g]
            if got:
                r["score"] = {d: round(statistics.mean([float(g.get(d) or 0) for g in got]), 3)
                              for d in DIMS}
            print(f"  judge[{r['arm']:14s} {r['tag']:10s}] {r.get('score') or 'FAIL'}", flush=True)

    print("\n=== 注入口径 A/B（三臂）===")
    summary = {}
    for name, _, _ in ARMS:
        for tag in ("general", "inspection"):
            grp = [r for r in rows if r["arm"] == name and r["tag"] == tag and r.get("score")]
            if not grp:
                continue
            summary[f"{name}_{tag}"] = {
                "n": len(grp),
                **{d: round(statistics.mean([r["score"][d] for r in grp]), 3) for d in DIMS},
                "cites_mean": round(statistics.mean([r["cites"] for r in grp]), 2),
            }
            print(f"  {name:14s} {tag:10s} {summary[f'{name}_{tag}']}")

    def acc(name, tag):
        return summary.get(f"{name}_{tag}", {}).get("accuracy")

    def safe(name, tag):
        return summary.get(f"{name}_{tag}", {}).get("safety")

    verdict = "insufficient"
    need = [acc("demo", "general"), acc("demo", "inspection"),
            acc("full_default", "general"), acc("full_default", "inspection"),
            acc("full_checklist", "general"), acc("full_checklist", "inspection"),
            safe("demo", "general"), safe("full_checklist", "general")]
    if all(v is not None for v in need):
        a_all = (acc("demo", "general") + acc("demo", "inspection")) / 2
        b_all = (acc("full_default", "general") + acc("full_default", "inspection")) / 2
        c_all = (acc("full_checklist", "general") + acc("full_checklist", "inspection")) / 2
        print(f"  子集加权 accuracy：demo={a_all:.3f} full_default={b_all:.3f} "
              f"full_checklist={c_all:.3f}")
        ok = (c_all >= a_all - 0.1
              and safe("full_checklist", "general") >= safe("demo", "general") - 0.1
              and acc("full_checklist", "inspection") >= acc("demo", "inspection")
              and c_all > b_all)
        verdict = "adopt_checklist" if ok else "keep_demo"
        print(f"  决策：{verdict}")
    else:
        print("  样本不足（有臂未产出或判分失败），不作结论")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"kb_checklist_ab_{datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps({"verdict": verdict, "summary": summary,
                             "rows": [{k: v for k, v in r.items() if k != "ans"} for r in rows]},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwritten {p}")


if __name__ == "__main__":
    os.environ.setdefault("SAFETYMIND_LLM_PROTOCOL", "openai")
    main()
