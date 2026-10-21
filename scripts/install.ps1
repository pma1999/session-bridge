param(
    [string]$Python = 'python',
    [string]$CommandDirectory = (Join-Path $env:USERPROFILE '.local\bin'),
    # Where the isolated environment goes; the project's .venv unless the updater picks another folder.
    [string]$Runtime = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Runtime) { $Runtime = Join-Path $projectRoot '.venv' }
$Runtime = [System.IO.Path]::GetFullPath($Runtime)
$runtimePython = Join-Path $Runtime 'Scripts\python.exe'
$versionCheck = & $Python -I -c 'import sys; print(int(sys.version_info >= (3,11)))'
if ($LASTEXITCODE -ne 0 -or $versionCheck -ne '1') { throw 'Python 3.11+ is required.' }
function Get-BridgeFileHash([string]$Path) {
    # Use the already required Python runtime on both Windows PowerShell and pwsh.
    $hashCode = "import hashlib,sys; print(hashlib.file_digest(open(sys.argv[1], 'rb'), 'sha256').hexdigest())"
    $hashOutput = & $Python -I -c $hashCode $Path
    if ($LASTEXITCODE -ne 0) { throw 'Unable to verify the managed command hash.' }
    return $hashOutput.Trim()
}
$launcher = Join-Path $Runtime 'Scripts\sessionbridge.exe'
$target = Join-Path $CommandDirectory 'sessionbridge.exe'
$receiptRoot = Join-Path $env:LOCALAPPDATA 'SessionBridge'
$receiptPath = Join-Path $receiptRoot 'installation.json'
$receipt = $null
if (Test-Path -LiteralPath $receiptPath) { $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json }
if (Test-Path -LiteralPath $target) {
    if ($null -eq $receipt) { throw 'Refusing to replace an unmanaged command.' }
    if ($receipt.command -ne $target -or (Get-BridgeFileHash $target) -ne $receipt.sha256) {
        throw 'The existing command was modified or belongs to another installation.'
    }
}
# A command replaced while it was running was renamed aside; remove the ones no longer in use.
# Never while the command itself is missing: an interrupted replacement left the renamed one as the only copy.
if (Test-Path -LiteralPath $target) {
    Get-ChildItem -LiteralPath $CommandDirectory -Filter 'sessionbridge.exe.old-*' -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
}

function Invoke-BridgeWorkerControl([string]$Interpreter, [string]$Action) {
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
    $workerOutput = & $Interpreter -I -c $workerCode $Action
    if ($LASTEXITCODE -ne 0) { throw "Worker $Action failed; runtime was not safely updated." }
    return ($workerOutput | ConvertFrom-Json)
}

# The running worker belongs to the installation the receipt names, which may be another folder.
$previousRuntime = $runtimePython
if ($null -ne $receipt -and $receipt.project) {
    $recorded = if ($receipt.runtime) { $receipt.runtime } else { Join-Path $receipt.project '.venv' }
    $candidate = Join-Path $recorded 'Scripts\python.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $previousRuntime = $candidate }
}
$restartWorker = $false
if (Test-Path -LiteralPath $previousRuntime -PathType Leaf) {
    $workerBefore = Invoke-BridgeWorkerControl $previousRuntime 'status'
    $restartWorker = [bool]$workerBefore.running
    if ($restartWorker) {
        $workerStopped = Invoke-BridgeWorkerControl $previousRuntime 'stop'
        if ($workerStopped.running) { throw 'Worker is still running; refusing to replace its code.' }
    }
}
$installed = $false
try {
    if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
        & $Python -I -m venv $Runtime
        if ($LASTEXITCODE -ne 0) { throw 'Unable to create the isolated environment.' }
    }
    & $runtimePython -I -m pip install -r (Join-Path $projectRoot 'requirements.lock')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    $versionCode = "import sys, tomllib; print(tomllib.load(open(sys.argv[1], 'rb'))['project']['version'])"
    $versionOutput = & $runtimePython -I -c $versionCode (Join-Path $projectRoot 'pyproject.toml')
    if ($LASTEXITCODE -ne 0 -or -not $versionOutput) { throw 'Unable to read the package version.' }
    $version = "$versionOutput".Trim()
    # A release ZIP carries the wheel its checksums cover; a Git checkout builds from its sources.
    $wheel = Join-Path $projectRoot "dist\session_bridge-$version-py3-none-any.whl"
    $package = $projectRoot
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.git')) -and (Test-Path -LiteralPath $wheel -PathType Leaf)) {
        $package = $wheel
    }
    & $runtimePython -I -m pip install --no-deps --force-reinstall $package
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed.' }
    # Check the new command before it replaces the one in use.
    & $launcher --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Installed command verification failed.' }

    New-Item -ItemType Directory -Path $CommandDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $receiptRoot -Force | Out-Null
    $previousReceipt = $null
    if (Test-Path -LiteralPath $receiptPath) { $previousReceipt = Get-Content -LiteralPath $receiptPath -Raw }
    $aside = $null
    if (Test-Path -LiteralPath $target) {
        # Windows cannot overwrite a program that is running, but it can rename it.
        $aside = "$target.old-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff'))"
        Move-Item -LiteralPath $target -Destination $aside
    }
    try {
        Copy-Item -LiteralPath $launcher -Destination $target
        [ordered]@{ project = $projectRoot; runtime = $Runtime; command = $target; sha256 = (Get-BridgeFileHash $target) } |
            ConvertTo-Json | Set-Content -LiteralPath $receiptPath -Encoding utf8
        & $target --help | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Installed command verification failed.' }
    }
    catch {
        $swapFailure = $_
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue }
        if ($null -ne $aside) { Move-Item -LiteralPath $aside -Destination $target -ErrorAction SilentlyContinue }
        if ($null -ne $previousReceipt) { Set-Content -LiteralPath $receiptPath -Value $previousReceipt -Encoding utf8 -NoNewline }
        else { Remove-Item -LiteralPath $receiptPath -Force -ErrorAction SilentlyContinue }
        throw $swapFailure
    }
    $installed = $true
    if ($null -ne $aside) { Remove-Item -LiteralPath $aside -Force -ErrorAction SilentlyContinue }
}
catch {
    $installFailure = $_
    if ($restartWorker -and -not $installed) {
        try { $null = Invoke-BridgeWorkerControl $previousRuntime 'start' }
        catch { Write-Warning 'Worker restart failed; repair the installation, then run sessionbridge sync start.' }
    }
    throw $installFailure
}
if ($restartWorker) {
    # The new version is installed and in use from here on, so this is a warning, not a failure:
    # exit code 0 always means the receipt and the command name the new installation.
    $workerHealthy = $false
    try {
        $workerAfter = Invoke-BridgeWorkerControl $runtimePython 'start'
        $workerHealthy = [bool]($workerAfter.running -and $workerAfter.healthy)
    }
    catch { }
    if (-not $workerHealthy) {
        Write-Warning 'Updated, but worker restart could not be verified. Run sessionbridge sync worker-status.'
    }
}
Write-Host "Installed: $target"
if (($env:PATH -split ';') -notcontains $CommandDirectory) {
    Write-Host "Add $CommandDirectory to your user PATH to invoke sessionbridge by name."
}
