param(
    [Parameter(Mandatory = $true)][string]$State,
    [ValidateRange(1, 65535)][int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$statePath = [System.IO.Path]::GetFullPath($State)
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$binary = Join-Path $root '.venv\Scripts\qa-web.exe'
$build = Join-Path $root 'web\dist\index.html'
$marker = Join-Path $statePath 'local-processes.json'

if (-not (Test-Path -LiteralPath (Join-Path $statePath 'control.sqlite'))) {
    throw 'Web state is not initialized. Run qa-web init first.'
}
if (-not (Test-Path -LiteralPath $build)) {
    throw 'Frontend build is missing. Run bun run build in web/ first.'
}
if (-not (Test-Path -LiteralPath $binary)) {
    throw 'qa-web is missing. Run uv sync --dev first.'
}
if (Test-Path -LiteralPath $marker) {
    throw 'Local processes are already recorded. Run stop-local.ps1 before starting again.'
}

$logs = Join-Path $statePath 'logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$worker = $null
$server = $null
try {
    $worker = Start-Process -FilePath $binary -ArgumentList "worker --state `"$statePath`"" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs 'worker.out.log') `
        -RedirectStandardError (Join-Path $logs 'worker.err.log')
    $server = Start-Process -FilePath $binary -ArgumentList "serve --state `"$statePath`" --port $Port" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs 'server.out.log') `
        -RedirectStandardError (Join-Path $logs 'server.err.log')

    $ready = $false
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        Start-Sleep -Milliseconds 250
        $worker.Refresh()
        $server.Refresh()
        if ($worker.HasExited -or $server.HasExited) {
            throw 'Worker or server exited during startup. Check state/logs.'
        }
        try {
            $session = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/session" -TimeoutSec 2
            if ($session.worker_healthy) {
                $ready = $true
                break
            }
        } catch {
            # The HTTP server may still be starting.
        }
    }
    if (-not $ready) {
        throw 'Local service did not become ready. Check state/logs.'
    }

    $record = @{
        port = $Port
        worker = @{ pid = $worker.Id; startedUtcTicks = $worker.StartTime.ToUniversalTime().Ticks }
        server = @{ pid = $server.Id; startedUtcTicks = $server.StartTime.ToUniversalTime().Ticks }
    }
    [System.IO.File]::WriteAllText(
        $marker,
        ($record | ConvertTo-Json -Depth 4),
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Output "Quality Platform ready at http://127.0.0.1:$Port"
} catch {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
    if ($worker -and -not $worker.HasExited) { Stop-Process -Id $worker.Id -Force }
    throw
}
