# Installs the Python Workflow Integration for the current Resolve installation.
# Run PowerShell as Administrator from this folder.

$ErrorActionPreference = 'Stop'
$sourceFile = Join-Path $PSScriptRoot 'ResolveToNukeShotExporter.py'
$targetDirectory = Join-Path $env:ProgramData 'Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins'
$targetFile = Join-Path $targetDirectory 'ResolveToNukeShotExporter.py'

if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
    throw "Missing: $sourceFile"
}

New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
Copy-Item -LiteralPath $sourceFile -Destination $targetFile -Force

Write-Host "Installed: $targetFile"
Write-Host 'Quit and reopen DaVinci Resolve, then launch Workspace > Workflow Integrations > ResolveToNukeShotExporter.'
