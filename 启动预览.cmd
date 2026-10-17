@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "打开预览.html" (
  start "" "%~dp0打开预览.html"
  exit /b 0
)
python scripts\serve.py --open
if errorlevel 1 pause
