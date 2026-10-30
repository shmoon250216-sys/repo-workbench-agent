param(
    [Parameter(Mandatory=$true)][string]$Workspace,
    [string]$Python = 'python',
    [int]$Port = 8023,
    [ValidateSet('disabled','docker','trusted-local')][string]$Backend = 'disabled',
    [switch]$Demo
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$resolvedWorkspace = (Resolve-Path -LiteralPath $Workspace).Path
. (Join-Path $PSScriptRoot 'load-local-config.ps1') -Project $project
$env:PYTHONPATH = Join-Path $project 'src'
$state = Join-Path $project '.runtime/mysql-ui'
$arguments = @('-m','repo_agent.cli','--workspace',$resolvedWorkspace,'--state',$state,'--backend',$Backend)
if ($Demo) { $arguments += '--demo' }
$arguments += @('serve','--port',[string]$Port)
& $Python @arguments
