# Stop the container (data is untouched)
Set-Location "$PSScriptRoot\.."
docker compose down
Write-Host "Container stopped. Your transcript library is safe in .\data\"
