@echo off
rem Double-click to start Tower Monitor on Windows. Close this window (or press Ctrl+C) to stop it.
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

rem First run only: make a private Python environment and install the packages it needs.
rem Later runs skip this, so Tower Monitor still starts when the internet is down.

rem Pick a normal Python 3.8 or newer. Free-threaded builds (3.13t and similar) are skipped:
rem the packages have no ready-made files for them, so pip fails with "conflicting dependencies".
rem Also look in the usual install folders, for a Python that is installed but not on PATH.
set "PY="
for %%V in (3 3.14 3.13 3.12 3.11 3.10 3.9 3.8) do if not defined PY call :trypy py -%%V
if not defined PY call :trypy python
if not defined PY call :trypy python3
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "%LOCALAPPDATA%\Python\pythoncore-3*") do if not defined PY if exist "%%~D\python.exe" call :trypy "%%~D\python.exe"
if not defined PY goto nopython

echo First start: installing what Tower Monitor needs (one time, needs internet)...
%PY% --version
%PY% -m venv .venv || goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --retries 10 --timeout 30 --only-binary=:all: -r requirements.txt || goto failed

:run
".venv\Scripts\python.exe" app.py --browser %*
pause
exit /b

:trypy
%* -c "import sys, sysconfig; sys.exit(0 if sys.version_info >= (3, 8) and not sysconfig.get_config_var('Py_GIL_DISABLED') else 1)" >nul 2>nul && set "PY=%*"
exit /b

:nopython
echo No usable Python was found. This is what the computer has:
ver
echo [py launcher]
py -0p 2>&1
echo [python]
python --version 2>&1
echo [python3]
python3 --version 2>&1
echo.
echo Tower Monitor needs a normal Python 3.8 or newer, not a "free-threaded" one like 3.13t.
echo Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" on the first screen of the installer.
echo Then close this window and start start-windows.bat again.
echo If it still fails, send a screenshot of this window.
pause
exit /b 1

:failed
if exist .venv rmdir /s /q .venv
echo.
echo Installing failed. The lines above say why.
echo If they mention a connection or timeout, check the internet connection and try again.
echo Otherwise open an issue on GitHub with a screenshot of this window.
pause
exit /b 1
