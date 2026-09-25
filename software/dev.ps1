param(
    [ValidateSet("all", "backend", "api", "frontend")]
    [string]$Mode = "all",

    [switch]$FeederOnly
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Stop-PortListeners {
    param([int[]]$Ports)

    foreach ($Port in $Ports) {
        $processIds = @()

        if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
            $processIds = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        }

        if (-not $processIds) {
            $netstatLines = netstat -ano | Select-String ":$Port"
            $processIds = $netstatLines |
                ForEach-Object {
                    $parts = ($_ -split "\s+").Where({ $_ })
                    if ($parts.Length -ge 5 -and $parts[3] -eq "LISTENING") {
                        $parts[4]
                    }
                } |
                Select-Object -Unique
        }

        foreach ($processId in $processIds) {
            if (-not [string]::IsNullOrWhiteSpace($processId) -and $processId -ne $PID) {
                Stop-Process -Id ([int]$processId) -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Invoke-InProjectShell {
    param(
        [string]$WorkingDirectory,
        [string]$Command
    )

    Push-Location $WorkingDirectory
    try {
        Invoke-Expression $Command
    }
    finally {
        Pop-Location
    }
}

switch ($Mode) {
    "backend" {
        Stop-PortListeners @(8000, 8001)
        $backendCommand = if ($FeederOnly) {
            '$env:LEGOSORTER_FEEDER_ONLY="1"; uv run python supervisor.py'
        }
        else {
            'Remove-Item Env:LEGOSORTER_FEEDER_ONLY -ErrorAction SilentlyContinue; uv run python supervisor.py'
        }
        Invoke-InProjectShell -WorkingDirectory (Join-Path $Root "sorter/backend") -Command $backendCommand
    }
    "api" {
        Stop-PortListeners @(8000)
        $apiCommand = if ($FeederOnly) {
            '$env:LEGOSORTER_FEEDER_ONLY="1"; uv run python api_only.py'
        }
        else {
            'Remove-Item Env:LEGOSORTER_FEEDER_ONLY -ErrorAction SilentlyContinue; uv run python api_only.py'
        }
        Invoke-InProjectShell -WorkingDirectory (Join-Path $Root "sorter/backend") -Command $apiCommand
    }
    "frontend" {
        Stop-PortListeners @(5173)
        Invoke-InProjectShell -WorkingDirectory (Join-Path $Root "sorter/frontend") -Command "npm run dev"
    }
    "all" {
        Stop-PortListeners @(8000, 8001, 5173)

        $frontendPath = Join-Path $Root "sorter/frontend"
        $backendPath = Join-Path $Root "sorter/backend"
        $backendEnvCommand = if ($FeederOnly) {
            '$env:LEGOSORTER_FEEDER_ONLY="1"; '
        }
        else {
            'Remove-Item Env:LEGOSORTER_FEEDER_ONLY -ErrorAction SilentlyContinue; '
        }

        Start-Process powershell -ArgumentList @(
            "-NoExit",
            "-Command",
            "Set-Location '$frontendPath'; npm run dev"
        ) | Out-Null

        Start-Process powershell -ArgumentList @(
            "-NoExit",
            "-Command",
            ($backendEnvCommand + "Set-Location '$backendPath'; uv run python supervisor.py")
        ) | Out-Null

        if ($FeederOnly) {
            Write-Host "Started frontend and backend in separate PowerShell windows (feeder-only mode)."
        }
        else {
            Write-Host "Started frontend and backend in separate PowerShell windows."
        }
    }
}