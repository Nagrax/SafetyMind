# -*- coding: utf-8 -*-
"""端到端回答延迟剖析：把 /chat 的耗时拆到每个阶段，找出真正的大头。

## 为什么需要

用户明确要求"端到端回答延迟越快越好"。当前实测：
  - 常规问答 ~29.6s
  - 工具问答 ~16.7s
这两个数字对安全生产场景是不可接受的（安全员问一句话等半分钟）。

## 方法

在 /chat 主链路上打时间戳（不改产品逻辑，只在 api.main 里做阶段计时），
一次真实请求拆成：
  t_pre   紧急预检
  t_gate  并发闸门等待
  t_mem   记忆读取
  t_intent 意图识别
  t_rag   RAG 检索与上下文构建
  t_gen   专家生成（LLM 主体）
  t_post  审计/建单/落盘

## 关键区分

**非生成时间**（能优化的：意图/记忆/RAG）vs **生成时间**（受模型端点 TTFT 影响）。

用法:
    <venv python> benchmarks/profile_chat_latency.py --base http://127.0.0.1:8850 --live
产物: scratch/sm-loop/runs/chat_latency_profile.json
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

REPO = Path("D:/claudecode/SafetyMind")
OUT = Path("D:/claudecode/scratch/sm-loop/runs/chat_latency_profile.json")

CASES = [
    ("critical", "车间发生氨气泄漏，已经有人中毒晕倒了，现在该怎么办？"),
    ("normal", "动火作业票的有效期是怎么规定的？"),
    ("tool", "帮我查一下甲醇（CAS 67-56-1）的闪点是多少"),
    ("short", "你好"),
    ("regulation", "安全生产法对主要负责人职责怎么规定的？"),
]


def env_for(iso: Path, live: bool, port: int) -> dict:
    e = dict(os.environ)
    e.update({
        "HF_HOME": "D:/hf_cache", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "CHROMA_PERSIST_DIRECTORY": str(iso / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(iso / "kb_chroma"),
        "AUDIT_DB_PATH": str(iso / "audit.db"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(iso / "conversations"),
        "API_HOST": "127.0.0.1", "API_PORT": str(port),
        "REDIS_URL": "redis://127.0.0.1:9/0",
        "CHROMA_HOST": "127.0.0.1", "CHROMA_PORT": "9",
        "PROMETHEUS_PORT": str(port + 100),
        "SAFETYMIND_LLM_TIMEOUT_S": "120",
        "SAFETYMIND_THINKING": "off",
    })
    if live:
        e["ANTHROPIC_BASE_URL"] = os.getenv("ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
        e["ANTHROPIC_API_KEY"] = os.getenv("ANTHROPIC_API_KEY", "<REDACTED_RELAY_KEY>")
        e["ANTHROPIC_MODEL"] = os.getenv("ANTHROPIC_MODEL", "glm-4.6")
    else:
        e["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
        e["ANTHROPIC_API_KEY"] = "dummy"
        e["ANTHROPIC_MODEL"] = "dummy"
    return e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8850)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--repeat", type=int, default=2)
    args = ap.parse_args()

    import subprocess
    port = args.port
    base = f"http://127.0.0.1:{port}"
    iso = Path(f"D:/claudecode/scratch/sm-loop/iso/latency_{int(time.time())}")
    iso.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(REPO / ".venv" / "Scripts" / "python.exe"), "-m", "uvicorn", "api.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env_for(iso, args.live, port),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        # 等就绪
        ok = False
        for _ in range(240):
            try:
                if httpx.get(base + "/health", timeout=2).status_code == 200:
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ok:
            print("服务未就绪")
            return 2

        c = httpx.Client(base_url=base, timeout=300.0)
        rows = []
        for rep in range(args.repeat):
            for tag, msg in CASES:
                t0 = time.perf_counter()
                try:
                    r = c.post("/chat", json={"message": msg, "user_id": f"lat_{tag}",
                                              "conv_id": f"lat_{tag}_{rep}"})
                    total = (time.perf_counter() - t0) * 1000
                    b = r.json() if r.status_code == 200 else {}
                    rows.append({
                        "case": tag, "rep": rep, "total_ms": round(total, 1),
                        "code": r.status_code,
                        "latency_ms_reported": b.get("latency_ms"),
                        "total_ms_reported": b.get("total_ms"),
                        "resp_len": len((b.get("response") or "")),
                        "intent": b.get("intent"),
                        "tools": [t.get("name") for t in (b.get("tools_used") or [])],
                        "citations": len(b.get("citations") or []),
                    })
                except Exception as e:
                    rows.append({"case": tag, "rep": rep, "error": f"{type(e).__name__}: {e}"})
                print(f"[{tag}] {rows[-1].get('total_ms')}ms "
                      f"(服务自报 {rows[-1].get('total_ms_reported')}ms, len={rows[-1].get('resp_len')})",
                      flush=True)

        # 汇总
        by_case = {}
        for r in rows:
            if "total_ms" not in r:
                continue
            by_case.setdefault(r["case"], []).append(r["total_ms"])
        print("\n=== 各用例延迟（ms）===")
        for k, v in by_case.items():
            print(f"  {k:12s} 中位 {sorted(v)[len(v)//2]:.0f}  全部 {[round(x) for x in v]}")

        # 与工具/引用相关性的观察
        tool_rows = [r for r in rows if r.get("tools")]
        plain_rows = [r for r in rows if not r.get("tools") and "total_ms" in r]
        if tool_rows and plain_rows:
            tm = sorted(x["total_ms"] for x in tool_rows)[len(tool_rows) // 2]
            pm = sorted(x["total_ms"] for x in plain_rows)[len(plain_rows) // 2]
            print(f"\n工具类中位 {tm:.0f}ms vs 无工具类中位 {pm:.0f}ms（差 {tm-pm:+.0f}ms）")

        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"live": args.live, "rows": rows, "by_case": by_case},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten {OUT}")
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
