# JudgmentLLM-only guest adapter; invoked over SSH by windows.sh.
param([Parameter(Mandatory = $true)][string]$Request)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$settings = Get-Content -LiteralPath $Request -Raw | ConvertFrom-Json
if ($settings.mode -notin @('prepare', 'check', 'doctor')) { throw 'Invalid mode' }
if ($settings.python_version -notin @('3.11', '3.12', 'all')) { throw 'Invalid Python version' }
if ($settings.run_id -notmatch '^windows-[A-Za-z0-9-]+$') { throw 'Invalid run ID' }
if ((Get-FileHash -LiteralPath $MyInvocation.MyCommand.Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $settings.adapter_sha256) {
    throw 'Guest adapter bytes differ from the staged commitment'
}

$workRoot = 'C:\JudgmentLLM-ci'
$uvExe = Join-Path $workRoot 'tools\uv-0.13.6\uv.exe'
# Official uv 0.13.6 x86_64-pc-windows-msvc ZIP, then its embedded uv.exe.
$uvArchiveSha256 = 'a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f'
$uvExeSha256 = '81ea623608f8c17720c62efd0a380a8ead380c4d365ba6968e9d279a2319c674'
$env:UV_CACHE_DIR = Join-Path $workRoot 'cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $workRoot 'python'
$env:UV_NO_CONFIG = '1'
$env:UV_NO_PROGRESS = '1'
$env:UV_MANAGED_PYTHON = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:NO_COLOR = '1'
$rBin = 'C:\Program Files\R\R-4.6.1\bin'
$env:PATH = "$rBin;$env:PATH"

if ($settings.mode -eq 'doctor') {
    $rscript = Join-Path $rBin 'Rscript.exe'
    [ordered]@{
        identity = (& whoami.exe)
        powershell = $PSVersionTable.PSVersion.ToString()
        os = [Environment]::OSVersion.VersionString
        uv_exists = (Test-Path -LiteralPath $uvExe -PathType Leaf)
        python_root_exists = (Test-Path -LiteralPath $env:UV_PYTHON_INSTALL_DIR)
        rscript_exists = (Test-Path -LiteralPath $rscript -PathType Leaf)
        work_root = $workRoot
        free_bytes = (Get-PSDrive C).Free
    } | ConvertTo-Json
    if (Test-Path -LiteralPath $uvExe -PathType Leaf) {
        if ((Get-FileHash -LiteralPath $uvExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $uvExeSha256) {
            throw 'Isolated uv executable hash differs from the pinned release'
        }
        & $uvExe --version
    }
    if (Test-Path -LiteralPath $rscript -PathType Leaf) {
        & $rscript --vanilla -e "cat('R ', as.character(getRversion()), '\njsonlite ', as.character(packageVersion('jsonlite')), '\n', sep='')"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    exit 0
}

if ($settings.commit -notmatch '^[0-9a-f]{40}$' -or $settings.source_archive_sha256 -notmatch '^[0-9a-f]{64}$') {
    throw 'Invalid source commitment'
}
$runRoot = Join-Path (Join-Path $workRoot 'runs') $settings.run_id
if (Test-Path -LiteralPath $runRoot) { throw "Refusing to overwrite guest run: $runRoot" }
$resultRoot = Join-Path $runRoot 'results'
New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null
$transcript = Join-Path $resultRoot 'adapter.log'
$status = 1
$failure = $null
$lanes = @()
$transcriptStarted = $false

function Invoke-Logged {
    param([string]$Executable, [string[]]$Arguments, [string]$Log)
    Get-Command $Executable -ErrorAction Stop | Out-Null
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Executable @Arguments 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $Log -Append | Write-Host
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw "Command failed with exit code ${code}: $Executable; log: $Log" }
}

try {
    Start-Transcript -LiteralPath $transcript | Out-Null
    $transcriptStarted = $true
    $archive = Join-Path $runRoot 'source.tar'
    $manifest = Join-Path $runRoot 'manifest.json'
    Copy-Item -LiteralPath (Join-Path $settings.transfer_root 'bundle\source.tar') -Destination $archive
    Copy-Item -LiteralPath (Join-Path $settings.transfer_root 'bundle\manifest.json') -Destination $manifest
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $settings.source_archive_sha256) {
        throw 'Source archive SHA-256 mismatch'
    }
    $sourceManifest = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
    if ($sourceManifest.commit -ne $settings.commit -or $sourceManifest.archive_sha256 -ne $settings.source_archive_sha256) {
        throw 'Source manifest differs from the requested commit/archive'
    }

    if (-not (Test-Path -LiteralPath $uvExe -PathType Leaf)) {
        if ($settings.mode -ne 'prepare') { throw 'Isolated uv is missing; run prepare first' }
        if ($settings.uv_sha256 -ne $uvArchiveSha256) { throw 'prepare requires the pinned official uv ZIP hash' }
        $uvZip = Join-Path $runRoot 'uv.zip'
        Copy-Item -LiteralPath (Join-Path $settings.transfer_root 'uv.zip') -Destination $uvZip
        if ((Get-FileHash -LiteralPath $uvZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $settings.uv_sha256) {
            throw 'uv ZIP SHA-256 mismatch'
        }
        $unpacked = Join-Path $runRoot 'uv-unpacked'
        Expand-Archive -LiteralPath $uvZip -DestinationPath $unpacked
        $executables = @(Get-ChildItem -LiteralPath $unpacked -Recurse -File -Filter uv.exe)
        if ($executables.Count -ne 1) { throw 'uv ZIP must contain exactly one uv.exe' }
        $toolRoot = Split-Path -Parent $uvExe
        if (Test-Path -LiteralPath $toolRoot) { throw 'Isolated uv tool directory exists but is incomplete; inspect it manually' }
        New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
        Copy-Item -LiteralPath $executables[0].FullName -Destination $uvExe
    }
    if ((Get-FileHash -LiteralPath $uvExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $uvExeSha256) {
        throw 'Isolated uv executable hash differs from the pinned release'
    }
    $uvVersion = (& $uvExe --version)
    if ($LASTEXITCODE -ne 0 -or $uvVersion -notmatch '^uv 0\.13\.6(?:\s|$)') { throw 'Expected isolated uv 0.13.6' }

    $versions = if ($settings.python_version -eq 'all') { @('3.11', '3.12') } else { @($settings.python_version) }
    foreach ($minor in $versions) {
        $patch = if ($minor -eq '3.11') { '3.11.18' } else { '3.12.16' }
        $laneLog = Join-Path $resultRoot "python-$minor.log"
        if ($settings.mode -eq 'prepare') {
            $env:UV_PYTHON_DOWNLOADS = 'automatic'
            $env:UV_OFFLINE = '0'
            Invoke-Logged $uvExe @('python', 'install', $patch, '--no-bin', '--no-registry', '--install-dir', $env:UV_PYTHON_INSTALL_DIR) $laneLog
        }
        $env:UV_PYTHON_DOWNLOADS = 'never'
        $env:UV_OFFLINE = '1'
        $source = Join-Path $runRoot "source-$minor"
        New-Item -ItemType Directory -Path $source | Out-Null
        Invoke-Logged 'tar.exe' @('-xf', $archive, '-C', $source) $laneLog
        Copy-Item -LiteralPath $manifest -Destination (Join-Path $source '.judgmentllm-source.json')
        $gates = Join-Path $source 'ci\local\gates.py'
        if (-not (Test-Path -LiteralPath $gates -PathType Leaf)) { throw 'The requested commit does not contain ci/local/gates.py' }
        $output = Join-Path $resultRoot "python-$minor"
        $arguments = @('run', '--no-project', '--python', $patch, '--offline', 'python', $gates,
            '--source', $source, '--output', $output, '--commit', $settings.commit,
            '--archive-sha256', $settings.source_archive_sha256, '--python-version', $minor,
            '--mode', $settings.mode, '--uv', $uvExe)
        # Bootstrap uses only the installed interpreter. The gate decides when
        # prepare may access the network; check's children remain offline.
        if ($settings.mode -eq 'prepare') { $env:UV_OFFLINE = '0' }
        Invoke-Logged $uvExe $arguments $laneLog
        $lanes += [ordered]@{ python_version = $minor; python_patch = $patch; status = 'passed'; output = $output }
    }
    $status = 0
}
catch {
    $failure = $_.Exception.Message
    Write-Host "JudgmentLLM Windows adapter failed: $failure"
}
finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    [ordered]@{
        run_id = $settings.run_id
        mode = $settings.mode
        commit = $settings.commit
        archive_sha256 = $settings.source_archive_sha256
        adapter_sha256 = $settings.adapter_sha256
        os = [Environment]::OSVersion.VersionString
        powershell = $PSVersionTable.PSVersion.ToString()
        uv_version = '0.13.6'
        lanes = $lanes
        exit_code = $status
        failure = $failure
        guest_run_root = $runRoot
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $resultRoot 'adapter.json') -Encoding UTF8
    $destination = Join-Path $settings.results_root $settings.run_id
    if (Test-Path -LiteralPath $destination) { throw "Refusing to overwrite shared results: $destination" }
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    Copy-Item -Path (Join-Path $resultRoot '*') -Destination $destination -Recurse
}
exit $status
