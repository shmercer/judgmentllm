#!/usr/bin/env bash
# Run JudgmentLLM's committed local-CI harness in the existing Dockur guest.
set -Eeuo pipefail

usage() {
  cat <<'HELP'
Usage: windows.sh --ref COMMIT [--repo PATH] [--mode prepare|check|doctor]
                  [--python-version 3.11|3.12|all] [--state-root PATH]
                  [--uv-archive PATH --uv-sha256 SHA256] [--stop-started]

The existing pairwise-windows-ci container is reused, never recreated. A VM
started by this launcher is stopped gracefully on exit; an already-running VM
is left running. The existing Windows CI lock is held for the whole operation.
Selecting all runs both Python lanes concurrently in separate guest processes,
with separate source, environment, scratch, output and logs. A failed lane does
not prevent its peer from finishing.

prepare may download Python/dependencies into JudgmentLLM-only directories;
it requires a separately downloaded, SHA-256-verified uv 0.13.6 Windows ZIP.
check uses prepared Python installations and an offline dependency cache.
doctor inventories the guest without installing tools or creating guest files.
--ref must be a full 40-character commit containing ci/local/gates.py.
--state-root must be /mnt/VM_Fast/pairwiseLLM-checks/judgmentllm-ci or one
of its subdirectories. The existing UNC share transfers source and logs.
No pairwise check is invoked.
HELP
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(cd -- "$script_dir/../.." && pwd -P)
ref=''
mode=check
python_version=all
state_root=/mnt/VM_Fast/pairwiseLLM-checks/judgmentllm-ci
uv_archive=''
uv_sha256=''
ssh_key=${JUDGMENTLLM_WINDOWS_SSH_KEY:-$HOME/.ssh/pairwise_windows_ci}
boot_timeout=${JUDGMENTLLM_WINDOWS_BOOT_TIMEOUT:-420}
container=pairwise-windows-ci
share_root=/mnt/VM_Fast/pairwiseLLM-checks
vm_started=0
active_ssh=''
active_tail=''
guest_started=0
registering_child=0
cancel_status=0

fatal() { printf '[judgmentllm-windows] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[judgmentllm-windows] %s\n' "$*" >&2; }
request_cancel() {
  cancel_status=$1
  # Finish registering a newly spawned child before cleanup can inspect it.
  if (( ! registering_child )); then exit "$cancel_status"; fi
}
while (( $# )); do
  case "$1" in
    --repo|--ref|--mode|--python-version|--state-root|--uv-archive|--uv-sha256)
      (( $# >= 2 )) || fatal "$1 requires a value"
      case "$1" in
        --repo) repo=$2 ;; --ref) ref=$2 ;; --mode) mode=$2 ;;
        --python-version) python_version=$2 ;; --state-root) state_root=$2 ;;
        --uv-archive) uv_archive=$2 ;; --uv-sha256) uv_sha256=$2 ;;
      esac
      shift 2 ;;
    --stop-started) shift ;; # This is also the default; never stop another job's VM.
    -h|--help) usage; exit 0 ;;
    *) fatal "Unknown argument: $1" ;;
  esac
done
[[ $mode =~ ^(prepare|check|doctor)$ ]] || fatal 'Invalid --mode'
[[ $python_version =~ ^(3\.11|3\.12|all)$ ]] || fatal 'Invalid --python-version'
[[ $boot_timeout =~ ^[0-9]+$ ]] && (( boot_timeout >= 10 )) || fatal 'Boot timeout must be >= 10 seconds'
[[ $mode == doctor || $ref =~ ^[0-9a-f]{40}$ ]] || fatal '--ref requires a full lowercase commit SHA'
[[ -z $ref || $ref =~ ^[0-9a-f]{40}$ ]] || fatal 'Invalid --ref'
for command_name in docker ssh python3 mountpoint flock realpath sha256sum tail timeout; do
  command -v "$command_name" >/dev/null || fatal "Missing command: $command_name"
