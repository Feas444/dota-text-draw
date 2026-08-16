@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  Dota Text Draw launcher (no cmd window by default)
rem    dota_text_draw.bat                silent tray launch
rem    dota_text_draw.bat --console      launch with console (debug)
rem ============================================================

if /i "%~1"=="--console" (
    py -X utf8 "%~dp0dota_text_draw.py" %*
    pause
    exit /b
)

start "" /b pyw -X utf8 "%~dp0dota_text_draw.py"
