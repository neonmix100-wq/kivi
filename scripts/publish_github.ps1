# Publish the Kivi repository to GitHub.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\publish_github.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\publish_github.ps1 -Public
#
# The script never commits: make a commit first, then publish.

param(
    [string]$RepoName = "kivi",
    [switch]$Public
)

$ErrorActionPreference = "Stop"
$gh = Join-Path $env:LOCALAPPDATA "Programs\gh\bin\gh.exe"
if (-not (Test-Path $gh)) {
    $gh = "gh"
}

& $gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI is not authenticated. Run: $gh auth login"
}

$owner = (& $gh api user --jq .login).Trim()
if (-not $owner) {
    throw "Could not resolve the GitHub user"
}
$fullName = "$owner/$RepoName"

& $gh repo view $fullName *> $null
$exists = ($LASTEXITCODE -eq 0)

if (-not $exists) {
    $visibility = if ($Public) { "--public" } else { "--private" }
    & $gh repo create $fullName $visibility --source . --remote origin --push
    if ($LASTEXITCODE -ne 0) {
        throw "gh repo create failed"
    }
    Write-Host "Created $fullName and pushed the current branch."
    exit 0
}

$origin = (git remote get-url origin 2>$null)
if (-not $origin) {
    git remote add origin "https://github.com/$fullName.git"
}
git push -u origin HEAD
if ($LASTEXITCODE -ne 0) {
    throw "git push failed"
}
Write-Host "Pushed to https://github.com/$fullName"
