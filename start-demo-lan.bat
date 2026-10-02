@echo off
rem Share the DEMO tenant on your office network (no outside service involved).
rem Colleagues on the same Wi-Fi/LAN open the "On your network" link it prints,
rem e.g. https://192.168.1.20:8001, and sign in as demo / grc-demo-2026.
rem The link is HTTPS with a self-signed certificate: browsers warn once, choose
rem Advanced > Proceed. Traffic, including passwords, is encrypted.
rem If Windows Firewall asks, tick "Private networks" only and click Allow.
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo Run start.bat once first to install the app.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo The Python that .venv was built with is gone. Run start.bat to rebuild it.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul

.venv\Scripts\python.exe -m grc_agent.web.cli demo --lan --https --open %*
if errorlevel 1 pause
exit /b %errorlevel%
