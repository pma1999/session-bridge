param(
    [string]$Python = 'python',
    [string]$CommandDirectory = (Join-Path $env:USERPROFILE '.local\bin')
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot '.venv\Scripts\python.exe'
$versionCheck = & $Python -I -c 'import sys; print(int(sys.version_info >= (3,11)))'
if ($LASTEXITCODE -ne 0 -or $versionCheck -ne '1') { throw 'Python 3.11+ is required.' }
function Get-BridgeFileHash([string]$Path) {
    # Use the already required Python runtime on both Windows PowerShell and pwsh.
    $hashCode = "import hashlib,sys; print(hashlib.file_digest(open(sys.argv[1], 'rb'), 'sha256').hexdigest())"
    $hashOutput = & $Python -I -c $hashCode $Path
    if ($LASTEXITCODE -ne 0) { throw 'Unable to verify the managed command hash.' }
    return $hashOutput.Trim()
}
$launcher = Join-Path $projectRoot '.venv\Scripts\sessionbridge.exe'
$target = Join-Path $CommandDirectory 'sessionbridge.exe'
$receiptRoot = Join-Path $env:LOCALAPPDATA 'SessionBridge'
$receiptPath = Join-Path $receiptRoot 'installation.json'
if (Test-Path -LiteralPath $target) {
    if (-not (Test-Path -LiteralPath $receiptPath)) { throw 'Refusing to replace an unmanaged command.' }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    if ($receipt.command -ne $target -or (Get-BridgeFileHash $target) -ne $receipt.sha256) {
        throw 'The existing command was modified or belongs to another installation.'
    }
}

function Invoke-BridgeWorkerControl([string]$Action) {
    # -I inspects the installed runtime, regardless of a developer's PYTHONPATH.
    $workerCode = @'
import importlib.util, json, sys
available = (importlib.util.find_spec('session_bridge') is not None
             and importlib.util.find_spec('session_bridge.sync_daemon') is not None)
if available:
    from session_bridge import sync_daemon
    result = getattr(sync_daemon, sys.argv[1])()
else:
    result = {'available': False, 'running': False}
print(json.dumps(result))
'@
    $workerOutput = & $runtime -I -c $workerCode $Action
    if ($LASTEXITCODE -ne 0) { throw "Worker $Action failed; runtime was not safely updated." }
    return ($workerOutput | ConvertFrom-Json)
}

$restartWorker = $false
if (Test-Path -LiteralPath $runtime -PathType Leaf) {
    $workerBefore = Invoke-BridgeWorkerControl 'status'
    $restartWorker = [bool]$workerBefore.running
    if ($restartWorker) {
        $workerStopped = Invoke-BridgeWorkerControl 'stop'
        if ($workerStopped.running) { throw 'Worker is still running; refusing to replace its code.' }
    }
}
try {
    if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
        & $Python -I -m venv (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Unable to create the isolated environment.' }
    }
    & $runtime -I -m pip install -r (Join-Path $projectRoot 'requirements.lock')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $runtime -I -m pip install --no-deps $projectRoot
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed.' }
    New-Item -ItemType Directory -Path $CommandDirectory -Force | Out-Null
    Copy-Item -LiteralPath $launcher -Destination $target
    New-Item -ItemType Directory -Path $receiptRoot -Force | Out-Null
    @{ project = $projectRoot; command = $target; sha256 = (Get-BridgeFileHash $target) } |
        ConvertTo-Json | Set-Content -LiteralPath $receiptPath -Encoding utf8
    & $target --help
    if ($LASTEXITCODE -ne 0) { throw 'Installed command verification failed.' }
}
catch {
    $installFailure = $_
    if ($restartWorker) {
        try { $null = Invoke-BridgeWorkerControl 'start' }
        catch { Write-Warning 'Worker restart failed; repair the installation, then run sessionbridge sync start.' }
    }
    throw $installFailure
}
if ($restartWorker) {
    $workerAfter = Invoke-BridgeWorkerControl 'start'
    if (-not $workerAfter.running -or -not $workerAfter.healthy) {
        throw 'Updated, but worker restart could not be verified. Run sessionbridge sync worker-status.'
    }
}
Write-Host "Installed: $target"
if (($env:PATH -split ';') -notcontains $CommandDirectory) {
    Write-Host "Add $CommandDirectory to your user PATH to invoke sessionbridge by name."
}
