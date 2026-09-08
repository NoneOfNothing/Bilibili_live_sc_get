@echo off
rem Double-click = open the SC monitor GUI without keeping a console window.
rem (Run logs are visible in the GUI "debug" tab. First run shows bootstrap progress.)
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [init] Creating virtualenv and installing dependencies, please wait ...
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
start "" ".venv\Scripts\pythonw.exe" gui.py
