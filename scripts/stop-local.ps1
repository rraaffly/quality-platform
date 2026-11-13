param([Parameter(Mandatory = $true)][string]$State)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python environment is missing. Run uv sync --locked --dev first.'
}
& $python -m quality_platform.web.launcher stop --state $State
if ($LASTEXITCODE -ne 0) { throw "Stop failed with exit code $LASTEXITCODE" }
