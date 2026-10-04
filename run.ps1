$ErrorActionPreference = 'Stop'

$repoRoot = $PSScriptRoot
$appDir = Join-Path $repoRoot 'VideoConverter'
$appEntry = Join-Path $appDir 'app.py'
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$port = 5001

if (-not (Test-Path $pythonExe)) {
    throw "Missing virtualenv Python: $pythonExe"
}

if (-not (Test-Path $appEntry)) {
    throw "Missing app entrypoint: $appEntry"
}

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "VideoConverter already listening on http://127.0.0.1:$port (LISTEN socket detected)."
    exit 0
}

New-Item -ItemType Directory -Force -Path (Join-Path $appDir 'logs') | Out-Null

Start-Process -FilePath $pythonExe `
    -ArgumentList @("$appEntry") `
    -WorkingDirectory $appDir `
    -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 250
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
} while (-not $listener -and (Get-Date) -lt $deadline)

if (-not $listener) {
    throw "VideoConverter did not begin listening on port $port within 20 seconds."
}

Write-Host "VideoConverter started hidden on http://127.0.0.1:$port"
Try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -Method Get -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
    Write-Host "Reachable: HTTP $($response.StatusCode)"
}
Catch {
    throw "VideoConverter is listening on port $port but did not answer HTTP requests on localhost."
}

