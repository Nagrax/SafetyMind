"""SafetyMind 桌面启动器 — 双击本文件即可使用。

设计要点（针对 Windows 双击场景，无控制台可见）：
  1. 自举虚拟环境：首次双击时自动创建 .venv 并安装 requirements.txt，
     在可见的控制台窗口里显示进度；装完后用 .venv 的 pythonw 无窗口重启自身。
  2. 零模型下载：嵌入式向量检索使用本地 n-gram 向量（core/local_embedding.py），
     不依赖外部嵌入模型 CDN。
  3. 永不静默失败：任何一步出错都用原生 MessageBox 弹窗说明原因。

运行时形态：pywebview 原生窗口（Edge WebView2）+ 后台线程里的 FastAPI，
前端静态文件与 API 由同一进程提供；关闭窗口即退出，不留后台进程。
本地无 Redis 时自动降级为进程内记忆；ChromaDB 退化为本地嵌入式存储。
"""

import os
import subprocess
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008


def _msg(text: str, title: str = "SafetyMind") -> None:
    """原生弹窗（pythonw 无控制台，这是唯一可见的错误通道）。"""
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR


def _venv_pythonw() -> Path:
    return ROOT / ".venv" / "Scripts" / "pythonw.exe"


def _in_project_venv() -> bool:
    try:
        return Path(sys.executable).resolve().parent == (ROOT / ".venv" / "Scripts").resolve()
    except Exception:
        return False


def _run_visible(cmd: list) -> int:
    """在独立的控制台窗口里执行命令，用户可看到进度；返回退出码。"""
    proc = subprocess.run(cmd, creationflags=CREATE_NEW_CONSOLE)
    return proc.returncode


def _bootstrap() -> None:
    """首次运行：建 venv → 装依赖 → 预热模型 → 用 pythonw 重启自身。"""
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"

    if not venv_python.exists():
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "首次运行 SafetyMind 桌面端：\n\n"
            "即将创建虚拟环境并安装依赖（含向量库），可能需要几分钟，请保持网络畅通。",
            "SafetyMind 首次启动", 0x40,  # MB_ICONINFORMATION
        )
        code = _run_visible([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        if code != 0:
            _msg(f"虚拟环境创建失败（退出码 {code}）。\n请改用 start_desktop.bat 启动并查看报错。")
            sys.exit(1)

    # 依赖缺失时安装（可见控制台显示进度；嵌入式向量用本地 n-gram，无需下载模型）
    probe = subprocess.run(
        [str(venv_python), "-c",
         "import webview, chromadb, redis, anthropic, fastapi, uvicorn, dotenv"],
        capture_output=True,
    )
    if probe.returncode != 0:
        code = _run_visible([str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"])
        if code != 0:
            _msg(
                "依赖安装失败。常见原因：\n"
                "1) 网络不通或超时（可配置国内镜像后重试：\n"
                "   .venv\\Scripts\\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple）\n"
                "2) Python 版本过低（需要 3.10+）。"
            )
            sys.exit(1)

    # 用无窗口的 pythonw 重启自身，退出当前进程
    subprocess.Popen(
        [str(_venv_pythonw()), str(Path(__file__).resolve())],
        creationflags=DETACHED_PROCESS,
        close_fds=True,
    )
    sys.exit(0)


def _check_api_key() -> None:
    """缺 key 时明确告知，避免服务起来就死、窗口白屏。"""
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("ANTHROPIC_API_KEY=") and line.split("=", 1)[1].strip():
                    return
        _msg(
            "缺少 ANTHROPIC_API_KEY：\n\n"
            "请将项目目录中的 .env.example 复制为 .env，\n"
            "填入 API 密钥（或兼容 Anthropic 协议的第三方端点）后重新双击。\n\n"
            "点击确定后自动打开项目文件夹。"
        )
        os.startfile(str(ROOT))
        sys.exit(1)


def _pick_port() -> int:
    """8800 被占用（比如本地服务已在跑）时自动换一个空闲端口。"""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", 8800))
            return 8800
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def main() -> None:
    import webbrowser

    import uvicorn
    import webview

    from api.main import app

    port = _pick_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
    ))
    threading.Thread(target=server.run, daemon=True).start()

    # 等后端就绪（最多 90 秒：冷启动含知识库导入），避免窗口先于服务白屏
    for _ in range(450):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.2)

    def open_external(url: str) -> None:
        """供前端调用：pywebview 不处理 target=_blank，外部链接走系统浏览器。"""
        webbrowser.open(url)

    window = webview.create_window(
        "SafetyMind 安全生产智能协同平台",
        f"http://127.0.0.1:{port}",
        width=1280,
        height=820,
        min_size=(960, 640),
    )
    window.expose(open_external)
    webview.start()
    # 窗口关闭后主线程退出，daemon 线程里的服务随之结束


if __name__ == "__main__":
    try:
        if not _in_project_venv():
            _bootstrap()
        _check_api_key()
        main()
    except SystemExit:
        raise
    except Exception as ex:  # 任何未预期错误都弹窗，绝不静默退出
        import traceback
        _msg(f"桌面端启动失败：\n\n{ex}\n\n{traceback.format_exc()[-800:]}")