done
mountpoint -q /mnt/VM_Fast || fatal '/mnt/VM_Fast must be mounted before using this VM'
[[ -f $ssh_key ]] || fatal "SSH key does not exist: $ssh_key"
repo=$(cd -- "$repo" && pwd -P) || fatal 'Cannot access --repo'
state_root=$(realpath -m -- "$state_root")
judgment_root="$share_root/judgmentllm-ci"
[[ $state_root == "$judgment_root" || $state_root == "$judgment_root/"* ]] || fatal '--state-root must remain inside the JudgmentLLM-only shared directory'
if [[ $mode == prepare ]]; then
  [[ -f $uv_archive && $uv_sha256 =~ ^[0-9a-f]{64}$ ]] || fatal 'prepare requires --uv-archive and --uv-sha256'
fi
if [[ -n $uv_archive || -n $uv_sha256 ]]; then
  [[ -f $uv_archive && $uv_sha256 =~ ^[0-9a-f]{64}$ ]] || fatal 'Supply both uv archive and its lowercase SHA-256'
  actual_uv_sha=$(sha256sum -- "$uv_archive")
  [[ ${actual_uv_sha%% *} == "$uv_sha256" ]] || fatal 'uv archive SHA-256 mismatch'
fi

# Share the established cross-project VM lock; leave the existing launcher intact.
exec 9>"$share_root/windows-ci/.local-ci-windows.lock"
flock -n 9 || fatal 'The Windows VM is already in use by a local CI launcher'
initial_state=$(docker inspect --format '{{.State.Status}}' "$container") || fatal 'Existing Windows container is unavailable'
[[ $initial_state == running || $initial_state == exited || $initial_state == created ]] || fatal "Unsupported VM state: $initial_state"

run_id="windows-$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
mkdir -p -- "$state_root/runs" "$state_root/results"
run_root="$state_root/runs/$run_id"
mkdir -- "$run_root"
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  set +e
  if (( guest_started )); then
    # Leave SSH connected until the guest closes and joins its owned lane job
    # and copies results. Grid sends TERM only to this adapter's PID.
    python3 - "$run_root" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
request = json.loads((root / "request.json").read_text())
identity = {key: request[key] for key in ("run_id", "commit", "source_archive_sha256", "adapter_sha256")}
temporary = root / "cancel.pending"
temporary.write_text(json.dumps(identity) + "\n")
temporary.replace(root / "cancel.request")
PY
    if [[ $mode != doctor ]]; then
      cancel_command=$(cat "$run_root/cancel-command.txt")
      ssh "${ssh_opts[@]}" pairwiseci@127.0.0.1 \
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $cancel_command" \
        >>"$run_root/cancellation.log" 2>&1 || { (( status != 0 )) || status=1; }
    fi
    if [[ -n $active_ssh ]]; then
      wait "$active_ssh" || true
      active_ssh=''
    fi
    if [[ $mode != doctor && ! -s $state_root/results/$run_id/adapter.json ]]; then
      log 'Collecting partial guest evidence after cancellation or SSH failure'
      collector=$(cat "$run_root/collect-command.txt")
      ssh "${ssh_opts[@]}" pairwiseci@127.0.0.1 \
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $collector" \
        >>"$run_root/cancellation.log" 2>&1 || { (( status != 0 )) || status=1; }
    fi
  fi
  if [[ -n $active_tail ]]; then
    # SSH has been joined; --pid makes this logger exit without a PID-based kill.
    wait "$active_tail" 2>/dev/null || true
  fi
  if (( vm_started )); then
    log 'Requesting graceful shutdown of the Windows VM started by this run'
    # A negative timeout disables Docker's forced-kill fallback.
    if ! docker stop --timeout=-1 "$container" >>"$run_root/lifecycle.log" 2>&1; then
      log 'Graceful shutdown failed; inspect the container before another run'
      (( status != 0 )) || status=4
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'request_cancel 130' INT
trap 'request_cancel 143' TERM
if [[ $initial_state != running ]]; then
  docker start "$container" >>"$run_root/lifecycle.log" 2>&1
  vm_started=1
fi

ssh_opts=(-p 2222 -i "$ssh_key" -o IdentitiesOnly=yes -o BatchMode=yes
  -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no
  -o StrictHostKeyChecking=yes -o ConnectionAttempts=1 -o ConnectTimeout=5)
deadline=$(( SECONDS + boot_timeout ))
ready=0
log "Waiting up to $boot_timeout seconds for Windows SSH"
while (( SECONDS < deadline )); do
  if timeout 10 ssh "${ssh_opts[@]}" pairwiseci@127.0.0.1 whoami >"$run_root/ssh-ready.log" 2>&1; then
    ready=1
    break
  fi
  sleep 5
