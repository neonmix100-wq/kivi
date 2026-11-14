@echo off
rem Stop the background Kivi wake listener.
cd /d "%~dp0"
powershell -NoProfile -WindowStyle Hidden -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -like '*-m kivi listen*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
exit /b 0
