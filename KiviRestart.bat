@echo off
rem Restart Kivi and pick up the current code/version.
cd /d "%~dp0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"127.0.0.1:8765 .*LISTENING"') do (
  taskkill /PID %%p /F >nul 2>&1
)
ping -n 2 127.0.0.1 >nul
start "" "%~dp0Kivi UI.bat"
