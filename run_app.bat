@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo [ERROR] .venv not found. Run install_deps.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "ram_gobbler.py"
