# JudgmentLLM-only guest adapter; invoked over SSH by windows.sh.
param(
    [Parameter(Mandatory = $true)][string]$Request,
    [ValidateSet('', '3.11', '3.12')][string]$Lane = '',
    [int]$OwnerProcessId = 0,
    [long]$OwnerStartedTicks = 0,
    [switch]$Collect,
    [switch]$Cancel
)

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
$resultRoot = Join-Path $runRoot 'results'
$cancelDirectory = Join-Path $workRoot 'cancel-requests'
$cancelPath = Join-Path $cancelDirectory ($settings.run_id + '.json')
$sharedCancelPath = Join-Path $settings.transfer_root 'cancel.request'
$joinedPath = Join-Path $runRoot 'workers-joined.json'
$transcript = Join-Path $resultRoot 'adapter.log'
$status = 1
$failure = $null
$lanes = @()
$transcriptStarted = $false
$ownedJob = $null
$workers = @()

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

function Test-Owner {
    param([int]$ProcessId, [long]$StartedTicks)
    try {
        $owner = Get-Process -Id $ProcessId -ErrorAction Stop
        return $owner.StartTime.ToUniversalTime().Ticks -eq $StartedTicks
    }
    catch { return $false }
}

function Test-CancelRequest {
    foreach ($path in @($cancelPath, $sharedCancelPath)) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $requestIdentity = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        if ($requestIdentity.run_id -ne $settings.run_id -or
            $requestIdentity.commit -ne $settings.commit -or
            $requestIdentity.source_archive_sha256 -ne $settings.source_archive_sha256 -or
            $requestIdentity.adapter_sha256 -ne $settings.adapter_sha256) {
            throw 'Cancellation identity differs from this run and source'
        }
        return $true
    }
    return $false
}

if ($Cancel) {
    # A separate SSH control invocation publishes locally: SMB negative path
    # caching must not delay adapter-PID cancellation. This does not create the
    # exclusive run directory, so cancellation can safely precede startup.
    New-Item -ItemType Directory -Path $cancelDirectory -Force | Out-Null
    if (-not (Test-Path -LiteralPath $cancelPath)) {
        $temporary = Join-Path $cancelDirectory ('.pending-' + [guid]::NewGuid().ToString('N'))
        try {
            [ordered]@{
                run_id = $settings.run_id
                commit = $settings.commit
                source_archive_sha256 = $settings.source_archive_sha256
                adapter_sha256 = $settings.adapter_sha256
            } | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
            [IO.File]::Move($temporary, $cancelPath)
        }
        finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
    }
    if (-not (Test-CancelRequest)) { throw 'Cancellation request was not published' }
    exit 0
}

function Copy-Results {
    param([switch]$Partial)
    $destination = if ($Partial) {
        Join-Path $settings.transfer_root 'cancelled-results'
    } else {
        Join-Path $settings.results_root $settings.run_id
    }
    if (Test-Path -LiteralPath $destination) { throw "Refusing to overwrite shared results: $destination" }
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    if (Test-Path -LiteralPath $resultRoot) {
        Copy-Item -Path (Join-Path $resultRoot '*') -Destination $destination -Recurse
    }
}

if ($Collect) {
    # A second SSH connection can retain partial logs after the original client
    # disappeared. Never collect while this run still owns active lane work.
    $identityPath = Join-Path $runRoot 'supervisor.json'
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ((Test-Path -LiteralPath $identityPath) -and -not (Test-Path -LiteralPath $joinedPath)) {
        $identity = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
        if (-not (Test-Owner $identity.pid $identity.started_ticks)) { break }
        if ($timer.Elapsed.TotalSeconds -ge 60) { throw 'Guest supervisor has not confirmed cancellation' }
        Start-Sleep -Milliseconds 200
    }
    if (Test-Path -LiteralPath $resultRoot) { Copy-Results -Partial }
    exit 0
}

