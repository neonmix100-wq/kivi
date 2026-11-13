@echo off
rem Restart Kivi and pick up the current code/version.
cd /d "%~dp0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"127.0.0.1:8765 .*LISTENING"') do (
  taskkill /PID %%p /F >nul 2>&1
)
timeout /t 1 /nobreak >nul
start "" "%~dp0Kivi UI.bat"
