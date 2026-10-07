$ErrorActionPreference = "Stop"
$ConfigDir = Join-Path $env:LOCALAPPDATA "NUNES_INDIAMART_AUTOMATION\config"
if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}
Write-Host "Restricting private config folder to the current Windows user..."
icacls $ConfigDir /inheritance:r /grant:r "$env:USERNAME:(OI)(CI)F" /T | Out-Host
Write-Host "Done: $ConfigDir"