if ($Lane) {
    if ($settings.python_version -ne 'all' -and $settings.python_version -ne $Lane) { throw 'Unexpected lane' }
    if ($OwnerProcessId -le 0 -or $OwnerStartedTicks -le 0) { throw 'Missing supervisor identity' }
    $startGate = Join-Path $runRoot "lane-$Lane.start"
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path -LiteralPath $startGate)) {
        if ((Test-CancelRequest) -or -not (Test-Owner $OwnerProcessId $OwnerStartedTicks)) { exit 125 }
        if ($timer.Elapsed.TotalSeconds -ge 60) { throw 'Lane was not assigned to its owned job' }
        Start-Sleep -Milliseconds 100
    }
    if ((Test-CancelRequest) -or -not (Test-Owner $OwnerProcessId $OwnerStartedTicks)) { exit 125 }
    $patch = if ($Lane -eq '3.11') { '3.11.18' } else { '3.12.16' }
    $laneLog = Join-Path $resultRoot "python-$Lane.log"
    $env:TEMP = Join-Path $runRoot "temp-$Lane"
    $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP | Out-Null
    try {
        if ($settings.mode -eq 'prepare') {
            $env:UV_PYTHON_DOWNLOADS = 'automatic'
            $env:UV_OFFLINE = '0'
            Invoke-Logged $uvExe @('python', 'install', $patch, '--no-bin', '--no-registry', '--install-dir', $env:UV_PYTHON_INSTALL_DIR) $laneLog
        }
        $env:UV_PYTHON_DOWNLOADS = 'never'
        $env:UV_OFFLINE = '1'
        $archive = Join-Path $runRoot 'source.tar'
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $settings.source_archive_sha256) {
            throw 'Lane source archive SHA-256 mismatch'
        }
        $source = Join-Path $runRoot "source-$Lane"
        New-Item -ItemType Directory -Path $source | Out-Null
        Invoke-Logged 'tar.exe' @('-xf', $archive, '-C', $source) $laneLog
        Copy-Item -LiteralPath (Join-Path $runRoot 'manifest.json') -Destination (Join-Path $source '.judgmentllm-source.json')
        $gates = Join-Path $source 'ci\local\gates.py'
        if (-not (Test-Path -LiteralPath $gates -PathType Leaf)) { throw 'The requested commit does not contain ci/local/gates.py' }
        $output = Join-Path $resultRoot "python-$Lane"
        $arguments = @('run', '--no-project', '--python', $patch, '--offline', 'python', $gates,
            '--source', $source, '--output', $output, '--commit', $settings.commit,
            '--archive-sha256', $settings.source_archive_sha256, '--python-version', $Lane,
            '--mode', $settings.mode, '--uv', $uvExe)
        if ($settings.mode -eq 'prepare') { $env:UV_OFFLINE = '0' }
        Invoke-Logged $uvExe $arguments $laneLog
        $status = 0
    }
    catch { $failure = $_.Exception.Message; Write-Host $failure }
    finally {
        [ordered]@{ python_version = $Lane; exit_code = $status; failure = $failure } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $resultRoot "python-$Lane.worker.json") -Encoding UTF8
    }
    exit $status
}

if (Test-Path -LiteralPath $runRoot) { throw "Refusing to overwrite guest run: $runRoot" }
New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null
$ownerStartedTicks = (Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks
[ordered]@{ pid = $PID; started_ticks = $ownerStartedTicks } | ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $runRoot 'supervisor.json') -Encoding UTF8

