@echo off
REM =============================================================================
REM OpenClaw Trading Agent — DEMO launcher
REM Starts the agent (apps\api\main.py) and the healthcheck watchdog together,
REM each in its own window. The agent serves /health/live on :8080; the watchdog
REM polls it and sends Telegram down/recovery alerts.
REM
REM Prereqs: .env configured (run start.bat once if missing — it seeds from
REM .env.example). Use a DEMO MT5 account/server for MT5_SERVER.
REM =============================================================================
echo OpenClaw Trading Agent - DEMO mode
cd /d "%~dp0"

if not exist ".env" (
    echo XATO: .env topilmadi.
    echo Avval start.bat ni ishga tushiring ^(.env.example dan nusxa oladi^), keyin sozlang.
    pause
    exit /b 1
)

echo [1/2] Agent ishga tushmoqda (alohida oyna)...
start "OpenClaw Agent" cmd /k "python apps\api\main.py"

echo [2/2] Watchdog 5s dan keyin (health server ko'tarilishini kutib)...
timeout /t 5 /nobreak >nul
start "OpenClaw Watchdog" powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\watchdog.ps1"

echo.
echo ==============================================================
echo  Ikkalasi alohida oynalarda ishlayapti:
echo    - "OpenClaw Agent"    : trade loop + Telegram (kunlik/haftalik hisobot)
echo    - "OpenClaw Watchdog" : /health/live monitoring (localhost:8080)
echo  To'xtatish: har oynada Ctrl+C, yoki oynani yoping.
echo ==============================================================
