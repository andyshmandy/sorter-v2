#Requires -Version 5.1
<#
.SYNOPSIS
    Unified dev runner / watchdog for the LEGO sorter on Windows.
    PowerShell equivalent of dev.sh.

.DESCRIPTION
    Starts the full machine client backend + frontend (vite) with:
      - Color-coded, prefixed log output
      - Auto-restart on crash (with backoff)
      - Graceful shutdown on Ctrl+C

.PARAMETER Mode
    all (default) | backend | api | frontend

.PARAMETER FeederOnly
    Sets LEGOSORTER_FEEDER_ONLY=1 for the backend process (skips distribution
    board / chute / servo init — see AGENTS notes on feeder-only mode).

.EXAMPLE
    ./dev.ps1                  # start both backend and frontend (separate windows)
    ./dev.ps1 backend          # full machine backend only, this window
    ./dev.ps1 api              # API-only backend (no controller / hardware)
    ./dev.ps1 frontend         # frontend only
    ./dev.ps1 backend -FeederOnly

.NOTES
    Run this shell as Administrator to access serial ports (Pico USB CDC).
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("all", "backend", "api", "frontend")]
    [string]$Mode = "all",

    [switch]$Dump,
    [switch]$FeederOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-DevLog {
    param([string]$Message, [ConsoleColor]$Color = [ConsoleColor]::Gray)
    Write-Host "[dev] $Message" -ForegroundColor $Color
}

function Import-DotEnv {
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) { return }
    foreach ($line in Get-Content $envFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $trimmed = $trimmed -replace '^export\s+', ''
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        $value = $value.Trim('"').Trim("'")
        [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
}

function Stop-DevPort {
    param([int]$Port)
    $procIds = @()
    try {
        $procIds = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    } catch {
        # Get-NetTCPConnection isn't available on every Windows SKU; fall back to netstat.
        $lines = (netstat -ano -p TCP 2>$null) | Select-String ":$Port\s+.*LISTENING"
        $procIds = $lines | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -Unique
    }
    foreach ($procId in $procIds) {
        if (-not $procId) { continue }
        Write-DevLog "Killing stale process on port $Port (pid $procId)" ([ConsoleColor]::Yellow)
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    if ($procIds) { Start-Sleep -Milliseconds 500 }
}

$script:Shutdown = $false
$script:ChildProcesses = New-Object System.Collections.Generic.List[System.Diagnostics.Process]

function Invoke-DevWatched {
    param(
        [string]$Name,
        [ConsoleColor]$Color,
        [string]$WorkingDirectory,
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    $attempt = 0
    while (-not $script:Shutdown) {
        $attempt++
        if ($attempt -gt 1) {
            $delay = [Math]::Min($attempt, 10)
            Write-DevLog "$Name crashed. Restarting in ${delay}s (attempt $attempt)..." ([ConsoleColor]::Yellow)
            Start-Sleep -Seconds $delay
        }
        Write-DevLog "Starting $Name..." $Color
        $proc = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
            -WorkingDirectory $WorkingDirectory -NoNewWindow -PassThru
        $script:ChildProcesses.Add($proc)
        Wait-Process -Id $proc.Id -ErrorAction SilentlyContinue
        $script:ChildProcesses.Remove($proc) | Out-Null
        if ($script:Shutdown) { break }
    }
}

function Stop-AllChildren {
    foreach ($proc in @($script:ChildProcesses)) {
        try {
            if (-not $proc.HasExited) {
                Write-DevLog "Stopping PID $($proc.Id)" ([ConsoleColor]::DarkGray)
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            }
        } catch { }
    }
}

Import-DotEnv
if ($FeederOnly) { $env:LEGOSORTER_FEEDER_ONLY = "1" }

if ($Dump) {
    $logDir = Join-Path $Root "logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $logFile = Join-Path $logDir ((Get-Date -Format "yyyy-MM-dd_HH-mm-ss") + ".log")
    Start-Transcript -Path $logFile -Append | Out-Null
    Write-DevLog "Dumping logs to $logFile" ([ConsoleColor]::Cyan)
}

Write-DevLog "LEGO Sorter Dev Runner (Windows)" ([ConsoleColor]::Cyan)
Write-DevLog "Mode: $Mode"
Write-DevLog "Run this shell as Administrator to access serial ports." ([ConsoleColor]::DarkGray)

$apiHost = if ($env:SORTER_API_HOST) { $env:SORTER_API_HOST } else { "127.0.0.1" }

try {
    switch ($Mode) {
        "backend" {
            Stop-DevPort -Port 8000
            Invoke-DevWatched -Name "backend" -Color ([ConsoleColor]::Green) `
                -WorkingDirectory (Join-Path $Root "sorter\backend") `
                -FilePath "uv" -ArgumentList @("run", "python", "supervisor.py", "--ui-port", "0")
        }
        "api" {
            Stop-DevPort -Port 8000
            Invoke-DevWatched -Name "api" -Color ([ConsoleColor]::Green) `
                -WorkingDirectory (Join-Path $Root "sorter\backend") `
                -FilePath "uv" -ArgumentList @("run", "uvicorn", "server.api:app", "--host", $apiHost, "--port", "8000")
        }
        "frontend" {
            Stop-DevPort -Port 5173
            Invoke-DevWatched -Name "frontend" -Color ([ConsoleColor]::Blue) `
                -WorkingDirectory (Join-Path $Root "sorter\frontend") `
                -FilePath "pnpm" -ArgumentList @("dev")
        }
        default {
            # Running backend and frontend watchdogs concurrently in one
            # console (with interleaved prefixed output) isn't practical in
            # Windows PowerShell without extra dependencies, so "all" opens
            # each in its own window instead — equivalent to the two terminal
            # tabs the README describes for manual startup.
            Stop-DevPort -Port 8000
            Stop-DevPort -Port 5173
            Write-DevLog "Opening backend and frontend in separate windows..." ([ConsoleColor]::Cyan)
            $backendWindow = Start-Process -FilePath "powershell" -ArgumentList @(
                "-NoExit", "-Command", "& '$($MyInvocation.MyCommand.Path)' -Mode backend $(if ($FeederOnly) { '-FeederOnly' })"
            ) -PassThru
            $frontendWindow = Start-Process -FilePath "powershell" -ArgumentList @(
                "-NoExit", "-Command", "& '$($MyInvocation.MyCommand.Path)' -Mode frontend"
            ) -PassThru
            $script:ChildProcesses.Add($backendWindow)
            $script:ChildProcesses.Add($frontendWindow)
            Write-DevLog "Backend window PID $($backendWindow.Id), frontend window PID $($frontendWindow.Id)." ([ConsoleColor]::DarkGray)
            Write-DevLog "Press Ctrl+C here to close both." ([ConsoleColor]::DarkGray)
            while (-not $backendWindow.HasExited -and -not $frontendWindow.HasExited) {
                Start-Sleep -Seconds 1
            }
        }
    }
}
finally {
    $script:Shutdown = $true
    Stop-AllChildren
    if ($Dump) { Stop-Transcript | Out-Null }
    Write-DevLog "Done."
}
