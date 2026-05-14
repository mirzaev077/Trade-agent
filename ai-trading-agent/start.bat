@echo off
echo OpenClaw Trading Agent - Starting...
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    where py >nul 2>&1
    if %errorlevel% neq 0 (
        echo.
        echo XATO: Python topilmadi!
        echo Python.org dan yuklab o'rnating: https://www.python.org/downloads/
        echo O'rnatayotganda "Add Python to PATH" ni belgilang!
        pause
        exit /b
    )
    py apps\api\main.py
) else (
    python apps\api\main.py
)
pause
