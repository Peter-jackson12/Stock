# ASCII bootstrap: works in Windows PowerShell 5.1 without a BOM.
# Uses only the existing project .venv; never installs or falls back to .venv32/PATH.
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
$allowed = @('help', 'doctor', 'status', 'ui-command', 'ui')
if ($ExtraArguments.Count -gt 0 -or $Command -cnotin $allowed) {
    Write-Output 'Unsupported command/arguments. Run: .\stock.ps1 help'
    exit 2
}
if ($Command -eq 'help') {
    Write-Output @'
Stock operator v2
  .\stock.ps1 help        : this help; no Python needed
  .\stock.ps1 doctor      : environment inventory; no auto-install/repair
  .\stock.ps1 status      : bounded saved evidence; NOT live health
  .\stock.ps1 ui-command  : print the existing GUI command; DO NOT launch it
  .\stock.ps1 ui          : launch the existing Streamlit dashboard in foreground
  Add -Json to doctor/status/ui-command only.
  PowerShell policy alternative: .\stock.cmd <command>
Korean walkthrough: START_HERE.md
Collector start/stop/canary and backtest execution are NOT connected.
'@
    exit 0
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$entrypoint = Join-Path $PSScriptRoot 'scripts\stock_operator.py'
$app = Join-Path $PSScriptRoot 'dashboard\app.py'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Output 'Project .venv\Scripts\python.exe is missing. Read START_HERE.md.'
    Write-Output 'No environment was installed/changed. No .venv32 or PATH fallback.'
    exit 2
}

if ($Command -eq 'ui') {
    if ($Json) {
        Write-Output 'The ui command does not support -Json. Use ui-command -Json for a dry-run report.'
        exit 2
    }
    if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
        Write-Output 'dashboard\app.py is missing. No process was launched.'
        exit 2
    }
    Write-Output 'Starting the existing Stock Streamlit UI in the foreground.'
    Write-Output 'Close it with Ctrl+C in this terminal. Closing only the browser tab does not stop the server.'
    Push-Location -LiteralPath $PSScriptRoot
    try {
        & $python -B -m streamlit run $app --server.address 127.0.0.1
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    exit $exitCode
}

$pythonArguments = @('-I', '-B', $entrypoint, $Command)
if ($Json) { $pythonArguments += '--json' }
try {
    & $python @pythonArguments
    exit $LASTEXITCODE
} catch {
    Write-Output ('Unable to run the operator helper: ' + $_.Exception.Message)
    exit 1
}
