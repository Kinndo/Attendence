@echo off
setlocal
cd /d "%~dp0"
echo === Attendance bot setup ===
py -3 --version >nul 2>&1
if errorlevel 1 (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)
py -3 -m pip install --upgrade pip
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
  echo pip install failed. See the message above.
  pause
  exit /b 1
)
if not exist config.json (
  copy config.example.json config.json >nul
  echo Created config.json from the example. Open it in Notepad and fill in the two URLs.
)
echo.
echo Setup finished. Next: edit config.json, then double-click run.bat
pause
