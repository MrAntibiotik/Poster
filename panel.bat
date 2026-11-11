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
if exist panel_error.txt del panel_error.txt
rem Close an old copy of the panel (e.g. after an update). The bot keeps running.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -like '*panel.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
start "" ".venv\Scripts\pythonw.exe" panel.py
echo.
echo Opening the control panel in your browser: http://127.0.0.1:8770/
echo If nothing opens in 10 seconds, run panel_debug.bat and send a screenshot.
timeout /t 6 >nul
