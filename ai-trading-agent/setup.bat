@echo off
echo ===================================================
echo  OpenClaw Trading Agent - Setup
echo ===================================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    where py >nul 2>&1
    if %errorlevel% neq 0 (
        echo XATO: Python topilmadi!
        echo.
        echo 1. https://www.python.org/downloads/ ga boring
        echo 2. "Download Python 3.11" bosing
        echo 3. O'rnatayotganda "Add Python to PATH" ni BELGILANG!
        echo 4. O'rnatib bo'lgach setup.bat ni qayta ishga tushiring
        pause
        exit /b
    )
    set PYTHON=py
) else (
    set PYTHON=python
)

echo Python topildi. Kutubxonalar o'rnatilmoqda...
echo.
%PYTHON% -m pip install --upgrade pip
%PYTHON% -m pip install -r requirements.txt
echo.
echo ===================================================
echo  Tayyor! Endi start.bat ni ishga tushiring.
echo ===================================================
pause
