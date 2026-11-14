param(
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dotnet = if (Test-Path "D:\dotnet\dotnet.exe") { "D:\dotnet\dotnet.exe" } else { "dotnet" }
$project = Join-Path $root "desktop\Kivi.Desktop\Kivi.Desktop.csproj"
$output = Join-Path $root "desktop\Kivi.Desktop\publish"

& $dotnet publish $project `
    -c $Configuration `
    -r win-x64 `
    --self-contained true `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    -o $output

Write-Host "Built: $(Join-Path $output 'Kivi.exe')"
