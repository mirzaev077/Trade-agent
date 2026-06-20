# =============================================================================
# OpenClaw Trading Agent — F4-2 Monthly Walk-Forward Validation
# -----------------------------------------------------------------------------
# Runs the OFFLINE validation job (walk-forward + Monte-Carlo + risk calibration)
# over a trailing window and sends a Telegram summary — ⚠️ when the walk-forward
# overfit score exceeds the threshold (default 0.6) or the verdict is REJECT.
#
# Why a scheduled job (NOT the live loop): a multi-month walk-forward re-runs the
# engine many times (minutes to ~1h). Running it inside the live MT5 tick would
# freeze trading, so analysis/offline_validation.py is designed to run here.
#
# F4-3 (reflector / SelfLearner A/B) is intentionally NOT run: the backtest engine
# uses a stub reflector and no SelfLearner, so an A/B compares identical runs →
# always NEUTRAL. It needs the reflector ported into the engine first.
#
# Requirements:
#   Windows PowerShell 5.1. Python on PATH. data/historical/ must cover the
#   window. TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env (else alert is skipped).
#
# Usage (manual):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\monthly_validation.ps1
#   powershell ... -File scripts\monthly_validation.ps1 -Start 2024-01-01 -End 2026-01-01
#
# Register as a monthly Windows scheduled task (1st of each month, 04:00):
#   $action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
#       -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\Game PC 2026\Desktop\TRD\ai-trade-agent\ai-trading-agent\scripts\monthly_validation.ps1"'
#   $trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 4 -DaysOfWeek Monday -At 4am
#   Register-ScheduledTask -TaskName 'OpenClaw-MonthlyValidation' -Action $action -Trigger $trigger
#   (Task Scheduler has no native "monthly" trigger in this cmdlet; -Weekly -WeeksInterval 4
#    approximates it. For a true day-of-month trigger use schtasks /SC MONTHLY /D 1.)
# =============================================================================

[CmdletBinding()]
param(
    # Explicit window. If omitted, a trailing -MonthsBack window ending today.
    [string]$Start = '',
    [string]$End = '',

    [ValidateRange(9, 60)]
    [int]$MonthsBack = 12,

    [double]$OverfitThreshold = 0.6,

    [string]$DataPath = 'data/historical/',
    [string]$OutDir = 'reports/validation',

    # Skip the Telegram alert (report is still written).
    [switch]$NoTelegram
)

$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent $PSScriptRoot   # scripts/.. = repo root
Set-Location $RepoRoot

if ([string]::IsNullOrWhiteSpace($End)) {
    $End = (Get-Date).ToString('yyyy-MM-dd')
}
if ([string]::IsNullOrWhiteSpace($Start)) {
    $Start = (Get-Date).AddMonths(-$MonthsBack).ToString('yyyy-MM-dd')
}

Write-Host "=== F4-2 monthly validation: $Start -> $End (overfit>$OverfitThreshold => warn) @ $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

$pyArgs = @(
    '-m', 'apps.api.src.agents.trader.analysis.offline_validation',
    '--start', $Start, '--end', $End, '--symbol', 'XAUUSD',
    '--data-path', $DataPath, '--out', $OutDir,
    '--overfit-threshold', "$OverfitThreshold"
)
if (-not $NoTelegram) { $pyArgs += '--telegram' }

& python @pyArgs
$code = $LASTEXITCODE
Write-Host "=== monthly validation done (exit $code) @ $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
exit $code
