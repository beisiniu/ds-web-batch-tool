@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. Please run start.bat first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m src.main import-session
if errorlevel 1 pause
