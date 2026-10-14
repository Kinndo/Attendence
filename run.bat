@echo off
cd /d "%~dp0"
py -3 bot.py %*
echo.
echo Bot exited. Press any key to close.
pause >nul
