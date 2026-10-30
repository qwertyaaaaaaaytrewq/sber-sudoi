param(
    [string]$BaseUrl = "http://127.0.0.1:8642/v1",
    [string]$ProjectEnv = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [string]$HermesEnv = (Join-Path $env:USERPROFILE ".hermes\.env")
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Read-DotEnvValue([string]$Path, [string[]]$Names) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue) {
        foreach ($name in $Names) {
            if ($line -match "^\s*$([regex]::Escape($name))\s*=(.*)$") {
                return $matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    return $null
}
$key = $env:API_SERVER_KEY
if (-not $key) { $key = Read-DotEnvValue $HermesEnv @("API_SERVER_KEY") }
if (-not $key) { $key = $env:HERMES_API_KEY }
if (-not $key) { $key = Read-DotEnvValue $ProjectEnv @("API_SERVER_KEY", "HERMES_API_KEY") }
if (-not $key) { throw "API key not found. Set API_SERVER_KEY or check $HermesEnv / $ProjectEnv." }

Write-Host "This setup changes Hermes Agent configuration, not the Hermes Chat .env." -ForegroundColor Cyan
Write-Host "Recommended tools for the api_server platform:"
Write-Host "  code_execution  -> execute_code (Python/pandas/statistics)"
Write-Host "  terminal        -> shell, downloads, package/runtime checks"
Write-Host "  file            -> read_file, write_file, patch, search_files"
Write-Host "  web             -> web_search, web_extract"
Write-Host "  skills          -> skills_list, skill_view, skill_manage"
Write-Host "  delegation      -> delegate_task"
Write-Host "  memory          -> memory"
Write-Host "Optional: browser, vision, image_gen, todo, session_search."
Write-Host ""
Write-Host "Opening Hermes tool manager. Select the api_server platform and enable the toolsets above."
Write-Host "Do not enable tools you do not trust; terminal and execute_code can run commands/code." -ForegroundColor Yellow

$cmd = Get-Command hermes -ErrorAction SilentlyContinue
if ($cmd) {
    & $cmd.Source tools
} else {
    $hermesExe = Join-Path $env:LOCALAPPDATA "hermes\bin\hermes.exe"
    if (Test-Path -LiteralPath $hermesExe) {
        & $hermesExe tools
    } else {
        throw "Could not find the Hermes CLI command. Run 'hermes tools' from the terminal where Hermes is installed."
    }
}

Write-Host "`nHermes tool manager closed. Restart the Hermes Gateway so the API Server reloads tool configuration." -ForegroundColor Cyan
$answer = Read-Host "Gateway restarted? Type Y to verify toolsets now"
if ($answer -match '^(Y|YES|Д|ДА)$') {
    & (Join-Path $PSScriptRoot "check_hermes_toolsets.ps1") -BaseUrl $BaseUrl -ProjectEnv $ProjectEnv -HermesEnv $HermesEnv
} else {
    Write-Host "After restarting the Gateway, run: .\scripts\check_hermes_toolsets.ps1"
}
