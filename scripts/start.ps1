# Start yt-dock in the background. Resolves the host port from
# $env:YTDOCK_PORT, then .env, then defaults to 8000.
Set-Location "$PSScriptRoot\.."

$port = $env:YTDOCK_PORT
if (-not $port -and (Test-Path ".env")) {
    $line = Get-Content ".env" | Where-Object { $_ -match '^\s*YTDOCK_PORT\s*=' } | Select-Object -Last 1
    if ($line) { $port = ($line -split '=', 2)[1].Trim() }
}
if (-not $port) { $port = "8000" }

docker compose up -d

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "yt-dock is running."
    Write-Host "  UI:        http://localhost:$port"
    Write-Host "  API docs:  http://localhost:$port/docs"
    Write-Host "  Health:    http://localhost:$port/health"
    Write-Host ""
    Write-Host "Data persists in: $(Get-Location)\data\transcripts.db"
} else {
    Write-Host "docker compose failed - check the output above." -ForegroundColor Red
}
