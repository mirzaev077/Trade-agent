# =============================================================================
# OpenClaw Trading Agent - C2 Heartbeat Check (one-shot)
# -----------------------------------------------------------------------------
# Why this exists (2026-08-01): the bot was OFF for 24 days and nobody noticed.
# watchdog.ps1 is an infinite loop - it only helps while it is itself running,
# and it dies with the console / reboot. This script is a SINGLE check designed
# to be driven by Windows Task Scheduler, so it survives reboots and its own
# crashes: every run is independent, all cross-run memory lives in a state file.
#
# Checks:
#   1. /health/live endpoint  -> is the trading loop alive?
#   2. brain/trades.csv mtime -> how long since the last recorded trade?
#
# Anti-spam: alerts once when the bot goes down, then at most one reminder per
# -RemindAfterHours. Sends a recovery message when it comes back.
#
# Windows PowerShell 5.1 (no ternary, no &&/||, no null-coalescing).
# Console output is ASCII-only on purpose (cp1251 consoles mangle emoji);
# emoji appear only inside the Telegram payload, which is sent as UTF-8.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\heartbeat_check.ps1
#   ... -DryRun -Verbose          # test without sending
#   ... -RemindAfterHours 24      # quieter reminders
#
# Exit codes: 0 = bot alive, 1 = bot down (usable from other schedulers).
# =============================================================================

[CmdletBinding()]
param(
    [string]$Url = 'http://localhost:8080/health/live',

    [ValidateRange(1, 60)]
    [int]$TimeoutSec = 5,

    [string]$EnvPath = '..\.env',

    [string]$TradesCsv = '..\apps\api\src\agents\trader\brain\trades.csv',

    [string]$StateFile = '..\apps\data\state\heartbeat.json',

    # Bot down bo'lsa qayta eslatish oralig'i (soat). 0 = faqat bir marta.
    [ValidateRange(0, 168)]
    [int]$RemindAfterHours = 12,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Level,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts $Level] $Message"
    switch ($Level) {
        'DEBUG' { if ($VerbosePreference -eq 'Continue') { Write-Host $line -ForegroundColor DarkGray } }
        'INFO'  { Write-Host $line -ForegroundColor Cyan }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        'ERROR' { Write-Host $line -ForegroundColor Red }
        default { Write-Host $line }
    }
}

# Tiny .env parser (watchdog.ps1 bilan bir xil semantika - operatsion skriptlar
# ataylab self-contained: Task Scheduler kontekstida dot-source yo'llari mo'rt).
function Read-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)
    $result = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $result }
    $lines = Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue
    foreach ($raw in $lines) {
        if ($null -eq $raw) { continue }
        $line = $raw.Trim()
        if ($line.Length -eq 0) { continue }
        if ($line.StartsWith('#')) { continue }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $line.Substring(0, $eq).Trim()
        $val = $line.Substring($eq + 1).Trim()
        if ($val.Length -ge 2) {
            $first = $val[0]
            $last  = $val[$val.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $val = $val.Substring(1, $val.Length - 2)
            }
        }
        $result[$key] = $val
    }
    return $result
}

function Send-TelegramMessage {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [string]$Token,
        [string]$ChatId
    )
    if ($DryRun) {
        Write-Log -Level 'INFO' -Message "[DRYRUN] Telegram xabari yuborilmadi. Matn:"
        Write-Host $Text
        return $true
    }
    if ([string]::IsNullOrWhiteSpace($Token) -or [string]::IsNullOrWhiteSpace($ChatId)) {
        Write-Log -Level 'WARN' -Message "Telegram credentials missing - notification skipped."
        return $false
    }
    $apiUrl = "https://api.telegram.org/bot$Token/sendMessage"
    $payload = @{ chat_id = $ChatId; text = $Text; parse_mode = 'HTML' } | ConvertTo-Json -Compress
    try {
        [System.Net.ServicePointManager]::SecurityProtocol =
            [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12
        $resp = Invoke-RestMethod -Uri $apiUrl -Method Post -Body $payload `
            -ContentType 'application/json; charset=utf-8' -TimeoutSec 15 -ErrorAction Stop
        if ($resp -and $resp.ok) {
            Write-Log -Level 'INFO' -Message "Telegram alert sent."
            return $true
        }
        Write-Log -Level 'WARN' -Message "Telegram API non-ok: $($resp | ConvertTo-Json -Compress)"
        return $false
    } catch {
        Write-Log -Level 'ERROR' -Message "Telegram send failed: $($_.Exception.Message)"
        return $false
    }
}

function Invoke-HealthPoll {
    param(
        [Parameter(Mandatory = $true)][string]$TargetUrl,
        [Parameter(Mandatory = $true)][int]$Timeout
    )
    try {
        $resp = Invoke-WebRequest -Uri $TargetUrl -Method Get -TimeoutSec $Timeout `
            -UseBasicParsing -ErrorAction Stop
        $code = [int]$resp.StatusCode
        if ($code -ne 200) { return @{ ok = $false; status = $null; reason = "HTTP $code" } }
        $bodyText = $resp.Content
        if ([string]::IsNullOrWhiteSpace($bodyText)) {
            return @{ ok = $false; status = $null; reason = 'empty body' }
        }
        $parsed = $null
        try { $parsed = $bodyText | ConvertFrom-Json -ErrorAction Stop }
        catch { return @{ ok = $false; status = $null; reason = "invalid JSON" } }
        $status = $null
        if ($parsed -and ($parsed.PSObject.Properties.Name -contains 'status')) {
            $status = [string]$parsed.status
        }
        if ($status -eq 'alive' -or $status -eq 'initializing') {
            return @{ ok = $true; status = $status; reason = $null }
        }
        return @{ ok = $false; status = $status; reason = "status='$status'" }
    } catch [System.Net.WebException] {
        return @{ ok = $false; status = $null; reason = 'ulanib bo`lmadi (endpoint yopiq)' }
    } catch {
        return @{ ok = $false; status = $null; reason = "$($_.Exception.GetType().Name)" }
    }
}

