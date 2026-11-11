@echo off
rem Runs the control panel with a visible console so any error is shown.
cd /d "%~dp0"
echo Python:
if exist ".venv\Scripts\python.exe" (".venv\Scripts\python.exe" --version) else (echo   .venv not found - run setup.bat first & pause & exit /b 1)
echo Folder: %CD%
echo.
".venv\Scripts\python.exe" panel.py
echo.
echo Panel stopped. If there is an error above - send a screenshot of this window.
pause
