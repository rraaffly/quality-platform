Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$installRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/'
}
Push-Location $installRoot
try {
    & uv sync --locked --no-dev --python 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
$entrypoint = Join-Path $installRoot '.venv\Scripts\quality-platform.exe'
if (-not (Test-Path -LiteralPath $entrypoint)) { throw "Missing launcher: $entrypoint" }
$customBin = $env:QUALITY_PLATFORM_BIN_DIR
$userBin = if ($customBin) { [System.IO.Path]::GetFullPath($customBin) } else { Join-Path $env:LOCALAPPDATA 'QualityPlatform\bin' }
New-Item -ItemType Directory -Force -Path $userBin | Out-Null
$shim = Join-Path $userBin 'quality-platform.cmd'
[System.IO.File]::WriteAllText($shim, "@echo off`r`n`"$entrypoint`" %*`r`n")
if (-not $customBin) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($userPath -split ';' | Where-Object { $_ })
    if ($parts -notcontains $userBin) {
        [Environment]::SetEnvironmentVariable('Path', (($parts + $userBin) -join ';'), 'User')
    }
}
$env:Path = "$userBin;$env:Path"
Write-Host "Installed $shim"
Write-Host 'Open a new terminal, then run: quality-platform setup'
