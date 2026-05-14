@echo off
echo OpenClaw Trading Agent - Starting...
cd /d "%~dp0"

REM ── F1-3: .env fallback ──────────────────────────────────────
if not exist ".env" (
    if exist ".env.example" (
        echo.
        echo OGOHLANTIRISH: .env fayli topilmadi.
        echo .env.example dan nusxa olinmoqda...
        copy ".env.example" ".env" >nul
        echo.
        echo ===============================================================
        echo MUHIM: .env faylini oching va sozlang:
        echo   1. MT5_LOGIN, MT5_PASSWORD, MT5_SERVER (broker akkountdan)
        echo   2. CLAUDE_API_KEY (https://console.anthropic.com)
        echo   3. TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (ixtiyoriy notify)
        echo.
        echo Sozlash tugagandan keyin start.bat ni qayta ishga tushiring.
        echo ===============================================================
        pause
        exit /b 1
    ) else (
        echo XATO: .env va .env.example fayllari topilmadi!
        echo Repo'ni to'liq clone qildingizmi?
        pause
        exit /b 1
    )
)

REM ── Python topish va botni ishga tushirish ───────────────────
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
