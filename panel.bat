@echo off
rem Opens the poster control panel in the browser. First run installs everything automatically.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: installing, this takes 1-3 minutes...
    call setup.bat nopause
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
if not exist config.json copy config.example.json config.json >nul
if not exist .env copy .env.example .env >nul
start "" ".venv\Scripts\pythonw.exe" panel.py
