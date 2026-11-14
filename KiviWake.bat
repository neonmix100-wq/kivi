@echo off
rem Background Kivi voice wake. Runs without a console window.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  exit /b 1
)
start "" /b "%~dp0.venv\Scripts\pythonw.exe" -m kivi listen --mic --speak ^
  --whisper-model "data/models/faster-whisper-large-v3-turbo" --turns 100000
exit /b 0
