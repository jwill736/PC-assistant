@echo off
REM Update Vesper to the newest version: closes it, downloads the new code, updates the packages.
REM config.yaml, .env and data\ are kept. Same as pasting the install line again.
cd /d "%~dp0"
set "VESPER_DIR=%~dp0."
REM One block: cmd reads it whole before running it, so replacing this file mid-update is harmless.
(
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; & ([scriptblock]::Create((Invoke-RestMethod -UseBasicParsing 'https://raw.githubusercontent.com/jwill736/PC-assistant/main/install.ps1'))) -Dir $env:VESPER_DIR"
  pause
  exit /b
)
