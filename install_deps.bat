@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ==================================================================
REM Ram Gobbler - Robust Dependency Installer (Windows 10/11)
REM - Works with Python 3.9+ (including 3.13)
REM - Creates .venv and installs deps WITHOUT 'activate.bat'
REM - Verifies imports; falls back to --user installs for pythonw
REM - Generates run_app.bat that uses pythonw from the venv
REM Usage:
REM   install_deps.bat
REM   install_deps.bat --run   (optionally launches the app after install)
REM ==================================================================

cd /d "%~dp0"

set "HERE=%cd%"
set "VENV_DIR=%HERE%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"

REM ---- Locate Python launcher -------------------------------------------------
set "PYCMD="
where py >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
  where python >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
  echo [ERROR] Python 3.x not found on PATH.
  echo         Install Python from https://www.python.org/downloads/windows/
  exit /b 1
)

for /f "tokens=1,2*" %%A in ('%PYCMD% -V 2^>^&1') do set "PYVER=%%B %%C"
echo Using: %PYCMD%  (Python %PYVER%)

REM ---- Create venv if missing -----------------------------------------------
if not exist "%VENV_PY%" (
  echo Creating virtual environment in ".venv" ...
  %PYCMD% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [WARN] venv creation reported an error. Trying ensurepip then venv again...
    %PYCMD% -m ensurepip --upgrade
    %PYCMD% -m venv "%VENV_DIR%"
  )
)

if not exist "%VENV_PY%" (
  echo [WARN] .venv\Scripts\python.exe not found after venv attempt.
  echo Trying 'virtualenv' fallback...
  %PYCMD% -m pip install --user virtualenv
  %PYCMD% -m virtualenv "%VENV_DIR%"
)

if not exist "%VENV_PY%" (
  echo [ERROR] Could not create the virtual environment at "%VENV_DIR%".
  echo Common causes:
  echo  - Insufficient permissions in this folder
  echo  - Antivirus locking files
  echo  - Python installation is damaged
  echo Try running this script from a normal folder like C:\Users\YOURNAME\RamGobbler
  exit /b 1
)

REM ---- Upgrade pip inside venv ----------------------------------------------
echo Upgrading pip, setuptools, wheel in the venv ...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel

REM ---- Install from requirements.txt if present ------------------------------
if exist "requirements.txt" (
  echo Installing requirements.txt into the venv ...
  "%VENV_PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [WARN] requirements.txt installation had errors. Continuing with core packages...
  )
) else (
  echo requirements.txt not found. Installing core packages directly...
)

REM ---- Ensure core packages (PySide6, psutil, pywin32) ----------------------
echo Ensuring core packages in the venv ...
"%VENV_PY%" -m pip install --upgrade --upgrade-strategy eager PySide6 psutil pywin32
if errorlevel 1 (
  echo [WARN] Bulk install failed, retrying one-by-one...
  "%VENV_PY%" -m pip install --upgrade PySide6
  "%VENV_PY%" -m pip install --upgrade psutil
  "%VENV_PY%" -m pip install --upgrade pywin32
)

REM ---- Import check inside venv ---------------------------------------------
echo Verifying imports in the venv ...
"%VENV_PY%" - <<PYCODE
import sys
missing = []
for m in ("PySide6", "psutil"):
    try: __import__(m)
    except Exception: missing.append(m)
try:
    import importlib; importlib.import_module("win32api")
except Exception:
    missing.append("pywin32")
print("OK" if not missing else "MISSING:" + ",".join(missing))
PYCODE
for /f "delims=" %%L in ('"%VENV_PY%" - <<^PYCODE 2^>NUL ^| findstr /R /C:"OK" /C:"MISSING:" ^& exit ^& :PYCODE') do set "RESULT=%%L"

if /i not "%RESULT%"=="OK" (
  echo Import check result: %RESULT%
  echo Attempting a fallback --user install so pythonw double-clicks still work...
  %PYCMD% -m pip install --user psutil pywin32
)

REM ---- Generate run_app.bat that uses pythonw from the venv ------------------
(
  echo @echo off
  echo cd /d "%%~dp0"
  echo if not exist ".venv\Scripts\pythonw.exe" (
  echo   echo [ERROR] .venv not found. Run install_deps.bat first.
  echo   exit /b 1
  echo )
  echo if exist "ram_gobbler.py" (
  echo   start "" ".venv\Scripts\pythonw.exe" "ram_gobbler.py"
  echo ) else (
  echo   echo Could not find ram_gobbler.py in this folder.
  echo )
) > "run_app.bat"

echo Created run_app.bat (launches with pythonw from the venv).

echo.
echo [OK] Dependencies installed into .venv
echo.
echo To launch the app:
echo   run_app.bat
echo or:
echo   ".venv\Scripts\python.exe" ram_gobbler.py

if /i "%~1"=="--run" (
  if exist "ram_gobbler.py" (
    echo Launching Ram Gobbler...
    start "" "%VENV_PYW%" "ram_gobbler.py"
  )
)

endlocal