done
(( ready )) || fatal "Windows SSH did not become ready; logs: $run_root"

if [[ $mode == doctor ]]; then
  cp -- "$script_dir/windows.ps1" "$run_root/windows.ps1"
else
  python3 "$script_dir/source.py" --repo "$repo" --ref "$ref" --output "$run_root/bundle"
  # Launch the script from the requested archive, never a mutable checkout copy.
  python3 - "$run_root/bundle/source.tar" "$run_root/windows.ps1" <<'PY'
import pathlib
import sys
import tarfile

with tarfile.open(sys.argv[1]) as archive:
    for name in ("ci/local/windows.ps1", "ci/local/gates.py"):
        member = archive.getmember(name)
        if not member.isfile():
            raise SystemExit(f"Required committed runner is not a regular file: {name}")
    stream = archive.extractfile("ci/local/windows.ps1")
    assert stream is not None
    pathlib.Path(sys.argv[2]).write_bytes(stream.read())
PY
fi
if [[ -n $uv_archive ]]; then cp -- "$uv_archive" "$run_root/uv.zip"; fi

encoded_command=$(python3 - "$run_root" "$share_root" "$mode" "$python_version" "$ref" "$uv_sha256" "$initial_state" <<'PY'
import base64
import hashlib
import json
import pathlib
import sys

root, share = map(pathlib.Path, sys.argv[1:3])
relative = root.relative_to(share).as_posix().replace("/", "\\")
unc = "\\\\host.lan\\Data\\" + relative
manifest_path = root / "bundle" / "manifest.json"
manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
request = {
    "run_id": root.name,
    "mode": sys.argv[3],
    "python_version": sys.argv[4],
    "commit": sys.argv[5],
    "transfer_root": unc,
    "results_root": "\\\\host.lan\\Data\\" + root.parent.parent.relative_to(share).as_posix().replace("/", "\\") + "\\results",
    "uv_sha256": sys.argv[6] or None,
    "adapter_sha256": hashlib.sha256((root / "windows.ps1").read_bytes()).hexdigest(),
    "source_archive_sha256": manifest["archive_sha256"] if manifest else None,
}
(root / "request.json").write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
(root / "host.json").write_text(json.dumps({**request, "initial_vm_state": sys.argv[7]}, indent=2) + "\n", encoding="utf-8")
quote = lambda value: "'" + value.replace("'", "''") + "'"
command = "& " + quote(unc + "\\windows.ps1") + " -Request " + quote(unc + "\\request.json")
(root / "collect-command.txt").write_text(base64.b64encode((command + " -Collect").encode("utf-16-le")).decode("ascii") + "\n")
(root / "cancel-command.txt").write_text(base64.b64encode((command + " -Cancel").encode("utf-16-le")).decode("ascii") + "\n")
print(base64.b64encode(command.encode("utf-16-le")).decode("ascii"))
PY
)
log "Running $mode ($python_version); host logs: $run_root"
set +e
registering_child=1
guest_started=1
ssh "${ssh_opts[@]}" pairwiseci@127.0.0.1 \
  "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded_command" \
  >"$run_root/windows.log" 2>&1 &
active_ssh=$!
registering_child=0
if (( cancel_status )); then exit "$cancel_status"; fi
registering_child=1
tail --pid="$active_ssh" -n +1 -f "$run_root/windows.log" &
active_tail=$!
registering_child=0
if (( cancel_status )); then exit "$cancel_status"; fi
wait "$active_ssh"
remote_status=$?
active_ssh=''
wait "$active_tail"
active_tail=''
set -e
if [[ $remote_status == 0 || $mode == doctor || -s $state_root/results/$run_id/adapter.json ]]; then
  # A completed failed lane is already joined and collected; preserve its
  # failure evidence without relabelling ordinary completion as cancellation.
  guest_started=0
fi
if [[ $mode != doctor ]]; then
  [[ -s $state_root/results/$run_id/adapter.json ]] || fatal "Guest result metadata is missing; inspect $run_root/windows.log"
  log "Guest results: $state_root/results/$run_id"
fi
exit "$remote_status"
