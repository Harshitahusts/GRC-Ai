@echo off
rem Share the DEMO tenant on your office network (no outside service involved).
rem Colleagues on the same Wi-Fi/LAN open the "On your network" link it prints,
rem e.g. http://192.168.1.20:8001, and sign in as demo / grc-demo-2026.
rem If Windows Firewall asks, tick "Private networks" only and click Allow.
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo Run start.bat once first to install the app.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul

.venv\Scripts\python.exe -m grc_agent.web.cli demo --lan --open %*
if errorlevel 1 pause
exit /b %errorlevel%
