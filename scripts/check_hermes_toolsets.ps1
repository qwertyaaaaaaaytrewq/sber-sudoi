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

$uri = $BaseUrl.TrimEnd('/') + "/toolsets"
Write-Host "Hermes API Server tool availability: $uri" -ForegroundColor Cyan
$result = Invoke-RestMethod -Method Get -Uri $uri -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 10

# Keep advertised tools separate from usable tools. /v1/toolsets may include
# tool names under disabled toolsets, so presence alone is not proof of availability.
$allTools = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$advertisedTools = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$enabledToolsets = @()
$disabledToolsets = @()
$toolsets = @()
if ($result -is [System.Array]) { $toolsets = @($result) }
elseif ($null -ne $result.data) { $toolsets = @($result.data) }
elseif ($null -ne $result.toolsets) { $toolsets = @($result.toolsets) }
else { $toolsets = @($result) }

foreach ($set in $toolsets) {
    $name = [string]$set.name
    $enabled = $set.enabled
    $configured = $set.configured
    if ($enabled -eq $true) { $enabledToolsets += $name }
    else { $disabledToolsets += $name }
    foreach ($tool in @($set.tools)) {
        $toolName = $null
        if ($tool -is [string] -and $tool.Trim()) { $toolName = $tool.Trim() }
        elseif ($null -ne $tool.name -and [string]$tool.name) { $toolName = [string]$tool.name }
        if ($toolName) {
            [void]$advertisedTools.Add($toolName)
            if ($enabled -eq $true) { [void]$allTools.Add($toolName) }
        }
    }
}
$required = @("execute_code", "terminal", "read_file", "write_file", "patch", "search_files", "web_search", "web_extract", "skills_list", "skill_view", "delegate_task", "memory")
Write-Host "`nEnabled toolsets: $($enabledToolsets -join ', ')"
Write-Host "Disabled toolsets: $($disabledToolsets -join ', ')"
Write-Host "`nRequired / useful tools (only enabled toolsets count):"
foreach ($tool in $required) {
    if ($allTools.Contains($tool)) { Write-Host ("[OK]       " + $tool) -ForegroundColor Green }
    elseif ($advertisedTools.Contains($tool)) { Write-Host ("[DISABLED] " + $tool) -ForegroundColor Yellow }
    else { Write-Host ("[MISSING]  " + $tool) -ForegroundColor Red }
}
Write-Host "`nAll advertised tools:"
$allTools | Sort-Object | ForEach-Object { Write-Host "  $_" }

$missing = @($required | Where-Object { -not $allTools.Contains($_) })
$codeExecutionEnabled = $enabledToolsets -contains "code_execution"
if ($missing.Count -gt 0 -or -not $codeExecutionEnabled) {
    if (-not $codeExecutionEnabled) {
        Write-Host "`nRequired toolset code_execution is not enabled for api_server." -ForegroundColor Yellow
    }
    if ($missing.Count -gt 0) {
        Write-Host "API Server does not expose these tools through enabled toolsets: $($missing -join ', ')" -ForegroundColor Yellow
    }
    Write-Host "Run .\scripts\enable_hermes_tools.ps1, enable the recommended toolsets for platform api_server, restart the Gateway, then rerun this check."
    exit 4
}
Write-Host "`nRESULT: essential tools are exposed by enabled toolsets on the API Server." -ForegroundColor Green
