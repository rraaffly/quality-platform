param(
    [Parameter(Mandatory = $true)][string]$State,
    [ValidateRange(1, 65535)][int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python environment is missing. Run uv sync --locked --dev first.'
}
& $python -m quality_platform.web.launcher dashboard --state $State --port $Port --no-open
if ($LASTEXITCODE -ne 0) { throw "Dashboard failed with exit code $LASTEXITCODE" }
