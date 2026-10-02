@echo off
rem Start the GRC Flow web app on this machine (Windows).
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

rem A .venv made with a Python that has since been removed or upgraded can't run.
rem Rebuild it; your workspace in .\var is not touched.
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo The Python that .venv was built with is gone. Rebuilding .venv ...
    rmdir /s /q .venv
    if exist .venv (
      echo Couldn't remove .venv. Close any window running the app, then try again.
      goto :error
    )
  )
)

if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment in .venv ...
  %PY% -m venv .venv || goto :error
)

rem A running copy started with grc-web.exe locks that file, so an update can't install.
tasklist /FI "IMAGENAME eq grc-web.exe" 2>nul | find /I "grc-web.exe" >nul
if not errorlevel 1 (
  echo The app is already running in another window.
  echo Close that window ^(or press Ctrl+C in it^), then run start.bat again.
  echo If you can't find it: Task Manager, Details tab, end grc-web.exe.
  pause
  exit /b 1
)

fc /b pyproject.toml .venv\installed-pyproject.toml >nul 2>&1
if errorlevel 1 (
  echo Installing the app ^(first run takes a minute^) ...
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip || goto :error
  .venv\Scripts\python.exe -m pip install --quiet -e ".[postgres]" || goto :error
  copy /y pyproject.toml .venv\installed-pyproject.toml >nul
)

if not exist .env copy .env.example .env >nul

rem Run through python.exe, not grc-web.exe: the installer never replaces python.exe,
rem so a running app can't block the next update.
.venv\Scripts\python.exe -m grc_agent.web.cli init || goto :error
.venv\Scripts\python.exe -m grc_agent.web.cli serve --open %*
if errorlevel 1 pause
exit /b %errorlevel%

:error
echo Setup failed. See the messages above.
pause
exit /b 1