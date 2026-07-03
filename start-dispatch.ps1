# Start dispatch.py on port 8000. Stops any existing listener on that port first.
$ErrorActionPreference = "Stop"
$Port = 8000
$Root = $PSScriptRoot

foreach ($conn in Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    $ownerPid = $conn.OwningProcess
    if ($ownerPid -and $ownerPid -ne $PID) {
        Write-Host "Stopping process $ownerPid on port $Port ..."
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 1

Set-Location $Root
& "$Root\venv\Scripts\python.exe" -m uvicorn dispatch:app --host 0.0.0.0 --port $Port