try {
    Start-Transcript -LiteralPath $transcript | Out-Null
    $transcriptStarted = $true
    if (Test-CancelRequest) { throw 'Host cancelled before guest work started' }
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

    # A job handle is not inherited. Its final close (including supervisor death)
    # kills only these freshly launched lane trees; no VM/SSH process is assigned.
    # https://learn.microsoft.com/windows/win32/procthread/job-objects
    $env:TEMP = Join-Path $runRoot 'job-temp'
    $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP | Out-Null
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public sealed class JudgmentLlmLaneJob : IDisposable {
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
        public uint ActiveProcesses;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters {
        public ulong ReadOperations, WriteOperations, OtherOperations;
        public ulong ReadBytes, WriteBytes, OtherBytes;
    }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential)] struct Accounting {
        public long UserTime, KernelTime, PeriodUserTime, PeriodKernelTime;
        public uint PageFaults, TotalProcesses, ActiveProcesses, TerminatedProcesses;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateJobObjectW(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(SafeFileHandle job, int kind, ref ExtendedLimits value, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(SafeFileHandle job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateJobObject(SafeFileHandle job, uint exitCode);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool QueryInformationJobObject(SafeFileHandle job, int kind, out Accounting value, uint size, IntPtr returned);
    readonly SafeFileHandle handle;
    public JudgmentLlmLaneJob() {
        handle = CreateJobObjectW(IntPtr.Zero, null);
        if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
        var limits = new ExtendedLimits();
        limits.Basic.Flags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no resource limits.
        if (!SetInformationJobObject(handle, 9, ref limits, (uint)Marshal.SizeOf(limits))) {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error);
        }
    }
    public void Assign(Process process) {
        if (!AssignProcessToJobObject(handle, process.Handle))
            throw new Win32Exception(Marshal.GetLastWin32Error());
    }
    public void StopAndJoin() {
        if (!TerminateJobObject(handle, 125)) throw new Win32Exception(Marshal.GetLastWin32Error());
        Accounting value;
        do {
            if (!QueryInformationJobObject(handle, 1, out value, (uint)Marshal.SizeOf(typeof(Accounting)), IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            if (value.ActiveProcesses != 0) System.Threading.Thread.Sleep(20);
        } while (value.ActiveProcesses != 0);
    }
    public void Dispose() { handle.Dispose(); }
}
'@
    $ownedJob = New-Object JudgmentLlmLaneJob
    $localScript = Join-Path $runRoot 'adapter.ps1'
    $localRequest = Join-Path $runRoot 'request.json'
    Copy-Item -LiteralPath $MyInvocation.MyCommand.Path -Destination $localScript
    Copy-Item -LiteralPath $Request -Destination $localRequest
    $versions = if ($settings.python_version -eq 'all') { @('3.11', '3.12') } else { @($settings.python_version) }
    foreach ($minor in $versions) {
        if (Test-CancelRequest) { throw 'Host cancelled before lane startup' }
        $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $localScript,
            '-Request', $localRequest, '-Lane', $minor, '-OwnerProcessId', $PID, '-OwnerStartedTicks', $ownerStartedTicks)
        $process = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -PassThru `
            -RedirectStandardOutput (Join-Path $resultRoot "python-$minor.stdout.log") `
            -RedirectStandardError (Join-Path $resultRoot "python-$minor.stderr.log")
        $null = $process.Handle
        $worker = @{ process = $process; assigned = $false; python_version = $minor; status = 'running' }
        $workers += $worker
        $ownedJob.Assign($process)
        $worker.assigned = $true
        # Children cannot start uv/Python before job ownership is established.
        New-Item -ItemType File -Path (Join-Path $runRoot "lane-$minor.start") | Out-Null
        Write-Host "Python $minor lane started (PID $($process.Id))"
    }
    while (@($workers | Where-Object { $_.status -eq 'running' }).Count) {
        if (Test-CancelRequest) {
            $failure = 'Host cancelled this run'
            $ownedJob.StopAndJoin()
            $ownedJob.Dispose()
            $ownedJob = $null
            foreach ($worker in $workers) {
                if ($worker.status -eq 'running') { $worker.status = 'cancelled' }
            }
            break
        }
        foreach ($worker in $workers) {
            if ($worker.status -ne 'running') { continue }
            $worker.process.Refresh()
            if ($worker.process.HasExited) {
                $worker.process.WaitForExit()
                $worker.exit_code = $worker.process.ExitCode
                $worker.status = if ($worker.exit_code -eq 0) { 'passed' } else { 'failed' }
                Write-Host "Python $($worker.python_version) lane $($worker.status)"
            }
        }
        Start-Sleep -Milliseconds 500
    }
    if (@($workers | Where-Object { $_.status -ne 'passed' }).Count -eq 0) { $status = 0 }
}
catch {
    $failure = $_.Exception.Message
    Write-Host "JudgmentLLM Windows adapter failed: $failure"
}
finally {
    if ($ownedJob) { $ownedJob.StopAndJoin(); $ownedJob.Dispose() }
    foreach ($worker in $workers) {
        if (-not $worker.assigned -and -not $worker.process.HasExited) {
            # An unassigned child is still behind its startup gate and has never
            # launched native work. Only this freshly owned process is stopped.
            try { $worker.process.Kill() }
            catch { if (-not $worker.process.HasExited) { throw } }
        }
        $worker.process.WaitForExit()
        if ($worker.status -eq 'running') { $worker.status = 'cancelled' }
        $lanes += [ordered]@{
            python_version = $worker.python_version
            status = $worker.status
            exit_code = $worker.process.ExitCode
            output = (Join-Path $resultRoot "python-$($worker.python_version)")
        }
        $worker.process.Dispose()
    }
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
    [ordered]@{ joined = $true; lanes = $lanes } | ConvertTo-Json -Depth 6 |
        Set-Content -LiteralPath $joinedPath -Encoding UTF8
    Copy-Results
}
exit $status
