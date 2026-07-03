# Start agent.py. Stops any existing listener on port 8081 first (LiveKit worker HTTP).
$ErrorActionPreference = "Stop"
$Port = 8081
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
& "$Root\venv\Scripts\python.exe" agent.py start
