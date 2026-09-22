@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  call .venv\Scripts\activate.bat
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
echo SafetyMind Web 启动中: http://127.0.0.1:8000
start "" http://127.0.0.1:8000
python -m api.main
