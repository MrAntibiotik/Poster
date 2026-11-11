@echo off
rem poster control menu. First run installs everything automatically.
cd /d "%~dp0"
title poster
if not exist ".venv\Scripts\python.exe" (
    echo First run: installing, this takes 1-3 minutes...
    call setup.bat nopause
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" menu.py
