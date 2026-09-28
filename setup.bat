@echo off
REM One-time setup: virtualenv, dependencies, config files.
cd /d "%~dp0"
REM Prefer the Pythons Vesper is tested on (3.12, then 3.11) when several are installed.
set "PY=python"
where py >nul 2>nul || goto :have_py
set "PY=py -3"
py -3.11 -c "" >nul 2>nul && set "PY=py -3.11"
py -3.12 -c "" >nul 2>nul && set "PY=py -3.12"
:have_py
echo Using %PY%
if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
echo Installing core packages...
pip install -r requirements.txt || goto :fail
echo Installing voice packages (speech recognition, wake words, voices)...
pip install -r requirements-voice.txt || echo Voice packages failed - the dashboard still works; see README "Voice".
if not exist config.yaml copy config.example.yaml config.yaml >nul && echo Created config.yaml - edit it.
if not exist .env copy .env.example .env >nul && echo Created .env - add your keys.
echo.
echo Done. Edit config.yaml and .env, then run start.bat
pause
exit /b 0
:fail
echo Setup failed. Install Python 3.12 from python.org (tick "Add python.exe to PATH") and retry.
pause
exit /b 1
