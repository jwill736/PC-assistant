@echo off
REM Launch the assistant + HUD. Pass --no-voice or --no-window if needed.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m assistant %*
if errorlevel 1 pause
