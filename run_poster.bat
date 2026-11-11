@echo off
rem Start poster with a console window. First run installs everything automatically.
rem Autostart with Windows: install_task.ps1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: installing...
    call setup.bat nopause || exit /b 1
)
".venv\Scripts\python.exe" poster.py run
pause
