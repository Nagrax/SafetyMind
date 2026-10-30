# -*- coding: utf-8 -*-
"""冷启动延迟诊断：为什么服务 180s 都起不来？

## 已观测到的现象

prod_readiness G1 失败：180s 内 /health 未就绪。日志显示启动阶段卡在
`httpx HEAD https://hf-mirror.com/BAAI/bge-base-zh-v1.5/...`——模型加载前在联网检查更新。

## 为什么这是生产缺陷（不只是慢）

- 生产服务器可能**无外网**（内网部署）或 HF 被墙/限流 → 服务永远起不来
- 即使能联网，每次冷启动都要等远端 HEAD 请求（数秒~数十秒）
- 重启是运维常态（发版、故障恢复），启动慢直接等于可用性差

## 本脚本做两件事

1. 测量：本地已缓存时，联网检查 vs 离线模式（HF_HUB_OFFLINE=1）的启动耗时差
2. 归因：确认卡点在哪一次外部请求

用法:
    <venv python> benchmarks/diagnose_coldstart.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / ".venv" / "Scripts" / "python.exe"
OUT = Path("D:/claudecode/scratch/sm-loop/runs/coldstart.json")


def env_for(iso: Path, offline: bool, port: int) -> dict:
    e = dict(os.environ)
    e.update({
        "HF_HOME": "D:/hf_cache",
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "CHROMA_PERSIST_DIRECTORY": str(iso / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(iso / "kb_chroma"),
        "AUDIT_DB_PATH": str(iso / "audit.db"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(iso / "conversations"),
        "API_HOST": "127.0.0.1", "API_PORT": str(port),
        "REDIS_URL": "redis://127.0.0.1:9/0",
        "CHROMA_HOST": "127.0.0.1", "CHROMA_PORT": "9",
        "PROMETHEUS_PORT": str(port + 100),
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy", "ANTHROPIC_MODEL": "dummy",
    })
    if offline:
        e["HF_HUB_OFFLINE"] = "1"
        e["TRANSFORMERS_OFFLINE"] = "1"
        e["HF_HUB_DISABLE_TELEMETRY"] = "1"
    return e


def try_boot(iso_name: str, offline: bool, port: int, timeout: float = 240) -> dict:
    import httpx
    iso = Path(f"D:/claudecode/scratch/sm-loop/iso/{iso_name}")
    iso.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{port}"
    log_path = iso / "boot.log"
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8") as lf:
        p = subprocess.Popen(
            [str(PY), "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(REPO), env=env_for(iso, offline, port),
            stdout=lf, stderr=subprocess.STDOUT,
        )
        ready = False
        while time.time() - t0 < timeout:
            try:
                if httpx.get(base + "/health", timeout=2).status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        elapsed = time.time() - t0
        try:
            p.terminate()
            p.wait(timeout=25)
        except Exception:
            p.kill()
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    # 统计外部请求次数
    hf_calls = log.count("hf-mirror.com")
    return {
        "mode": "offline" if offline else "online",
        "ready": ready,
        "seconds": round(elapsed, 1),
        "hf_head_requests": hf_calls,
        "log_tail": log[-1200:],
        "log_path": str(log_path),
    }


def main():
    print("=== A. 联网模式（当前默认行为）===")
    a = try_boot(f"cold_online_{int(time.time())}", offline=False, port=8803)
    print(f"ready={a['ready']} {a['seconds']}s, HF 外部请求 {a['hf_head_requests']} 次", flush=True)

    print("\n=== B. 离线模式（HF_HUB_OFFLINE=1，本地缓存命中）===")
    b = try_boot(f"cold_offline_{int(time.time())}", offline=True, port=8804)
    print(f"ready={b['ready']} {b['seconds']}s, HF 外部请求 {b['hf_head_requests']} 次", flush=True)

    print("\n=== 结论 ===")
    if a["ready"] and b["ready"]:
        diff = a["seconds"] - b["seconds"]
        print(f"联网 {a['seconds']}s vs 离线 {b['seconds']}s（差 {diff:+.1f}s）")
        print(f"外部请求：联网 {a['hf_head_requests']} 次 → 离线 {b['hf_head_requests']} 次")
        if diff > 5:
            print("→ 结论：联网检查是显著延迟来源，生产应默认离线模式")
        else:
            print("→ 结论：缓存已命中，联网检查成本可接受；但无网环境仍会失败")
    else:
        print(f"联网 ready={a['ready']}，离线 ready={b['ready']} —— 需看日志定位")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"online": a, "offline": b}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\nwritten {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
