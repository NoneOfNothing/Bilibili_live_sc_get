@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [init] Creating virtualenv and installing dependencies ...
    python -m venv .venv
    if errorlevel 1 (
        echo [error] Failed to create venv. Please install Python 3.9+ first.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [error] Failed to install dependencies. Check your network.
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" main.py %*
echo.
echo [exit] 程序已退出。按任意键关闭窗口...
pause >nul
