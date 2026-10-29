# -*- coding: utf-8 -*-
"""启动卡死定位：用子进程 + 实时读取输出，找出卡在哪一步。

前面用 `Start-Process -RedirectStandardOutput` 抓不到日志（文件为空），
管道/重定向在这个环境不可靠。本脚本用 subprocess.PIPE 直接读，
并**每 5 秒打印一次新增输出**，这样即使最终超时也能看到最后一行卡在哪。
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path("D:/claudecode/SafetyMind")
PY = REPO / ".venv" / "Scripts" / "python.exe"
ISO = Path("D:/claudecode/scratch/sm-loop/iso/boot_probe")
ISO.mkdir(parents=True, exist_ok=True)

env = dict(os.environ)
env.update({
    "HF_HOME": "D:/hf_cache", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
    "CHROMA_PERSIST_DIRECTORY": str(ISO / "chroma"),
    "CHROMA_KB_PERSIST_DIRECTORY": str(ISO / "kb"),
    "AUDIT_DB_PATH": str(ISO / "audit.db"),
    "SAFETYMIND_CONVERSATIONS_DIR": str(ISO / "conv"),
    "API_HOST": "127.0.0.1", "API_PORT": "8809",
    "REDIS_URL": "redis://127.0.0.1:9/0",
    "CHROMA_HOST": "127.0.0.1", "CHROMA_PORT": "9",
    "PROMETHEUS_PORT": "8909",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    "ANTHROPIC_API_KEY": "dummy", "ANTHROPIC_MODEL": "dummy",
})

lines = []
lock = threading.Lock()


def reader(stream, tag):
    for raw in iter(stream.readline, ""):
        ln = raw.rstrip()
        if not ln:
            continue
        with lock:
            lines.append(ln)
        print(f"[{tag}] {ln[:220]}", flush=True)


p = subprocess.Popen(
    [str(PY), "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8809"],
    cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", errors="replace", bufsize=1,
)
threading.Thread(target=reader, args=(p.stdout, "out"), daemon=True).start()
threading.Thread(target=reader, args=(p.stderr, "err"), daemon=True).start()

import httpx
t0 = time.time()
ready = False
last_report = 0
while time.time() - t0 < 420:
    if p.poll() is not None:
        print(f"!!! 进程提前退出 code={p.returncode}", flush=True)
        break
    try:
        if httpx.get("http://127.0.0.1:8809/health", timeout=2).status_code == 200:
            ready = True
            break
    except Exception:
        pass
    el = time.time() - t0
    if int(el) - last_report >= 30:
        last_report = int(el)
        with lock:
            tail = lines[-3:] if lines else ["(尚无输出)"]
        print(f"--- {el:.0f}s 未就绪，最近输出：{tail[-1][:180] if tail else '?'}", flush=True)
    time.sleep(1)

elapsed = time.time() - t0
print(f"\n=== ready={ready} elapsed={elapsed:.1f}s 输出行数={len(lines)} ===")
print("=== 最后 25 行 ===")
for ln in lines[-25:]:
    print("  " + ln[:220])

try:
    p.terminate()
    p.wait(timeout=25)
except Exception:
    p.kill()

Path("D:/claudecode/scratch/sm-loop/runs/boot_probe.log").write_text(
    "\n".join(lines), encoding="utf-8")
sys.exit(0 if ready else 1)
