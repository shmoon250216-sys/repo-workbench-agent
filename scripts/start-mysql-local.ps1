# Starts only the dedicated instance recorded by this machine's setup.
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $project '.runtime/mysql-local/instance.json'
if (-not (Test-Path -LiteralPath $configPath)) { throw 'Local instance metadata missing; use compose.mysql.yml or configure an existing server.' }
$instance = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$listener = Get-NetTCPConnection -LocalPort $instance.port -State Listen -ErrorAction SilentlyContinue
if ($listener) { Write-Host 'Database port is already listening; no process started.'; return }
Start-Process -FilePath $instance.executable -ArgumentList ('--defaults-file="' + $instance.config + '"') -WindowStyle Hidden | Out-Null
Write-Host 'Dedicated local MySQL start requested. Data remains in its existing directory.'
