@echo off
rem ============================================
rem 直播间启动脚本模板
rem 用法：复制本文件，重命名为 start_room_你的房间号.bat，
rem       并把下面两处的「房间号」替换成实际直播间号（支持短号）。
rem 复制出的副本已被 .gitignore 排除，不会被提交到 git。
rem 也可以从命令行传参，例：start_room_模板.bat --duration 60 1234567
rem ============================================
chcp 65001 >nul
if "%~1"=="" (
    start "blive_sc_get - room 房间号" "%~dp0run_with_venv.bat" 房间号
) else (
    call "%~dp0run_with_venv.bat" %*
)
