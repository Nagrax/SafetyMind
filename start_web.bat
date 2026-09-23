@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [SafetyMind] 首次运行：正在创建虚拟环境并安装依赖，请保持网络畅通...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [SafetyMind] 依赖安装失败，请检查网络后重试，或使用国内镜像：
    echo ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
  )
)
echo SafetyMind Web 启动中: http://127.0.0.1:8000
start "" http://127.0.0.1:8000
".venv\Scripts\python.exe" -m api.main
