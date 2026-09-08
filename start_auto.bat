@echo off
rem Double-click = auto-detect bilibili live rooms open in Edge/Chrome tabs,
rem then record their SuperChat. Newly opened rooms are picked up every 30 seconds.
start "blive_sc_get - auto browser rooms" "%~dp0run_with_venv.bat" --auto
