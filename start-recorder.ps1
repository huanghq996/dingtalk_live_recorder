[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSCommandPath
$LogDirectory = Join-Path $ProjectRoot "logs"
$StatePath = Join-Path $LogDirectory "dingtalk-live-recorder.pid.json"
$EntryPoint = Join-Path $ProjectRoot ".venv\Scripts\dingtalk-live-recorder.exe"

if (-not (Test-Path -LiteralPath $EntryPoint -PathType Leaf)) {
    throw "Missing $EntryPoint. Run python -m uv sync in the project directory first."
}

if ((-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) -or (-not (Get-Command ffprobe.exe -ErrorAction SilentlyContinue))) {
    $InstalledFfmpegBin = Join-Path $env:LOCALAPPDATA "Programs\FFmpeg\bin"
    if ((-not (Test-Path -LiteralPath (Join-Path $InstalledFfmpegBin "ffmpeg.exe") -PathType Leaf)) -or (-not (Test-Path -LiteralPath (Join-Path $InstalledFfmpegBin "ffprobe.exe") -PathType Leaf))) {
        throw "ffmpeg.exe or ffprobe.exe was not found. Install FFmpeg and add it to PATH."
    }
    $env:PATH = "$InstalledFfmpegBin;$env:PATH"
}

New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
    try {
        $State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        $ExistingProcess = Get-Process -Id ([int]$State.process_id) -ErrorAction SilentlyContinue
    }
    catch {
        throw "Cannot read existing background process state: $StatePath"
    }

    if ($null -ne $ExistingProcess) {
        $RecordedStartTime = [DateTime]::Parse(
            [string]$State.started_at_utc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
        $ActualStartTime = $ExistingProcess.StartTime.ToUniversalTime()
        if ([Math]::Abs(($ActualStartTime - $RecordedStartTime).TotalSeconds) -le 1) {
            Write-Error "The background recorder is already running with process ID $($ExistingProcess.Id)."
            exit 1
        }
        Write-Warning "Process ID $($ExistingProcess.Id) has been reused; it will not be stopped."
    }

    Remove-Item -LiteralPath $StatePath -Force
}

$Process = Start-Process -FilePath $EntryPoint -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
Start-Sleep -Milliseconds 500
if ($Process.HasExited) {
    throw "The background recorder exited during startup with code $($Process.ExitCode)."
}

@{
    process_id = $Process.Id
    started_at_utc = $Process.StartTime.ToUniversalTime().ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding utf8

Write-Output "The background recorder started with process ID $($Process.Id)."
