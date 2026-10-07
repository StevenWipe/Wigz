$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonW = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
$Bot = Join-Path $ProjectDir "bot.py"

if (-not (Test-Path $PythonW)) {
    throw "Wigz virtual environment not found at $PythonW"
}

$Action = New-ScheduledTaskAction -Execute $PythonW -Argument ('"' + $Bot + '"') -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "Wigz Discord Bot" -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Runs Wigz Discord voice statistics bot in the background." -Force | Out-Null
Start-ScheduledTask -TaskName "Wigz Discord Bot"

Write-Host "Wigz background task installed and started."
Write-Host "Log: $ProjectDir\logs\wigz.log"
