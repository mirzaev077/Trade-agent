# =============================================================================
# OpenClaw Trading Agent — F1-4 Healthcheck Watchdog
# -----------------------------------------------------------------------------
# Polls the bot's /health/live endpoint and sends a Telegram alert when the
# endpoint stops responding. Sends a recovery alert when it comes back online.
#
# Requirements:
#   Windows PowerShell 5.1 (no ternary, no &&/||, no null-coalescing).
#   TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in ../.env (or the path given
#   via -EnvPath). If missing, watchdog still polls but skips notifications.
#
# Usage:
#   powershell -NoProfile -File scripts\watchdog.ps1
#   powershell -NoProfile -File scripts\watchdog.ps1 -IntervalSec 30 -DryRun
# =============================================================================

[CmdletBinding()]
param(
    [string]$Url = 'http://localhost:8080/health/live',

    [ValidateRange(10, 600)]
    [int]$IntervalSec = 60,

    [ValidateRange(1, 20)]
    [int]$FailureThreshold = 2,

    [ValidateRange(1, 60)]
    [int]$TimeoutSec = 5,

    [string]$EnvPath = '..\.env',

    [switch]$DryRun
)

# Enforce strict mode but keep the loop running on non-terminating errors.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# -----------------------------------------------------------------------------
# Logging helper — timestamped, level-tagged, all to stdout via Write-Host.
# Levels: DEBUG (silent unless -Verbose), INFO, WARN, ERROR
# -----------------------------------------------------------------------------
function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Level,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts $Level] $Message"

    switch ($Level) {
        'DEBUG' {
            # Only show DEBUG when -Verbose is passed.
            if ($VerbosePreference -eq 'Continue') {
                Write-Host $line -ForegroundColor DarkGray
            }
        }
        'INFO'  { Write-Host $line -ForegroundColor Cyan }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        'ERROR' { Write-Host $line -ForegroundColor Red }
        default { Write-Host $line }
    }
}

# -----------------------------------------------------------------------------
# Tiny .env parser. Skips blank and `#` lines, splits on the FIRST `=`, strips
# surrounding single/double quotes. Returns a hashtable.
# -----------------------------------------------------------------------------
function Read-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    $result = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $result
    }

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

        # Strip wrapping single or double quotes.
        if ($val.Length -ge 2) {
            $first = $val[0]
            $last  = $val[$val.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or
                ($first -eq "'" -and $last -eq "'")) {
                $val = $val.Substring(1, $val.Length - 2)
            }
        }

        $result[$key] = $val
    }
    return $result
}

