@echo off
rem Emergency stop: closes ALL poster processes (bot, panels, channel bots) and the PowerShell helpers they started.
rem Other Python programs are not touched. Start again afterwards with run_poster.bat / panel.bat.
cd /d "%~dp0"
echo Stopping poster processes...
powershell -NoProfile -Command "$root = (Get-Location).Path; Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*') -and ($_.CommandLine -like '*poster.py*' -or $_.CommandLine -like '*panel.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*Win32_Process*' -or $_.CommandLine -like '*Get-NetTCPConnection*' -or $_.CommandLine -like '*ScheduledTask*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
del /q "%TEMP%\poster-spawn-*" >nul 2>&1
echo Done. Start the bot again with run_poster.bat (or wait for autostart).
pause
