param(
    [ValidateSet("all", "backend", "api", "frontend")]
    [string]$Mode = "all",

    [switch]$FeederOnly
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Ensure-UvAvailable {
        $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
        if ($uvCommand) {
                return
        }

        throw @"
uv is required to run the sorter backend.

Install it, open a fresh PowerShell window, then run this command again:
    winget install Astral-sh.uv

If winget is unavailable, use:
    powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
"@
}

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

function Ensure-FrontendDependencies {
    param([string]$FrontendPath)

    $viteShim = Join-Path $FrontendPath "node_modules/.bin/vite.cmd"
    if (Test-Path $viteShim) {
        return
    }

    Write-Host "Frontend dependencies missing; running npm install..."
    Invoke-InProjectShell -WorkingDirectory $FrontendPath -Command "npm install"
}

function Test-BackendImports {
    param([string]$BackendPath)

    Push-Location $BackendPath
    try {
        $null = & uv run python -c "import cv2, fastapi, serial, onnxruntime" 2>$null
        return $LASTEXITCODE -eq 0
    }
    finally {
        Pop-Location
    }
}

function Ensure-BackendDependencies {
    param([string]$BackendPath)

    Ensure-UvAvailable

    if (Test-BackendImports -BackendPath $BackendPath) {
        return
    }

    Write-Host "Backend dependencies missing or incomplete; running uv sync..."
    Invoke-InProjectShell -WorkingDirectory $BackendPath -Command "uv sync --locked"

    if (-not (Test-BackendImports -BackendPath $BackendPath)) {
        throw "Backend dependency check still failed after uv sync. Try 'cd sorter/backend; uv run python -c \"import cv2, fastapi, serial, onnxruntime\"' to see the exact import error."
    }
}

switch ($Mode) {
    "backend" {
        Stop-PortListeners @(8000, 8001)
        $backendPath = Join-Path $Root "sorter/backend"
        Ensure-BackendDependencies -BackendPath $backendPath
        $backendCommand = if ($FeederOnly) {
            '$env:LEGOSORTER_FEEDER_ONLY="1"; uv run python supervisor.py'
        }
        else {
            'Remove-Item Env:LEGOSORTER_FEEDER_ONLY -ErrorAction SilentlyContinue; uv run python supervisor.py'
        }
        Invoke-InProjectShell -WorkingDirectory $backendPath -Command $backendCommand
    }
    "api" {
        Stop-PortListeners @(8000)
        $backendPath = Join-Path $Root "sorter/backend"
        Ensure-BackendDependencies -BackendPath $backendPath
        $apiCommand = if ($FeederOnly) {
            '$env:LEGOSORTER_FEEDER_ONLY="1"; uv run python api_only.py'
        }
        else {
            'Remove-Item Env:LEGOSORTER_FEEDER_ONLY -ErrorAction SilentlyContinue; uv run python api_only.py'
        }
        Invoke-InProjectShell -WorkingDirectory $backendPath -Command $apiCommand
    }
    "frontend" {
        Stop-PortListeners @(5173)
        Ensure-FrontendDependencies -FrontendPath (Join-Path $Root "sorter/frontend")
        Invoke-InProjectShell -WorkingDirectory (Join-Path $Root "sorter/frontend") -Command "npm run dev"
    }
    "all" {
        Stop-PortListeners @(8000, 8001, 5173)

        $frontendPath = Join-Path $Root "sorter/frontend"
        $backendPath = Join-Path $Root "sorter/backend"
        Ensure-FrontendDependencies -FrontendPath $frontendPath
        Ensure-BackendDependencies -BackendPath $backendPath
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