@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [First run] Creating Python environment...
  py -3.10 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
)

".venv\Scripts\python.exe" -c "import playwright, yaml, openpyxl, psutil" >nul 2>&1
if errorlevel 1 (
  echo [First run] Installing dependencies, please wait...
  ".venv\Scripts\python.exe" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
)

".venv\Scripts\python.exe" -m src.main menu
if errorlevel 1 pause
