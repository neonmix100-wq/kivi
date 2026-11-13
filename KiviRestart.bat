@echo off
rem Restart Kivi cleanly: stop old servers, close only the Kivi app window,
rem then start the current build on the first free local port.
cd /d "%~dp0"

for %%P in (8765 8766 8767) do (
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"127.0.0.1:%%P .*LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
  )
)

powershell -NoProfile -WindowStyle Hidden -Command ^
  "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'msedge|chrome|browser' -and ($_.CommandLine -like '*Kivi*app-profile*' -or $_.CommandLine -like '*--app=http://127.0.0.1:87*') }; $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-Process msedge,chrome,browser -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -match '^Kivi' } | ForEach-Object { $_.CloseMainWindow() | Out-Null }"
ping -n 2 127.0.0.1 >nul

if not exist ".venv\Scripts\pythonw.exe" (
  echo Kivi pythonw.exe not found. Run setup first.
  exit /b 1
)

set "PORT="
for /f %%p in ('powershell -NoProfile -WindowStyle Hidden -Command "$p=8765; while((Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)){$p++}; $p"') do set "PORT=%%p"
if not defined PORT set "PORT=8767"
start "" /b "%~dp0.venv\Scripts\pythonw.exe" -m kivi ui --port %PORT% --new-instance --app --tray
exit /b 0
