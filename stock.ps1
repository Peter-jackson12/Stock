# ASCII bootstrap: works in Windows PowerShell 5.1 without a BOM.
# Only the existing project .venv is used; never install or fall back to .venv32/PATH.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = 'help',
    [switch]$Json,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArguments = @()
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$allowed = @('help', 'doctor', 'status', 'ui-command')
if ($ExtraArguments.Count -gt 0 -or $Command -cnotin $allowed) {
    Write-Output 'Unsupported command/arguments. Run: .\stock.ps1 help'
    exit 2
}
if ($Command -eq 'help') {
    Write-Output @'
Stock operator (read-only v1)
  .\stock.ps1 help        : this help; no Python needed
  .\stock.ps1 doctor      : environment inventory; no auto-install/repair
  .\stock.ps1 status      : bounded saved evidence; NOT live health
  .\stock.ps1 ui-command  : print existing GUI command; DO NOT launch it
  Add -Json to doctor/status/ui-command for JSON.
Korean walkthrough: START_HERE.md
Collector start/stop/canary and backtest execution are NOT connected.
'@
    exit 0
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$entrypoint = Join-Path $PSScriptRoot 'scripts\stock_operator.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Output 'Project .venv\Scripts\python.exe is missing. Read START_HERE.md.'
    Write-Output 'No environment was installed/changed. No .venv32 or PATH fallback.'
    exit 2
}
$pythonArguments = @('-I', '-B', $entrypoint, $Command)
if ($Json) { $pythonArguments += '--json' }
try {
    & $python @pythonArguments
    exit $LASTEXITCODE
} catch {
    Write-Output ('Unable to run the read-only helper: ' + $_.Exception.Message)
    exit 1
}
