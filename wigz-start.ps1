$ErrorActionPreference = "Stop"
$TaskName = "Wigz Discord Bot"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Wigz started. Task state: $($task.State)"
