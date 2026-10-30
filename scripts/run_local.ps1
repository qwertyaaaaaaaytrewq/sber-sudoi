param(
    [string]$Python = "python",
    [string]$PythonVersionArgs = "",
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 8085,
    [switch]$KeepRunning,
    [switch]$RunSmokeTest
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([^#=\s]+)\s*=(.*)$') {
            $name = $matches[1]
            $value = $matches[2].Trim()
            if (-not [Environment]::GetEnvironmentVariable($name)) {
                Set-Item -Path "Env:$name" -Value $value
            }
        }
    }
}

$dataDir = Join-Path $root "data"
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
}

if (-not $env:HERMES_BASE_URL) { $env:HERMES_BASE_URL = "http://127.0.0.1:8642/v1" }
if (-not $env:HERMES_API_MODEL) { $env:HERMES_API_MODEL = "hermes-agent" }
if (-not $env:HERMES_MODEL) { $env:HERMES_MODEL = "pvx-ai-test:latest" }
$env:DATA_DIR = $dataDir
if (-not $env:HERMES_CALLBACK_BASE_URL -or $env:HERMES_CALLBACK_BASE_URL -eq "http://127.0.0.1:8000/api") { $env:HERMES_CALLBACK_BASE_URL = "http://127.0.0.1:$BackendPort/api" }
$env:CORS_ORIGINS = "*"
$env:PYTHONUNBUFFERED = "1"

$services = @(
    @{ Name = "backend"; Dir = Join-Path $root "backend"; Port = $BackendPort }
)

$processes = @()
$pythonPrefix = @()
if ($PythonVersionArgs.Trim()) {
    $pythonPrefix = $PythonVersionArgs.Trim() -split '\s+'
}

foreach ($service in $services) {
    $arguments = $pythonPrefix + @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$($service.Port)")
    Write-Host "Starting $($service.Name) on port $($service.Port) ..."
    $logPath = Join-Path $dataDir "$($service.Name).log"
    $errorPath = Join-Path $dataDir "$($service.Name).err.log"
    $processes += Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $service.Dir -PassThru -WindowStyle Hidden -RedirectStandardOutput $logPath -RedirectStandardError $errorPath
}

$frontend = $null
if ($KeepRunning) {
    Write-Host "Serving the static UI on port $FrontendPort ..."
    $frontend = Start-Process -FilePath $Python -ArgumentList ($pythonPrefix + @("-m", "http.server", "$FrontendPort", "--bind", "127.0.0.1")) -WorkingDirectory (Join-Path $root "frontend") -PassThru -WindowStyle Hidden
}

try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$BackendPort/api/health" -TimeoutSec 3
            $ready = $true
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }

    if (-not $ready) {
        throw "Backend did not start on port $BackendPort (see $dataDir\backend.err.log)"
    }

    Write-Host "Backend is ready. Hermes Agent health: $($health.hermes.ok)."
    if ($KeepRunning) {
        Write-Host "UI: http://127.0.0.1:$FrontendPort/?api=http://127.0.0.1:$BackendPort/api"
    }

    if ($RunSmokeTest) {
        & $Python $PythonVersionArgs (Join-Path $root "scripts\smoke_test.py") --base "http://127.0.0.1:$BackendPort" --csv (Join-Path $root "samples\sales.csv")
        if ($LASTEXITCODE -ne 0) { throw "Smoke test failed with exit code $LASTEXITCODE" }
    }

    if ($KeepRunning) {
        Write-Host "Press Enter to stop the local services ..."
        Read-Host | Out-Null
    }
} finally {
    foreach ($process in ($processes + $frontend)) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "Local services stopped."
}
