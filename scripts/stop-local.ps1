param([Parameter(Mandatory = $true)][string]$State)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$statePath = [System.IO.Path]::GetFullPath($State)
$marker = Join-Path $statePath 'local-processes.json'
if (-not (Test-Path -LiteralPath $marker)) {
    Write-Output 'No local processes are recorded.'
    return
}

$record = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
foreach ($name in @('server', 'worker')) {
    $entry = $record.$name
    $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if ($null -eq $process) { continue }
    if ($process.StartTime.ToUniversalTime().Ticks -ne $entry.startedUtcTicks) {
        throw "Recorded $name PID now belongs to another process; refusing to stop it."
    }
    Stop-Process -Id $entry.pid -Force
}
Remove-Item -LiteralPath $marker
Write-Output 'Quality Platform local processes stopped.'
