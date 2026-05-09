# run.ps1 — Stop any running instance and start the Flask dev server
$venv = "$PSScriptRoot\.venv\Scripts"
$app  = "$PSScriptRoot\VideoConverter\app.py"

# Kill any python process holding port 5001
$procs = Get-NetTCPConnection -LocalPort 5001 -ErrorAction SilentlyContinue |
         Select-Object -ExpandProperty OwningProcess -Unique
if ($procs) {
    Write-Host "Stopping process(es) on port 5001: $procs" -ForegroundColor Yellow
    $procs | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 400
} else {
    Write-Host "Port 5001 is free." -ForegroundColor Green
}

Write-Host "Starting Flask..." -ForegroundColor Cyan
$logDir = "$PSScriptRoot\VideoConverter\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$crashLog = "$logDir\flask_crash.log"
# Launch in a new window so output is visible and stderr goes to flask_crash.log
Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/k `"`"$venv\python.exe`" `"$app`" 2>>`"$crashLog`"`"" `
    -WindowStyle Normal
