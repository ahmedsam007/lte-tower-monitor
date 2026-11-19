@echo off
rem Double-click to start Tower Monitor on Windows. Close this window (or press Ctrl+C) to stop it.
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

rem First run only: make a private Python environment and install the two packages it needs.
rem Later runs skip this, so Tower Monitor still starts when the internet is down.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 is not installed. Get it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup. Then try again.
  pause
  exit /b 1
)
echo First start: installing what Tower Monitor needs (one time, needs internet)...
%PY% -m venv .venv || goto failed
".venv\Scripts\python.exe" -m pip install --retries 10 --timeout 30 --quiet --only-binary=:all: -r requirements.txt || goto failed

:run
".venv\Scripts\python.exe" server.py %*
pause
exit /b

:failed
if exist .venv rmdir /s /q .venv
echo Installing failed. Check the internet connection and try again.
pause
exit /b 1
