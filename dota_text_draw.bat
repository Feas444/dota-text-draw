@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  Dota Text Draw launcher
rem  First run: auto-installs Python (winget or python.org) and
rem  dependencies, then starts the app in the tray.
rem    dota_text_draw.bat                silent tray launch
rem    dota_text_draw.bat --console      launch with console (debug)
rem ============================================================

set "PYEXE="

rem --- find existing python (prefer py launcher) ---
where py >nul 2>&1
if !errorlevel!==0 goto found_py
where python >nul 2>&1
if !errorlevel!==0 goto found_python
goto no_python

:found_py
set "PYEXE=py"
goto prepare

:found_python
set "PYEXE=python"
rem reject the Microsoft Store stub (does nothing on its own)
"%PYEXE%" -c "pass" >nul 2>&1
if !errorlevel!==0 set "PYEXE="
if defined PYEXE goto prepare
goto no_python

:no_python
echo [setup] Python not found - installing Python 3.12...
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "!PYEXE!" call :install_python
if not exist "!PYEXE!" (
    echo [setup] Could not install Python automatically.
    echo [setup] Download and install it manually: https://www.python.org/downloads/
    echo [setup] (tick "Add python.exe to PATH")
    pause
    exit /b 1
)

:prepare
rem --- install dependencies once (marker: .deps_ok) ---
if not exist ".deps_ok" (
    echo [setup] Installing dependencies...
    "%PYEXE%" -X utf8 -m pip install --disable-pip-version-check -r requirements.txt
    if !errorlevel!==0 (
        echo .deps_ok> .deps_ok
    ) else (
        echo [setup] Failed to install dependencies. Check your internet and retry.
        pause
        exit /b 1
    )
)

rem --- run ---
if "%~1"=="" (
    rem windowless interpreter (pyw/pythonw) so no console window
    set "WRUNNER=!PYEXE!"
    if /i "!PYEXE!"=="py" set "WRUNNER=pyw"
    if /i "!PYEXE!"=="python" set "WRUNNER=pythonw"
    set "WRUNNER=!WRUNNER:.exe=w.exe!"
    start "" /b "!WRUNNER!" -X utf8 "%~dp0dota_text_draw.py"
    exit /b
)
rem any argument (--console, --selftest, --doctor, --test) = console mode
"%PYEXE%" -X utf8 "%~dp0dota_text_draw.py" %*
pause
exit /b

:install_python
rem try winget first (Windows 10 1809+/11)
where winget >nul 2>&1
if !errorlevel!==0 (
    echo [setup] Using winget...
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    if exist "!PYEXE!" exit /b 0
    echo [setup] winget failed - downloading the installer.
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$u='https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe';" ^
  "$o=Join-Path $env:TEMP 'python-3.12.10-amd64.exe';" ^
  "Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $o;" ^
  "Start-Process -Wait -FilePath $o -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=1','Include_launcher=1','Include_pip=1'"
exit /b 0