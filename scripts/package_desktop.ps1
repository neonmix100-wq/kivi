param(
    [switch]$SkipRuntime
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stage = Join-Path $root "dist\Kivi"
$dotnet = if (Test-Path "D:\dotnet\dotnet.exe") { "D:\dotnet\dotnet.exe" } else { "dotnet" }

& (Join-Path $PSScriptRoot "build_desktop.ps1")
if ($LASTEXITCODE -ne 0) { throw "desktop build failed" }

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item $stage -ItemType Directory -Force | Out-Null

Copy-Item (Join-Path $root "desktop\Kivi.Desktop\publish\Kivi.exe") $stage
foreach ($name in @("src", "config", "web", "assets", "docs")) {
    Copy-Item (Join-Path $root $name) (Join-Path $stage $name) -Recurse
}

if (-not $SkipRuntime) {
    $base = (& (Join-Path $root ".venv\Scripts\python.exe") -c "import sys; print(sys.base_prefix)").Trim()
    $runtime = Join-Path $stage "runtime"
    New-Item $runtime -ItemType Directory -Force | Out-Null
    robocopy $base $runtime /E /XD (Join-Path $base "Lib\site-packages") /NFL /NDL /NJH /NJS /NP | Out-Null
    $site = Join-Path $runtime "Lib\site-packages"
    New-Item $site -ItemType Directory -Force | Out-Null
    robocopy (Join-Path $root ".venv\Lib\site-packages") $site /E /NFL /NDL /NJH /NJS /NP | Out-Null
}

$iss = Join-Path $root "desktop\Kivi.Desktop\installer.iss"
$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { throw "Inno Setup compiler not found: $iscc" }
& $iscc $iss
Write-Host "Installer: $(Join-Path $root 'dist\Kivi-Setup.exe')"
