$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=================================================="
Write-Host " NUNES V2.4 - IMPORT OLD GOOGLE SHEET SETTINGS"
Write-Host "=================================================="
Write-Host ""

$Project = (Get-Location).Path
if (-not (Test-Path (Join-Path $Project "app.py"))) {
    Write-Host "ERROR: Run this from the V2.4 project folder."
    exit 1
}

$LocalRoot = Join-Path $env:LOCALAPPDATA "NUNES_INDIAMART_AUTOMATION"
$ConfigDir = Join-Path $LocalRoot "config"
$Target = Join-Path $ConfigDir ".env"
$Example = Join-Path $Project ".env.example"
New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
if (-not (Test-Path $Target)) {
    if (Test-Path $Example) { Copy-Item $Example $Target -Force }
    else { New-Item -ItemType File -Path $Target | Out-Null }
}

$Roots = @(
    (Join-Path $env:USERPROFILE "Downloads"),
    (Join-Path $env:USERPROFILE "Desktop")
) | Where-Object { Test-Path $_ }

$candidates = @()
foreach ($root in $Roots) {
    try {
        $files = Get-ChildItem -Path $root -Filter ".env" -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object {
                $_.FullName -ne $Target -and
                $_.FullName -match "NUNES|INDIAMART|indiamart"
            }
        foreach ($f in $files) {
            $content = Get-Content $f.FullName -ErrorAction SilentlyContinue
            $urlLine = $content | Where-Object { $_ -match '^\s*APPS_SCRIPT_URL\s*=\s*.+$' } | Select-Object -First 1
            $tokLine = $content | Where-Object { $_ -match '^\s*APPS_SCRIPT_TOKEN\s*=\s*.+$' } | Select-Object -First 1
            if ($urlLine -and $tokLine) {
                $url = ($urlLine -replace '^\s*APPS_SCRIPT_URL\s*=\s*','').Trim().Trim('"').Trim("'")
                $tok = ($tokLine -replace '^\s*APPS_SCRIPT_TOKEN\s*=\s*','').Trim().Trim('"').Trim("'")
                if ($url -and $tok) {
                    $candidates += [PSCustomObject]@{
                        File = $f.FullName
                        Url = $url
                        Token = $tok
                        Modified = $f.LastWriteTime
                    }
                }
            }
        }
    } catch {}
}

$candidates = @($candidates | Sort-Object Modified -Descending -Unique)
if ($candidates.Count -eq 0) {
    Write-Host "No previous NUNES .env with Google Sheet settings was found."
    Write-Host "Use the in-app Google Sheet setup from the SERVER PC instead."
    exit 0
}

Write-Host "Found previous configurations (newest first):"
Write-Host ""
for ($i = 0; $i -lt $candidates.Count; $i++) {
    $n = $i + 1
    Write-Host ("[{0}] {1:yyyy-MM-dd HH:mm}  {2}" -f $n, $candidates[$i].Modified, $candidates[$i].File)
}
Write-Host ""
$answer = Read-Host "Select number, or press Enter for newest [1]"
if ([string]::IsNullOrWhiteSpace($answer)) { $index = 0 }
else {
    $num = 0
    if (-not [int]::TryParse($answer, [ref]$num) -or $num -lt 1 -or $num -gt $candidates.Count) {
        Write-Host "Invalid selection. Nothing changed."
        exit 1
    }
    $index = $num - 1
}
$chosen = $candidates[$index]

$lines = @(Get-Content $Target -ErrorAction SilentlyContinue)
function Set-EnvLine([string[]]$Current,[string]$Key,[string]$Value) {
    $found = $false
    $out = foreach ($line in $Current) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
            $found = $true
            "$Key=$Value"
        } else { $line }
    }
    if (-not $found) { $out += "$Key=$Value" }
    return ,$out
}
$lines = Set-EnvLine $lines "APPS_SCRIPT_URL" $chosen.Url
$lines = Set-EnvLine $lines "APPS_SCRIPT_TOKEN" $chosen.Token
Set-Content -Path $Target -Value $lines -Encoding UTF8

Write-Host ""
Write-Host "Imported Google Sheet settings into PRIVATE V2.4 config:"
Write-Host $Target
Write-Host "Source:"
Write-Host $chosen.File
Write-Host ""
Write-Host "Restart START.bat if the website is already running."
