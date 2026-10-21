param([switch]$Disable)
$ErrorActionPreference = 'Stop'
$portfolioTaskName = 'Betafolio'
if ($Disable) {
    Unregister-ScheduledTask -TaskName $portfolioTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Betafolio startup disabled.'
    exit
}
$portfolioRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$portfolioPython = Join-Path $portfolioRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $portfolioPython)) { throw 'Run uv sync in the project before enabling startup.' }
$portfolioAction = New-ScheduledTaskAction -Execute $portfolioPython -Argument '-m betafolio serve' -WorkingDirectory $portfolioRoot
$portfolioTrigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$portfolioSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $portfolioTaskName -Action $portfolioAction -Trigger $portfolioTrigger -Settings $portfolioSettings -Description 'Personal read-only portfolio research dashboard' -Force | Out-Null
Write-Output 'Betafolio will start hidden when you sign in. Your computer must remain awake for scheduled research.'
