[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSCommandPath
$StatePath = Join-Path $ProjectRoot "logs\dingtalk-live-recorder.pid.json"

if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    Write-Output "The background recorder is not running."
    exit 0
}

try {
    $State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $Process = Get-Process -Id ([int]$State.process_id) -ErrorAction SilentlyContinue
}
catch {
    throw "Cannot read background process state: $StatePath"
}

if ($null -eq $Process) {
    Remove-Item -LiteralPath $StatePath -Force
    Write-Output "The background recorder was already stopped; the stale state file was removed."
    exit 0
}

$RecordedStartTime = [DateTime]::Parse(
    [string]$State.started_at_utc,
    [Globalization.CultureInfo]::InvariantCulture,
    [Globalization.DateTimeStyles]::RoundtripKind
)
$ActualStartTime = $Process.StartTime.ToUniversalTime()
if ([Math]::Abs(($ActualStartTime - $RecordedStartTime).TotalSeconds) -gt 1) {
    Remove-Item -LiteralPath $StatePath -Force
    throw "Process ID $($Process.Id) has been reused; it was not terminated."
}

$TaskKill = Join-Path $env:SystemRoot "System32\taskkill.exe"
& $TaskKill /PID $Process.Id /T /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Failed to terminate the background recorder; taskkill exit code: $LASTEXITCODE"
}

Remove-Item -LiteralPath $StatePath -Force
Write-Output "The background recorder stopped; process ID: $($Process.Id)"
