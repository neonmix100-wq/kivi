param([string]$Configuration = "Release")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env:DOTNET_ROOT = "D:\dotnet"
$env:MSBuildSDKsPath = "D:\dotnet\sdk\8.0.428\Sdks"
$env:PATH = "D:\dotnet;$env:PATH"
$msbuild = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
$stage = Join-Path $root "dist\KiviWinUI"

& $msbuild (Join-Path $root "desktop\Kivi.WinUI\Kivi.WinUI.csproj") /t:Publish `
    /p:Configuration=$Configuration /p:Platform=x64 /p:RuntimeIdentifier=win-x64 /p:SelfContained=true `
    "/p:PublishDir=$root\desktop\Kivi.WinUI\publish" /v:minimal
if ($LASTEXITCODE -ne 0) { throw "WinUI publish failed" }

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item $stage -ItemType Directory -Force | Out-Null
robocopy (Join-Path $root "desktop\Kivi.WinUI\publish") $stage /E /NFL /NDL /NJH /NJS /NP | Out-Null
foreach ($name in @("src", "config", "web", "assets", "docs")) {
    Copy-Item (Join-Path $root $name) (Join-Path $stage $name) -Recurse
}
$runtime = Join-Path $stage "runtime"
New-Item $runtime -ItemType Directory -Force | Out-Null
$base = (& (Join-Path $root ".runtime-venv\Scripts\python.exe") -c "import sys; print(sys.base_prefix)").Trim()
robocopy $base $runtime /E /XD (Join-Path $base "Lib\site-packages") /NFL /NDL /NJH /NJS /NP | Out-Null
$site = Join-Path $runtime "Lib\site-packages"
New-Item $site -ItemType Directory -Force | Out-Null
robocopy (Join-Path $root ".runtime-venv\Lib\site-packages") $site /E /NFL /NDL /NJH /NJS /NP | Out-Null
$wake = Join-Path $root ".venv\Lib\site-packages\openwakeword\resources"
if (Test-Path $wake) { Copy-Item $wake (Join-Path $site "openwakeword") -Recurse -Force }

$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
& $iscc (Join-Path $root "desktop\Kivi.WinUI\installer.iss")
Write-Host "WinUI installer: $(Join-Path $root 'dist\Kivi-WinUI-Setup.exe')"
