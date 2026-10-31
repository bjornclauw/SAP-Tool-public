@echo off
REM Launch the app with the portable Python. No console window.
REM To inspect engine logs, tick "Debug Console" in the toolbar.

setlocal
cd /d "%~dp0"

if exist "python\python-3.11.8.amd64\pythonw.exe" (
    start "" "python\python-3.11.8.amd64\pythonw.exe" "main.py"
) else if exist "python\pythonw.exe" (
    start "" "python\pythonw.exe" "main.py"
) else (
    start "" pythonw "main.py"
)