# -----------------------------------------------------------------------------
# Send a Telegram message. Honors -DryRun. Swallows errors so the watchdog
# loop is never killed by a Telegram outage.
# -----------------------------------------------------------------------------
function Send-TelegramMessage {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [string]$Token,
        [string]$ChatId
    )

    if ($DryRun) {
        Write-Log -Level 'INFO' -Message "[DRYRUN] Would send Telegram: $Text"
        return
    }

    if ([string]::IsNullOrWhiteSpace($Token) -or [string]::IsNullOrWhiteSpace($ChatId)) {
        Write-Log -Level 'WARN' -Message "Telegram credentials missing — skipping notification: $Text"
        return
    }

    $apiUrl = "https://api.telegram.org/bot$Token/sendMessage"
    $payload = @{
        chat_id    = $ChatId
        text       = $Text
        parse_mode = 'HTML'
    } | ConvertTo-Json -Compress

    try {
        # Force TLS 1.2 — Windows PowerShell 5.1 defaults can refuse the handshake.
        [System.Net.ServicePointManager]::SecurityProtocol =
            [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12

        $resp = Invoke-RestMethod -Uri $apiUrl `
            -Method Post `
            -Body $payload `
            -ContentType 'application/json; charset=utf-8' `
            -TimeoutSec 10 `
            -ErrorAction Stop

        if ($resp -and $resp.ok) {
            Write-Log -Level 'INFO' -Message "Telegram notification sent."
        } else {
            Write-Log -Level 'WARN' -Message "Telegram API returned non-ok: $($resp | ConvertTo-Json -Compress)"
        }
    } catch {
        Write-Log -Level 'ERROR' -Message "Telegram send failed: $($_.Exception.Message)"
    }
}

# -----------------------------------------------------------------------------
# Single poll. Returns a hashtable: @{ ok = $bool; status = $string; reason = $string }
# -----------------------------------------------------------------------------
function Invoke-HealthPoll {
    param(
        [Parameter(Mandatory = $true)][string]$TargetUrl,
        [Parameter(Mandatory = $true)][int]$Timeout
    )

    try {
        $resp = Invoke-WebRequest -Uri $TargetUrl `
            -Method Get `
            -TimeoutSec $Timeout `
            -UseBasicParsing `
            -ErrorAction Stop

        $code = [int]$resp.StatusCode

        # Treat 5xx as failure; 2xx with the right payload as success.
        if ($code -ge 500) {
            return @{ ok = $false; status = $null; reason = "HTTP $code" }
        }

        if ($code -ne 200) {
            return @{ ok = $false; status = $null; reason = "HTTP $code (expected 200)" }
        }

        $bodyText = $resp.Content
        if ([string]::IsNullOrWhiteSpace($bodyText)) {
            return @{ ok = $false; status = $null; reason = "empty body" }
        }

        $parsed = $null
        try {
            $parsed = $bodyText | ConvertFrom-Json -ErrorAction Stop
        } catch {
            return @{ ok = $false; status = $null; reason = "invalid JSON: $($_.Exception.Message)" }
        }

        $status = $null
        if ($parsed -and ($parsed.PSObject.Properties.Name -contains 'status')) {
            $status = [string]$parsed.status
        }

        if ($status -eq 'alive' -or $status -eq 'initializing') {
            return @{ ok = $true; status = $status; reason = $null }
        }

        return @{ ok = $false; status = $status; reason = "unexpected status='$status'" }
    } catch [System.Net.WebException] {
        return @{ ok = $false; status = $null; reason = "WebException: $($_.Exception.Message)" }
    } catch {
        return @{ ok = $false; status = $null; reason = "$($_.Exception.GetType().Name): $($_.Exception.Message)" }
    }
}

# =============================================================================
# Bootstrap
# =============================================================================

# Resolve the .env path relative to the script when it's not absolute.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([System.IO.Path]::IsPathRooted($EnvPath)) {
    $resolvedEnvPath = $EnvPath
} else {
    $resolvedEnvPath = Join-Path -Path $scriptDir -ChildPath $EnvPath
}

$envVars = Read-DotEnv -Path $resolvedEnvPath

$tgToken  = ''
$tgChatId = ''
if ($envVars.ContainsKey('TELEGRAM_BOT_TOKEN')) { $tgToken  = [string]$envVars['TELEGRAM_BOT_TOKEN'] }
if ($envVars.ContainsKey('TELEGRAM_CHAT_ID'))   { $tgChatId = [string]$envVars['TELEGRAM_CHAT_ID'] }

Write-Log -Level 'INFO' -Message "Watchdog started, polling $Url every ${IntervalSec}s"
Write-Log -Level 'INFO' -Message "Config: timeout=${TimeoutSec}s, failure_threshold=$FailureThreshold, env=$resolvedEnvPath"

if ($DryRun) {
    Write-Log -Level 'INFO' -Message "DRY RUN mode — Telegram messages will be logged, not sent."
}

if ([string]::IsNullOrWhiteSpace($tgToken) -or [string]::IsNullOrWhiteSpace($tgChatId)) {
    Write-Log -Level 'WARN' -Message "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing — notifications disabled."
} else {
    $maskedToken = if ($tgToken.Length -ge 6) { $tgToken.Substring(0, 4) + '…' + $tgToken.Substring($tgToken.Length - 2) } else { '***' }
    Write-Log -Level 'INFO' -Message "Telegram configured (token=$maskedToken, chat_id=$tgChatId)"
}

# =============================================================================
# State machine
# =============================================================================
# isDown          — currently in a "down" state (alert already sent)
# consecFailures  — running counter of consecutive failed polls
# downSince       — timestamp of the first failure that started the outage
# =============================================================================
$isDown         = $false
$consecFailures = 0
$downSince      = $null

try {
    while ($true) {
        $result = Invoke-HealthPoll -TargetUrl $Url -Timeout $TimeoutSec

        if ($result.ok) {
            # Healthy response.
            $statusStr = $result.status
            Write-Log -Level 'DEBUG' -Message "OK 200 (status=$statusStr)"

            if ($isDown) {
                # Recovery!
                $downtime = New-TimeSpan -Start $downSince -End (Get-Date)
                $downtimeMin = [int][math]::Round($downtime.TotalMinutes)
                if ($downtimeMin -lt 1) { $downtimeMin = 1 }

                Write-Log -Level 'INFO' -Message "Bot recovered — sending Telegram recovery alert (downtime $downtimeMin min)"
                Send-TelegramMessage `
                    -Text "✅ Bot tiklandi — downtime $downtimeMin daqiqa" `
                    -Token $tgToken `
                    -ChatId $tgChatId

                $isDown    = $false
                $downSince = $null
            }

            $consecFailures = 0
        } else {
            # Failure.
            $consecFailures++
            $reason = $result.reason

            if ($consecFailures -lt $FailureThreshold) {
                Write-Log -Level 'WARN' -Message ("Failed attempt {0}/{1}: {2}" -f $consecFailures, $FailureThreshold, $reason)
            } elseif (-not $isDown) {
                # Just crossed the threshold — first alert.
                $downSince = Get-Date
                $isDown    = $true
                $downtimeMin = 1  # at least one interval has passed across the failed attempts

                Write-Log -Level 'ERROR' -Message "Bot down — sending Telegram alert (downtime $downtimeMin min) — reason: $reason"
                Send-TelegramMessage `
                    -Text "🚨 Bot health endpoint javob bermayapti — $downtimeMin daqiqa" `
                    -Token $tgToken `
                    -ChatId $tgChatId
            } else {
                # Already in down state, keep quiet (no spam) but log it.
                $downtime = New-TimeSpan -Start $downSince -End (Get-Date)
                $downtimeMin = [int][math]::Round($downtime.TotalMinutes)
                Write-Log -Level 'WARN' -Message "Still down (downtime ${downtimeMin} min): $reason"
            }
        }

        Start-Sleep -Seconds $IntervalSec
    }
} finally {
    Write-Log -Level 'INFO' -Message "Watchdog stopped."
}
