@echo off
rem Start the DEMO tenant: sample clients and data on http://127.0.0.1:8001.
rem It lives in .\var-demo, separate from your real workspace in .\var.
rem Sign in as demo / grc-demo-2026. Use "start-demo.bat --reset" for fresh sample data.
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo Run start.bat once first to install the app.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul

.venv\Scripts\python.exe -m grc_agent.web.cli demo --open %*
if errorlevel 1 pause
exit /b %errorlevel%
