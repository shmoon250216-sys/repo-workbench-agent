param([string]$Project = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$allowed = @('REPO_AGENT_BASE_URL', 'REPO_AGENT_MODEL', 'REPO_AGENT_API_KEY',
    'REPO_AGENT_REQUIREMENTS_ENABLED', 'REPO_AGENT_REQUIREMENTS_BASE_URL',
    'REPO_AGENT_REQUIREMENTS_MODEL', 'REPO_AGENT_REQUIREMENTS_API_KEY',
    'REPO_AGENT_DATABASE_URL', 'REPO_AGENT_WEB_TOKEN')
foreach ($name in @('.env', '.env.requirements.local', '.env.mysql.local')) {
    $file = Join-Path $Project $name
    if (-not (Test-Path -LiteralPath $file)) { continue }
    foreach ($line in Get-Content -LiteralPath $file -Encoding utf8) {
        $entry = $line.Trim()
        if (-not $entry -or $entry.StartsWith('#')) { continue }
        $parts = $entry.Split('=', 2)
        if ($parts.Count -ne 2 -or $parts[0].Trim() -notin $allowed) { continue }
        $value = $parts[1].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'")))) { $value = $value.Substring(1, $value.Length - 2) }
        [Environment]::SetEnvironmentVariable($parts[0].Trim(), $value, 'Process')
    }
}
Write-Host 'Local model and database configuration loaded (values hidden).'