# trades.csv oxirgi qatoridan savdo vaqtini o'qiydi (UTC, "yyyy-MM-dd HH:mm:ss").
# Qaytaradi: @{ found = $bool; last = [datetime]; count = [int] }
function Get-LastTradeInfo {
    param([Parameter(Mandatory = $true)][string]$Path)
    $out = @{ found = $false; last = $null; count = 0 }
    if (-not (Test-Path -LiteralPath $Path)) { return $out }
    try {
        $lines = @(Get-Content -LiteralPath $Path -ErrorAction Stop |
                   Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($lines.Count -lt 2) { return $out }          # faqat header
        $out.count = $lines.Count - 1
        $lastLine = $lines[$lines.Count - 1]
        $ts = ($lastLine -split ',')[0]
        $parsed = [datetime]::MinValue
        $okParse = [datetime]::TryParseExact(
            $ts, 'yyyy-MM-dd HH:mm:ss',
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AssumeUniversal -bor
                [System.Globalization.DateTimeStyles]::AdjustToUniversal,
            [ref]$parsed)
        if ($okParse) {
            $out.found = $true
            $out.last  = $parsed
        }
    } catch {
        Write-Log -Level 'DEBUG' -Message "trades.csv read error: $($_.Exception.Message)"
    }
    return $out
}

function Read-State {
    param([Parameter(Mandatory = $true)][string]$Path)
    $state = @{ isDown = $false; downSince = $null; lastAlertAt = $null }
    if (-not (Test-Path -LiteralPath $Path)) { return $state }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($raw)) { return $state }
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        $names = $obj.PSObject.Properties.Name
        if ($names -contains 'isDown')      { $state.isDown = [bool]$obj.isDown }
        if ($names -contains 'downSince')   { $state.downSince = $obj.downSince }
        if ($names -contains 'lastAlertAt') { $state.lastAlertAt = $obj.lastAlertAt }
    } catch {
        Write-Log -Level 'WARN' -Message "State file unreadable, starting fresh: $($_.Exception.Message)"
    }
    return $state
}

function Write-State {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$State
    )
    try {
        $dir = Split-Path -Parent $Path
        if (-not [string]::IsNullOrWhiteSpace($dir) -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        ($State | ConvertTo-Json -Compress) | Set-Content -LiteralPath $Path -Encoding utf8
    } catch {
        Write-Log -Level 'ERROR' -Message "State save failed: $($_.Exception.Message)"
    }
}

