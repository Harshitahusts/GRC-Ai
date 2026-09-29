@echo off
rem Check this machine's setup for the GRC agent: which Python and Node are installed,
rem which Python start.bat will use, and whether .venv still works. Changes nothing.
setlocal
cd /d "%~dp0"

echo.
echo === Python (the app needs 3.10 or newer) ===
where py >nul 2>&1
if errorlevel 1 goto :no_launcher
echo Installed Pythons, from the "py" launcher:
py -0p
goto :python_on_path
:no_launcher
echo The "py" launcher isn't installed. That's fine if "python" below works.

:python_on_path
echo.
where python >nul 2>&1
if errorlevel 1 goto :no_python
echo "python" on PATH:
where python
python --version 2>nul
if errorlevel 1 echo   ^^ that "python" doesn't run. If it's under WindowsApps, it's the Microsoft Store shortcut: turn it off in Settings, Apps, Advanced app settings, App execution aliases.
goto :chosen
:no_python
echo No "python" on PATH.

:chosen
echo.
set "PY=py -3"
%PY% --version >nul 2>&1 || set "PY=python"
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto :bad_python
echo start.bat will use:
%PY% -c "import sys; print('  ' + sys.version.split()[0] + '  ' + sys.executable)"
goto :venv
:bad_python
echo start.bat can't find a working Python 3.10 or newer.
echo   Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".

:venv
echo.
echo === The app's .venv ===
if not exist .venv\Scripts\python.exe goto :no_venv
.venv\Scripts\python.exe -c "import sys; print('  works: Python ' + sys.version.split()[0])" 2>nul
if errorlevel 1 goto :broken_venv
findstr /b /c:"home" .venv\pyvenv.cfg
goto :node
:broken_venv
echo   BROKEN: it was built with a Python that is no longer there.
findstr /b /c:"home" .venv\pyvenv.cfg
echo   Fix: run start.bat, which rebuilds it. Your data in .\var is kept.
goto :node
:no_venv
echo   Not created yet. start.bat creates it on the first run.

:node
echo.
echo === Node.js (optional: the app doesn't need it) ===
where node >nul 2>&1
if errorlevel 1 goto :no_node
where node
node --version
call npm --version >nul 2>&1
if errorlevel 1 (echo   npm: not found) else (for /f %%v in ('npm --version') do echo   npm %%v)
goto :done
:no_node
echo   Not installed.

:done
echo.
echo Python runs the app (start.bat). Node, if installed, sits alongside it and
echo doesn't change how the app starts.
echo.
pause
