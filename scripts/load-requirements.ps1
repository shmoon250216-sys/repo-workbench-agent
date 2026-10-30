param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '../.env.requirements.local')
)

# Parse data only. Never dot-source or evaluate configuration contents.
$requirementNames = @(
    'REPO_AGENT_REQUIREMENTS_ENABLED',
    'REPO_AGENT_REQUIREMENTS_BASE_URL',
    'REPO_AGENT_REQUIREMENTS_MODEL',
    'REPO_AGENT_REQUIREMENTS_API_KEY'
)
$requirementValues = @{}
foreach ($configLine in [System.IO.File]::ReadAllLines((Resolve-Path -LiteralPath $ConfigPath))) {
    $trimmedLine = $configLine.Trim()
    if (-not $trimmedLine -or $trimmedLine.StartsWith('#')) { continue }
    $parts = $trimmedLine.Split(@('='), 2)
    if ($parts.Count -ne 2 -or $parts[0].Trim() -notin $requirementNames) {
        throw 'Unsupported configuration entry; expected only REPO_AGENT_REQUIREMENTS_* settings.'
    }
    $settingName = $parts[0].Trim()
    if ($requirementValues.ContainsKey($settingName)) { throw "Duplicate setting: $settingName" }
    $settingValue = $parts[1].Trim()
    if ($settingValue.Length -ge 2 -and (
        ($settingValue.StartsWith('"') -and $settingValue.EndsWith('"')) -or
        ($settingValue.StartsWith("'") -and $settingValue.EndsWith("'"))
    )) { $settingValue = $settingValue.Substring(1, $settingValue.Length - 2) }
    $requirementValues[$settingName] = $settingValue
}
foreach ($settingName in $requirementNames) {
    if (-not $requirementValues[$settingName]) { throw "Missing value: $settingName" }
}
foreach ($settingName in $requirementNames) {
    [Environment]::SetEnvironmentVariable($settingName, $requirementValues[$settingName], 'Process')
}
Write-Host 'Requirement model configuration loaded into this terminal. Start/restart the workbench from this terminal; credentials are not displayed.'