function ConvertTo-DateTimeOrNull {
    param($Value)
    if ($null -eq $Value) { return $null }
    $s = [string]$Value
    if ([string]::IsNullOrWhiteSpace($s)) { return $null }
    $parsed = [datetime]::MinValue
    $ok = [datetime]::TryParse($s, [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind, [ref]$parsed)
    if ($ok) { return $parsed }
    return $null
}

function Format-Age {
    param([Parameter(Mandatory = $true)][TimeSpan]$Span)
    $totalHours = $Span.TotalHours
    if ($totalHours -lt 1)  { return ("{0} daqiqa" -f [int][math]::Max(1, $Span.TotalMinutes)) }
    if ($totalHours -lt 48) { return ("{0} soat"   -f [int][math]::Round($totalHours)) }
    return ("{0} kun" -f [int][math]::Round($Span.TotalDays))
}

# =============================================================================
# Bootstrap
# =============================================================================
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Resolve-FromScript {
    param([Parameter(Mandatory = $true)][string]$MaybeRelative)
    if ([System.IO.Path]::IsPathRooted($MaybeRelative)) { return $MaybeRelative }
    return (Join-Path -Path $scriptDir -ChildPath $MaybeRelative)
}

$envFull    = Resolve-FromScript $EnvPath
$csvFull    = Resolve-FromScript $TradesCsv
$stateFull  = Resolve-FromScript $StateFile

$envVars  = Read-DotEnv -Path $envFull
$tgToken  = ''
$tgChatId = ''
if ($envVars.ContainsKey('TELEGRAM_BOT_TOKEN')) { $tgToken  = [string]$envVars['TELEGRAM_BOT_TOKEN'] }
if ($envVars.ContainsKey('TELEGRAM_CHAT_ID'))   { $tgChatId = [string]$envVars['TELEGRAM_CHAT_ID'] }

$now   = (Get-Date).ToUniversalTime()
$poll  = Invoke-HealthPoll -TargetUrl $Url -Timeout $TimeoutSec
$trade = Get-LastTradeInfo -Path $csvFull
$state = Read-State -Path $stateFull

# Savdo jurnali konteksti - alert ichida ko'rsatiladi (F5-5 kuzatuvi uchun).
$tradeLine = 'Savdo jurnali: topilmadi'
if ($trade.found) {
    $age = New-TimeSpan -Start $trade.last -End $now
    $tradeLine = ("Oxirgi savdo: {0} UTC ({1} oldin) | jami {2} ta" -f `
        $trade.last.ToString('yyyy-MM-dd HH:mm'), (Format-Age $age), $trade.count)
}

if ($poll.ok) {
    Write-Log -Level 'DEBUG' -Message "Bot alive (status=$($poll.status)). $tradeLine"

    if ($state.isDown) {
        $downSince = ConvertTo-DateTimeOrNull $state.downSince
        $downText = 'noma`lum'
        if ($null -ne $downSince) { $downText = Format-Age (New-TimeSpan -Start $downSince -End $now) }
        $msg = "✅ <b>OpenClaw bot tiklandi</b>`n`nO'chiq turgan vaqt: $downText`n$tradeLine"
        Write-Log -Level 'INFO' -Message "Bot recovered (down for $downText) - sending alert."
        [void](Send-TelegramMessage -Text $msg -Token $tgToken -ChatId $tgChatId)
    }

    Write-State -Path $stateFull -State @{
        isDown      = $false
        downSince   = $null
        lastAlertAt = $state.lastAlertAt
    }
    exit 0
}

# ── Bot javob bermayapti ─────────────────────────────────────────────────────
$downSince = ConvertTo-DateTimeOrNull $state.downSince
if (-not $state.isDown -or $null -eq $downSince) { $downSince = $now }

$lastAlertAt = ConvertTo-DateTimeOrNull $state.lastAlertAt
$shouldAlert = $false
$why = ''

if (-not $state.isDown) {
    $shouldAlert = $true
    $why = 'birinchi aniqlash'
} elseif ($RemindAfterHours -gt 0) {
    if ($null -eq $lastAlertAt) {
        $shouldAlert = $true
        $why = 'oldingi alert vaqti yo`q'
    } elseif ((New-TimeSpan -Start $lastAlertAt -End $now).TotalHours -ge $RemindAfterHours) {
        $shouldAlert = $true
        $why = "eslatma (${RemindAfterHours}h)"
    }
}

$downText = Format-Age (New-TimeSpan -Start $downSince -End $now)
Write-Log -Level 'WARN' -Message "Bot DOWN ($($poll.reason)), down for $downText. alert=$shouldAlert $why"

if ($shouldAlert) {
    $msg = "🚨 <b>OpenClaw bot ishlamayapti</b>`n`n" +
           "Health endpoint javob bermadi ($Url)`n" +
           "Sabab: $($poll.reason)`n" +
           "O'chiq: $downText`n$tradeLine`n`n" +
           "Yoqish uchun: <code>start_demo.bat</code>"
    $sent = Send-TelegramMessage -Text $msg -Token $tgToken -ChatId $tgChatId
    if ($sent) { $lastAlertAt = $now }
}

$newState = @{
    isDown    = $true
    downSince = $downSince.ToString('o')
    lastAlertAt = $null
}
if ($null -ne $lastAlertAt) { $newState.lastAlertAt = $lastAlertAt.ToString('o') }
Write-State -Path $stateFull -State $newState

exit 1
