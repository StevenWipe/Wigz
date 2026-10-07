$ErrorActionPreference = "Stop"
$TaskName = "Wigz Discord Bot"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Wigz restarted. Task state: $($task.State)"
