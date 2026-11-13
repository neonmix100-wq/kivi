@echo off
rem Restart Kivi cleanly: stop the server, close only the Kivi app window,
rem then start the new build without leaving a console window.
cd /d "%~dp0"

for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"127.0.0.1:8765 .*LISTENING"') do (
  taskkill /PID %%p /F >nul 2>&1
)

powershell -NoProfile -WindowStyle Hidden -Command ^
  "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'msedge|chrome|browser' -and ($_.CommandLine -like '*Kivi*app-profile*' -or $_.CommandLine -like '*--app=http://127.0.0.1:8765*') }; $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-Process msedge,chrome,browser -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -match '^Kivi' } | ForEach-Object { $_.CloseMainWindow() | Out-Null }"
ping -n 2 127.0.0.1 >nul

if not exist ".venv\Scripts\pythonw.exe" (
  echo Kivi pythonw.exe not found. Run setup first.
  exit /b 1
)
set "PORT=8765"
netstat -ano | findstr /R /C:"127.0.0.1:8765 .*LISTENING" >nul
if not errorlevel 1 set "PORT=8766"
start "" /b "%~dp0.venv\Scripts\pythonw.exe" -m kivi ui --port %PORT% --app --tray
exit /b 0
