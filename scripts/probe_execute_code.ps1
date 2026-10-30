param(
    [string]$BaseUrl = "http://127.0.0.1:8642/v1",
    [string]$ProjectEnv = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env"),
    [string]$HermesEnv = (Join-Path $env:USERPROFILE ".hermes\.env"),
    [string]$Model = ""
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
if (-not $key -or $key -eq "change-me-local-dev") {
    throw "API key not found or still placeholder. Set API_SERVER_KEY in $HermesEnv or HERMES_API_KEY in $ProjectEnv. The key will not be printed."
}

$configuredBase = Read-DotEnvValue $ProjectEnv @("HERMES_BASE_URL")
if ($configuredBase) { $BaseUrl = $configuredBase }
$configuredModel = Read-DotEnvValue $ProjectEnv @("HERMES_API_MODEL")
if (-not $Model) { $Model = if ($configuredModel) { $configuredModel } else { "hermes-agent" } }
$BaseUrl = $BaseUrl.TrimEnd('/')
if (-not $BaseUrl.EndsWith('/v1')) { $BaseUrl += '/v1' }
$headers = @{ Authorization = "Bearer $key"; Accept = "application/json" }

Write-Host "=== Direct Hermes execute_code probe ===" -ForegroundColor Cyan
Write-Host "API: $BaseUrl"
Write-Host "Model alias: $Model"
Write-Host "Credentials: loaded (value hidden)"

# Show effective toolset metadata including configured, which the older checker did not report.
try {
    $sets = Invoke-RestMethod -Method Get -Uri "$BaseUrl/toolsets" -Headers $headers -TimeoutSec 15
    if ($sets -is [System.Array]) { $items = @($sets) }
    elseif ($null -ne $sets.data) { $items = @($sets.data) }
    elseif ($null -ne $sets.toolsets) { $items = @($sets.toolsets) }
    else { $items = @($sets) }

    Write-Host "`n--- Effective toolset metadata ---" -ForegroundColor Cyan
    foreach ($set in $items) {
        if ([string]$set.name -in @("code_execution", "terminal")) {
            $toolNames = @($set.tools | ForEach-Object {
                if ($_ -is [string]) { $_ } elseif ($_.name) { [string]$_.name }
            })
            $configuredText = if ($null -eq $set.configured) { "<field absent>" } else { [string]$set.configured }
            Write-Host ("{0}: enabled={1}; configured={2}; tools=[{3}]" -f $set.name, $set.enabled, $configuredText, ($toolNames -join ", "))
        }
    }
} catch {
    Write-Host "[FAIL] GET /toolsets: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message }
    exit 2
}

# A single Responses request lets us inspect function_call and function_call_output items,
# rather than judging from an ordinary text-only health check.
$bodyObject = @{
    model = $Model
    instructions = "You are testing your own available Hermes Agent tools. You must invoke the native execute_code tool from the code_execution toolset, not describe it and not print pseudo-tool syntax. Execute exactly this Python: print('EXECUTE_CODE_DIRECT_OK', 6 * 7). After the tool returns, report the literal output you received. If you cannot call the tool, state that plainly."
    input = "Run the execute_code diagnostic now. Do not ask for confirmation; this is a harmless local calculation."
    store = $true
}
$payload = $bodyObject | ConvertTo-Json -Depth 12 -Compress
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
$headers["Idempotency-Key"] = "hermes-execute-code-probe-$( [guid]::NewGuid().ToString('N') )"

Write-Host "`n--- POST /v1/responses (one execution attempt; no automatic retry) ---" -ForegroundColor Cyan
try {
    $response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$BaseUrl/responses" -Headers $headers `
        -ContentType "application/json; charset=utf-8" -Body $bodyBytes -TimeoutSec 300
    Write-Host "HTTP $([int]$response.StatusCode)"
    $result = $response.Content | ConvertFrom-Json
} catch {
    Write-Host "[FAIL] POST /responses: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message }
    Write-Host "If this endpoint is not advertised by /v1/capabilities, stop here and report that result; do not assume the request ran."
    exit 3
}

Write-Host "Response status: $($result.status)"
if ($result.error) {
    Write-Host ("API error: " + ($result.error | ConvertTo-Json -Depth 6 -Compress)) -ForegroundColor Red
    exit 4
}
$output = @($result.output)
$toolCalls = @($output | Where-Object { $_.type -eq "function_call" })
$toolResults = @($output | Where-Object { $_.type -eq "function_call_output" })
$executeCalls = @($toolCalls | Where-Object { $_.name -eq "execute_code" })

Write-Host "`n--- Structured output items ---" -ForegroundColor Cyan
foreach ($item in $output) {
    switch ([string]$item.type) {
        "function_call" {
            Write-Host ("TOOL_CALL name={0}; status={1}; arguments={2}" -f $item.name, $item.status, ([string]$item.arguments))
        }
        "function_call_output" {
            $out = [string]$item.output
            if ($out.Length -gt 1200) { $out = $out.Substring(0, 1200) + " ...[truncated]" }
            Write-Host ("TOOL_OUTPUT call_id={0}; output={1}" -f $item.call_id, $out)
        }
        "message" {
            foreach ($part in @($item.content)) {
                if ($part.text) { Write-Host ("ASSISTANT_TEXT: " + [string]$part.text) }
            }
        }
        default { Write-Host ("OUTPUT_ITEM type={0}; status={1}" -f $item.type, $item.status) }
    }
}

Write-Host "`n--- Verdict ---" -ForegroundColor Cyan
$executeCallIds = @($executeCalls | ForEach-Object { [string]$_.call_id } | Where-Object { $_ })
$matchedExecuteResults = @($toolResults | Where-Object { $executeCallIds -contains [string]$_.call_id })
if ($executeCalls.Count -gt 0 -and $matchedExecuteResults.Count -gt 0) {
    Write-Host "PASS: the Responses API returned a real execute_code function_call and the matching tool result." -ForegroundColor Green
    if ($result.status -ne "completed") { Write-Host "NOTE: response status is not completed; inspect the output above." -ForegroundColor Yellow }
    exit 0
} elseif ($executeCalls.Count -gt 0) {
    Write-Host "PARTIAL: execute_code was called, but no matching function_call_output was returned in this response. Inspect status/error above and Gateway logs." -ForegroundColor Yellow
    exit 5
} elseif ($toolCalls.Count -gt 0) {
    Write-Host ("NOT PASSED: Hermes called other tool(s) but not execute_code: " + (($toolCalls | ForEach-Object { $_.name } | Sort-Object -Unique) -join ", ")) -ForegroundColor Yellow
    exit 6
} else {
    Write-Host "NOT PASSED: no structured function_call appeared. If the assistant text contains execute_code: or says Python is forbidden, this is a model/tool-call selection or enforcement issue, not evidence that the API key is rejected." -ForegroundColor Red
    exit 7
}
