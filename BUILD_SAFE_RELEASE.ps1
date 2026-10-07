$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Name = "NUNES_INDIAMART_AUTOMATION_V2_4_SAFE"
$Stage = Join-Path $env:TEMP $Name
$Zip = Join-Path $Root ($Name + ".zip")

if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item $Stage -ItemType Directory | Out-Null

$ExcludeDirs = @("venv", ".venv", "__pycache__", "outputs", "input_images", "logs", "data", ".git")
$ExcludeFiles = @(".env", "*.pyc", "*.pyo", "*.log", "*.zip")

Get-ChildItem $Root -Force | ForEach-Object {
    if ($ExcludeDirs -contains $_.Name) { return }
    if ($_.Name -eq ".env") { return }
    if ($_.Extension -eq ".zip") { return }
    Copy-Item $_.FullName $Stage -Recurse -Force
}

foreach ($d in @("outputs","input_images","logs","data")) {
    New-Item (Join-Path $Stage $d) -ItemType Directory -Force | Out-Null
    New-Item (Join-Path $Stage ($d + "\.gitkeep")) -ItemType File -Force | Out-Null
}

if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Zip -CompressionLevel Optimal
Write-Host "Safe release created: $Zip"
