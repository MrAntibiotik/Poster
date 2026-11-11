@echo off
rem One-click install: finds or installs Python, creates .venv, installs dependencies, copies example configs.
rem Usage: setup.bat [nopause]
setlocal
cd /d "%~dp0"
set "PY="

rem 1) Python in PATH (the Microsoft Store stub fails this check, so it is skipped)
python -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>nul && set "PY=python"
rem 2) py launcher
if not defined PY py -3 -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>nul && set "PY=py -3"
rem 3) default per-user install location
if not defined PY call :find_local
rem 4) install with winget
if not defined PY (
    echo Python 3.11+ not found. Installing Python 3.12 via winget...
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    call :find_local
)
if not defined PY (
    echo [!] Could not install Python automatically.
    echo     Install Python 3.11+ from https://www.python.org/downloads/ with "Add python.exe to PATH" and run again.
    if /i not "%~1"=="nopause" pause
    exit /b 1
)
echo Using Python: %PY%

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail
if not exist config.json copy config.example.json config.json >nul
if not exist .env copy .env.example .env >nul
echo.
echo Install complete. Use menu.bat to run and manage poster.
if /i not "%~1"=="nopause" pause
exit /b 0

:find_local
for %%V in (313 312 311) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)
exit /b 0

:fail
echo [!] Installation failed - see messages above.
if /i not "%~1"=="nopause" pause
exit /b 1
