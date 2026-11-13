# Create a Kivi desktop shortcut that starts the app without a console window.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root "Kivi UI.bat"
if (-not (Test-Path $launcher)) {
    throw "Kivi UI.bat not found: $launcher"
}
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Kivi.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = $root
$shortcut.Description = "Kivi - локальный ассистент"
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,220"
$shortcut.Save()
Write-Host "Создан ярлык: $shortcutPath"
