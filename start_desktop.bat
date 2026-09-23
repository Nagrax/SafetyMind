@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem desktop.pyw 自带自举逻辑：首次双击自动建 venv、装依赖并弹窗提示进度
python desktop.pyw
