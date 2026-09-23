@echo off
rem Start the GRC agent web app on this machine (Windows).
rem First run: creates .venv, installs the app, and asks you to create an account.
rem Extra arguments go to "grc-web serve", e.g. start.bat --port 9000
setlocal
cd /d "%~dp0"

set "PY=py -3"
%PY% --version >nul 2>&1 || set "PY=python"
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 (
  echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment in .venv ...
  %PY% -m venv .venv || goto :error
)

fc /b pyproject.toml .venv\installed-pyproject.toml >nul 2>&1
if errorlevel 1 (
  echo Installing the app ^(first run takes a minute^) ...
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip || goto :error
  .venv\Scripts\python.exe -m pip install --quiet -e . || goto :error
  copy /y pyproject.toml .venv\installed-pyproject.toml >nul
)

if not exist .env copy .env.example .env >nul

.venv\Scripts\grc-web.exe init || goto :error
.venv\Scripts\grc-web.exe serve --open %*
exit /b %errorlevel%

:error
echo Setup failed. See the messages above.
pause
exit /b 1