@echo off
cd /d "%~dp0"
:again
py -3 bot.py %*
set code=%errorlevel%
if "%code%"=="0" goto done
echo.
echo Bot exited with code %code%. See %LOCALAPPDATA%\AttendanceBot\logs\bot.log for details.
echo Restarting in 15 seconds. Close this window to stop.
timeout /t 15 >nul
goto again
:done
echo.
echo Bot exited. Press any key to close.
pause >nul
