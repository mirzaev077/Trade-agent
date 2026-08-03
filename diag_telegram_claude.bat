@echo off
title Telegram Claude DIAG
cd /d "C:\Users\Game PC 2026\Desktop\TRD\ai-trade-agent"
where bun >nul 2>&1 || set "PATH=%USERPROFILE%\.bun\bin;%PATH%"
echo PATH bun check: & where bun
echo Starting claude --channels ... (chiqish diag_channel.log ga yoziladi)
claude --channels plugin:telegram@claude-plugins-official > "diag_channel.log" 2>&1
echo EXIT CODE: %errorlevel% >> "diag_channel.log"
echo [claude chiqdi] exit=%errorlevel%
pause
