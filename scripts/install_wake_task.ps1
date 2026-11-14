param(
    [string]$TaskName = "KiviWake",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$script = "m kivi listen --mic --speak --whisper-model data/models/faster-whisper-large-v3-turbo --turns 100000"

if ($Uninstall) {
    schtasks.exe /Delete /TN $TaskName /F | Out-Null
    Write-Host "Removed scheduled task $TaskName"
    exit 0
}

if (-not (Test-Path $pythonw)) {
    throw "pythonw.exe not found: $pythonw"
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $script -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Installed $TaskName; it starts at the next logon."
Write-Host "Run now: Start-ScheduledTask -TaskName $TaskName"
