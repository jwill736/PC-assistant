@echo off
REM One-time setup: virtualenv, dependencies, config files.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
echo Installing core packages...
pip install -r requirements.txt || goto :fail
echo Installing voice packages (Whisper, mic, speech)...
pip install -r requirements-voice.txt || echo Voice packages failed - the dashboard still works; see README "Voice".
if not exist config.yaml copy config.example.yaml config.yaml >nul && echo Created config.yaml - edit it.
if not exist .env copy .env.example .env >nul && echo Created .env - add your keys.
echo.
echo Done. Edit config.yaml and .env, then run start.bat
pause
exit /b 0
:fail
echo Setup failed. Install Python 3.11+ from python.org (tick "Add to PATH") and retry.
pause
exit /b 1
