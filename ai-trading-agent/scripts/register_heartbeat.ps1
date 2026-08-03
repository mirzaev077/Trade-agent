# =============================================================================
# OpenClaw Trading Agent - C2: heartbeat_check.ps1 ni Task Scheduler'ga ulash
# -----------------------------------------------------------------------------
# heartbeat_check.ps1 ni har -IntervalMinutes daqiqada ishga tushiradigan
# Windows vazifasini yaratadi. Vazifa foydalanuvchi kontekstida ishlaydi -
# administrator huquqi SHART EMAS.
#
# Nega davomiy loop (watchdog.ps1) emas: loop konsol yopilishi, reboot yoki
# o'z crash'i bilan o'ladi va shundan keyin hech kim xabar bermaydi. Task
# Scheduler esa har chaqiruvni mustaqil qayta ishga tushiradi.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_heartbeat.ps1
#   ... -IntervalMinutes 15
#   ... -Unregister                 # vazifani o'chirish
#   ... -Status                     # holatini ko'rish
# =============================================================================

[CmdletBinding()]
param(
    [string]$TaskName = 'OpenClaw-Heartbeat',

    [ValidateRange(5, 1440)]
    [int]$IntervalMinutes = 30,

    # heartbeat_check.ps1 ga uzatiladi: bot o'chiq bo'lsa qayta eslatish oralig'i.
    [ValidateRange(0, 168)]
    [int]$RemindAfterHours = 12,

    [switch]$Unregister,
    [switch]$Status
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$checkScript = Join-Path -Path $scriptDir -ChildPath 'heartbeat_check.ps1'

function Get-Task {
    try { return Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
    catch { return $null }
}

# ── -Status ──────────────────────────────────────────────────────────────────
if ($Status) {
    $t = Get-Task
    if ($null -eq $t) {
        Write-Host "Vazifa ro'yxatdan o'tmagan: $TaskName" -ForegroundColor Yellow
        exit 1
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Vazifa      : $TaskName"        -ForegroundColor Cyan
    Write-Host "Holat       : $($t.State)"
    Write-Host "Oxirgi run  : $($info.LastRunTime)  (natija: $($info.LastTaskResult))"
    Write-Host "Keyingi run : $($info.NextRunTime)"
    Write-Host ""
    Write-Host "Eslatma: LastTaskResult 0 = bot sog'lom, 1 = bot o'chiq." -ForegroundColor DarkGray
    exit 0
}

# ── -Unregister ──────────────────────────────────────────────────────────────
if ($Unregister) {
    if ($null -eq (Get-Task)) {
        Write-Host "Vazifa allaqachon yo'q: $TaskName" -ForegroundColor Yellow
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "O'chirildi: $TaskName" -ForegroundColor Green
    exit 0
}

# ── Ro'yxatdan o'tkazish ─────────────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $checkScript)) {
    throw "heartbeat_check.ps1 topilmadi: $checkScript"
}

$argLine = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ' +
            '-File "{0}" -RemindAfterHours {1}' -f $checkScript, $RemindAfterHours)

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument $argLine -WorkingDirectory $scriptDir

# -Once + RepetitionInterval: cheksiz takrorlanadigan trigger.
# Boshlanish vaqti 1 daqiqa keyin - ro'yxatdan o'tgach darhol birinchi tekshiruv.
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "OpenClaw: bot /health/live tekshiruvi, o'chib qolsa Telegram alert" `
    -Force | Out-Null

Write-Host "Ro'yxatdan o'tdi: $TaskName" -ForegroundColor Green
Write-Host "  Oraliq        : har $IntervalMinutes daqiqa"
Write-Host "  Eslatma       : bot o'chiq bo'lsa har $RemindAfterHours soatda"
Write-Host "  Skript        : $checkScript"
Write-Host ""
Write-Host "Holatni ko'rish : .\scripts\register_heartbeat.ps1 -Status"
Write-Host "O'chirish       : .\scripts\register_heartbeat.ps1 -Unregister"
