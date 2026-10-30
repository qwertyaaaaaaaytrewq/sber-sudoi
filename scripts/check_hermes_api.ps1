$ErrorActionPreference = 'Stop'

function Read-DotEnvValue([string]$Name, [string]$DefaultValue = '') {
    $line = Get-Content -LiteralPath '.env' -ErrorAction SilentlyContinue |
        Where-Object { $_ -match ('^\s*' + [regex]::Escape($Name) + '=') } |
        Select-Object -First 1
    if (-not $line) { return $DefaultValue }
    return (($line -split '=', 2)[1]).Trim().Trim('"').Trim("'")
}

$base = Read-DotEnvValue 'HERMES_BASE_URL' 'http://127.0.0.1:8642/v1'
$key = Read-DotEnvValue 'HERMES_API_KEY' ''
$model = Read-DotEnvValue 'HERMES_API_MODEL' 'hermes-agent'
if (-not $key -or $key -eq 'change-me-local-dev') {
    throw 'Set HERMES_API_KEY in .env to the same value as API_SERVER_KEY in Hermes .env.'
}

$base = $base.TrimEnd('/')
if ($base -match '/v1$') {
    $v1 = $base
    $root = $base.Substring(0, $base.Length - 3)
} else {
    $root = $base
    $v1 = "$base/v1"
}
$headers = @{ Authorization = "Bearer $key" }
$allOk = $true

function Test-GetEndpoint([string]$Label, [string]$Url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers $headers -Method Get -TimeoutSec 10
        Write-Host "[OK]   $Label => HTTP $([int]$response.StatusCode)"
        if ($response.Content) { Write-Host ($response.Content.Substring(0, [Math]::Min(500, $response.Content.Length))) }
        return $true
    } catch {
        Write-Host "[FAIL] $Label => $($_.Exception.Message)" -ForegroundColor Red
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message }
        return $false
    }
}

Write-Host '=== Hermes Agent API checks ==='
$allOk = (Test-GetEndpoint 'health' "$v1/health") -and $allOk
$allOk = (Test-GetEndpoint 'models' "$v1/models") -and $allOk
$allOk = (Test-GetEndpoint 'detailed health' "$root/health/detailed") -and $allOk
$capabilitiesOk = Test-GetEndpoint 'capabilities (optional; detects Runs API)' "$v1/capabilities"
if (-not $capabilitiesOk) {
    Write-Host '[INFO] /v1/capabilities is unavailable; Hermes Chat will use the compatible legacy API path.' -ForegroundColor Yellow
}

Write-Host "=== POST /v1/chat/completions (non-streaming JSON test; model=$model) ==="
# ConvertTo-Json + UTF-8 byte[] avoids Windows PowerShell native-argument quote mangling with curl.exe.
$payload = @{
    model = $model
    messages = @(@{ role = 'user'; content = 'Reply exactly API_OK' })
    stream = $false
} | ConvertTo-Json -Depth 10 -Compress
$body = [System.Text.Encoding]::UTF8.GetBytes($payload)
try {
    $reply = Invoke-RestMethod -Uri "$v1/chat/completions" -Method Post -Headers $headers `
        -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 180
    $content = $reply.choices[0].message.content
    if (-not $content) { throw 'HTTP success received, but choices[0].message.content is empty.' }
    Write-Host "[OK]   Chat Completions returned text: $content"
} catch {
    Write-Host "[FAIL] POST /v1/chat/completions => $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message }
    Write-Host "Exact UTF-8 JSON body: $payload"
    $allOk = $false
}

if (-not $allOk) {
    Write-Host 'RESULT: at least one API check failed. Read the response body above; do not repeatedly resubmit a task while its outcome is unknown.' -ForegroundColor Red
    exit 1
}
Write-Host 'RESULT: basic Hermes API checks passed.' -ForegroundColor Green
exit 0
