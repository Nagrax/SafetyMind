"""SafetyMind 桌面启动器 — 双击本文件即可使用。

在一个独立原生窗口里打开 Web 前端（pywebview + Edge WebView2），
FastAPI 后端在窗口背后的线程里运行，前端静态文件与 API 由同一进程提供；
关闭窗口即退出，不留后台进程。

本地无 Redis 时自动降级为进程内记忆（仅演示用途）；ChromaDB 同理
退化为本地嵌入式存储。填写同目录 .env 中的 ANTHROPIC_API_KEY 即可对话。
"""

import os
import socket
import threading
import time
import urllib.request
from pathlib import Path

# 双击运行时把工作目录锁定到项目根，保证 .env / skills 配置都能找到
os.chdir(Path(__file__).resolve().parent)


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
    import uvicorn
    import webview

    from api.main import app

    port = _pick_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
    ))
    threading.Thread(target=server.run, daemon=True).start()

    # 等后端就绪（最多 15 秒：首次启动需加载 ChromaDB 与知识库），避免窗口白屏
    for _ in range(150):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    webview.create_window(
        "SafetyMind 安全生产智能协同平台",
        f"http://127.0.0.1:{port}",
        width=1280,
        height=820,
        min_size=(960, 640),
    )
    webview.start()
    # 窗口关闭后主线程退出，daemon 线程里的服务随之结束


if __name__ == "__main__":
    main()
