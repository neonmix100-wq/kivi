@echo off
rem Kivi 3D avatar launcher.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Kivi venv not found. Run setup first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m kivi avatar --port 8766
if errorlevel 1 pause
