# Run this only on the intended server PC, in an Administrator PowerShell.
$ErrorActionPreference = "Stop"
$rule = "NUNES IndiaMART V2 Port 5077"
$existing = Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
if (-not $existing) {
    New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5077 -Profile Private | Out-Null
    Write-Host "Created private-network firewall rule for TCP 5077."
} else {
    Write-Host "Firewall rule already exists: $rule"
}
