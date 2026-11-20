@echo off
rem ============================================================
rem  Double-click this file to open the kicad-agent menu.
rem  It only launches menu.py with the project virtualenv.
rem  Keep the ASCII parts ASCII so cmd.exe never garbles them.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [!] Python virtualenv not found.
    echo      Open a terminal in this folder and run:
    echo          python -m venv .venv
    echo          .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" menu.py

if errorlevel 1 pause
