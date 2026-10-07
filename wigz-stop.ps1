$ErrorActionPreference = "Stop"
$TaskName = "Wigz Discord Bot"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Wigz stopped. Task state: $($task.State)"
