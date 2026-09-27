"""SafetyMind 手机版启动器 — 双击打开 390×844 手机比例窗口。

与 desktop.pyw 同一套自举逻辑，区别只在窗口形态：
  1. 后端已在跑（desktop.pyw 或本脚本启动的）→ 只开手机窗口，不重复起服务；
  2. 后端没跑 → 走自举（建 venv/装依赖/查 API Key）+ 单实例锁启动后端，
     开手机窗口；关窗即退。
首次运行同样需要可见控制台装依赖（复用 desktop.pyw 的流程）。
"""

import os
import subprocess
import socket
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

# 与 desktop.pyw 相同的 D 盘缓存与下载兜底
_HF_CACHE = ROOT.drive + "\\hf_cache"
if os.path.isdir(_HF_CACHE):
    os.environ.setdefault("HF_HOME", _HF_CACHE)
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("API_HOST", "127.0.0.1")  # 桌面端仅本机监听：管理端点本地免密


def _log(msg: str) -> None:
    try:
        from datetime import datetime
        p = ROOT / "data" / "mobile.log"
        p.parent.mkdir(exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%m-%d %H:%M:%S} [{os.getpid()}] {msg}\n")
    except Exception:
        pass


def _msg(text: str, title: str = "SafetyMind 手机版") -> None:
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)


def _backend_alive(port: int = 8800, timeout: float = 0.8) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout)
        return True
    except Exception:
        return False


def _wait_backend(port: int, seconds: int = 90) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _backend_alive(port, timeout=1):
            return True
        time.sleep(0.5)
    return False


def _venv_pythonw() -> Path:
    return ROOT / ".venv" / "Scripts" / "pythonw.exe"


def _in_project_venv() -> bool:
    try:
        return Path(sys.executable).resolve().parent == (ROOT / ".venv" / "Scripts").resolve()
    except Exception:
        return False


def _bootstrap_and_restart() -> None:
    """首次运行：建 venv → 装依赖 → 用 pythonw 重启自身（复用 desktop.pyw 的控制台流程）。"""
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "首次运行 SafetyMind 手机版：\n\n即将创建虚拟环境并安装依赖（含向量库），可能需要几分钟，请保持网络畅通。",
            "首次启动", 0x40)
        proc = subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")],
                              creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        if proc.returncode != 0:
            _msg(f"虚拟环境创建失败（退出码 {proc.returncode}）。")
            sys.exit(1)
    probe = subprocess.run(
        [str(venv_python), "-c", "import webview, chromadb, redis, anthropic, fastapi, uvicorn, dotenv"],
        capture_output=True)
    if probe.returncode != 0:
        proc = subprocess.run([str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"],
                              creationflags=0x00000010)
        if proc.returncode != 0:
            _msg("依赖安装失败，请检查网络后重试。")
            sys.exit(1)
    _log("依赖就绪，重启为 venv pythonw")
    subprocess.Popen([str(_venv_pythonw()), str(Path(__file__).resolve())], close_fds=True)
    sys.exit(0)


def _check_api_key() -> None:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if key:
        return
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("ANTHROPIC_API_KEY=") and line.split("=", 1)[1].strip():
                return
    _msg("缺少 ANTHROPIC_API_KEY：请将 .env.example 复制为 .env 并填入密钥后重新打开。")
    os.startfile(str(ROOT))
    sys.exit(1)


def _pick_port() -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", 8800))
            return 8800
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def main(port: int) -> None:
    import threading
    import webbrowser

    import uvicorn
    import webview

    from api.main import app

    _log(f"手机窗口 port={port}")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    ready = False
    _deadline = time.time() + 90
    while time.time() < _deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.2)
    if not ready:
        _log("后端 90s 未就绪，进程退出")
        _msg("后端启动超时（90 秒未就绪），进程已退出。请关闭所有 SafetyMind 窗口后重试。")
        sys.exit(1)
    _log(f"窗口就绪 port={port}")

    def open_external(url: str) -> None:
        webbrowser.open(url)

    window = webview.create_window(
        "SafetyMind 手机版",
        f"http://127.0.0.1:{port}",
        width=390,
        height=844,
        min_size=(360, 640),
    )
    window.expose(open_external)
    webview.start()
    _log("手机窗口已关闭，进程退出")


if __name__ == "__main__":
    _log(f"mobile.pyw 启动 python={sys.executable}")
    try:
        own_backend = not _backend_alive()
        if own_backend:
            # 后端没跑：需要单实例锁 + 自举 + API Key
            from desktop import _single_instance, _check_api_key, _pick_port  # noqa: E402
            if not _single_instance():
                sys.exit(1)
            if not _in_project_venv():
                _log("非 venv 运行，进入自举")
                _bootstrap_and_restart()
            _check_api_key()
            main(_pick_port())
        else:
            # 后端已在跑（desktop.pyw）：只开手机窗口，服务生命周期归 desktop 管
            _log("检测到后端已在运行，直接开手机窗口")
            main(8800)
    except SystemExit:
        raise
    except Exception as ex:
        import traceback
        _log(f"启动失败: {ex}\n{traceback.format_exc()[-500:]}")
        _msg(f"手机版启动失败：\n\n{ex}\n\n{traceback.format_exc()[-800:]}")
