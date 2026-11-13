@echo off
rem Kivi UI launcher: double-click to start the local interface.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Kivi venv not found. Run setup first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m kivi ui --app --tray
if errorlevel 1 pause
