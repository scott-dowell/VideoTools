param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'run.ps1'
$runKeyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName = 'VideoToolsLocal'
$command = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $scriptPath

New-Item -Path $runKeyPath -Force | Out-Null

if ($Remove) {
    if (Test-Path $runKeyPath) {
        Remove-ItemProperty -Path $runKeyPath -Name $runName -ErrorAction SilentlyContinue
    }
    Write-Host "Removed $runName from the user Run key."
    exit 0
}

Set-ItemProperty -Path $runKeyPath -Name $runName -Value $command -Type String -Force
Write-Host "Registered $runName to start hidden on login."
