@echo off
setlocal

echo ai-relay setup
echo ==============
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on this PC.
    echo Install it from https://www.python.org/downloads/
    echo During install, check the box that says "Add Python to PATH".
    echo Then run this file again.
    echo.
    pause
    exit /b 1
)

echo Installing required packages ^(only takes a moment, and only happens once^)...
python -m pip install --user -q -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo Package install failed -- see the error above.
    pause
    exit /b 1
)

echo.
echo Starting the dashboard... a browser tab will open in a moment.
echo Keep this window open -- closing it stops your tunnels.
echo.
python "%~dp0dashboard.py"
pause
