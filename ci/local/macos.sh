#!/usr/bin/env bash
# JudgmentLLM adapter for the existing local Quickemu macOS guest.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd -- "$SCRIPT_DIR/../.." && pwd)
REF=
STATE_ROOT=/mnt/VM_Fast/judgmentllm-ci/macos
MODE=check
PYTHON_VERSION=all
UV_ARCHIVE=
UV_SHA256=
UV_RELEASE_ARCHIVE_SHA256=63ba17fdda55ad1a230d4b37fc6017ffa10c23688c83818bd3bfdf914c652a02
UV_RELEASE_BINARY_SHA256=a94795fa09796e20b220c237410465fb40a334657816d58cf7c4f4df4bc8239e
STOP_STARTED=1
VM_ROOT=/mnt/VM_Fast/VMs/quickemu
VM_NAME=macos-sequoia
QUICKEMU=/mnt/VM_Fast/tools/quickemu-source/quickemu
SHARED_LOCK=/mnt/VM_Fast/pairwiseLLM-checks/macos-ci/.ci.lock
SSH_KEY="$HOME/.ssh/pairwise_macos_ci"
BOOT_TIMEOUT=${JUDGMENTLLM_MACOS_BOOT_TIMEOUT:-300}
STARTED=0
GUEST_STAGED=0
RESULTS_COPIED=0
ARCHIVE_SHA256=
HOST_RUN=
RUN_ID=

usage() {
  cat <<'HELP'
Usage: macos.sh [--repo PATH] --ref SHA [options]

  --state-root PATH             Evidence root beneath /mnt/VM_Fast/judgmentllm-ci/
  --mode doctor|prepare|check    Inventory, install prerequisites, or run offline gates
  --python-version 3.11|3.12|all Python minors (exact patches: 3.11.18, 3.12.16)
  --uv-archive PATH             Official uv 0.13.6 darwin-x86_64 archive (prepare only)
  --uv-sha256 SHA               Independently verified archive SHA-256 (prepare only)
  --stop-started                Gracefully stop only a VM this invocation starts (default)

Uses the existing pairwise macOS CI lock, VM configuration and SSH identity.
Never stops a preexisting VM, changes host kernel settings, or force-kills a VM.
prepare may access the network; check and its test gates run offline.
doctor needs no --ref and does not install tools or create guest files.
HELP
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
while (($#)); do
  case "$1" in
    --repo|--ref|--state-root|--mode|--python-version|--uv-archive|--uv-sha256)
      (($# >= 2)) || fail "$1 requires a value"
      option=$1; value=$2; shift 2
      case "$option" in
        --repo) REPO=$value ;; --ref) REF=$value ;; --state-root) STATE_ROOT=$value ;;
        --mode) MODE=$value ;; --python-version) PYTHON_VERSION=$value ;;
        --uv-archive) UV_ARCHIVE=$value ;; --uv-sha256) UV_SHA256=$value ;;
      esac ;;
    --stop-started) STOP_STARTED=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done
[[ "$MODE" == doctor || "$MODE" == prepare || "$MODE" == check ]] || fail 'invalid --mode'
[[ "$MODE" == doctor || "$REF" =~ ^[0-9a-f]{40}$ ]] || fail '--ref must be a full lowercase 40-character commit SHA'
[[ -z "$REF" || "$REF" =~ ^[0-9a-f]{40}$ ]] || fail 'invalid --ref'
[[ "$PYTHON_VERSION" == 3.11 || "$PYTHON_VERSION" == 3.12 || "$PYTHON_VERSION" == all ]] || fail 'invalid --python-version'
[[ "$STATE_ROOT" == /* ]] || fail '--state-root must be absolute'
[[ "$BOOT_TIMEOUT" =~ ^[0-9]+$ ]] && ((BOOT_TIMEOUT >= 10)) || fail 'boot timeout must be at least 10 seconds'
[[ -x "$QUICKEMU" && -f "$VM_ROOT/$VM_NAME.conf" && -f "$VM_ROOT/$VM_NAME/disk.qcow2" ]] || fail 'existing Quickemu installation is incomplete'
[[ -r "$SSH_KEY" && -f "$SHARED_LOCK" ]] || fail 'existing SSH identity or shared CI lock is missing'
command -v python3 >/dev/null || fail 'host python3 is required'
command -v flock >/dev/null || fail 'host flock is required'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Host Python >=3.11 is required")'
mountpoint -q /mnt/VM_Fast || fail '/mnt/VM_Fast must be mounted'
STATE_ROOT=$(realpath -m -- "$STATE_ROOT")
[[ "$STATE_ROOT" == /mnt/VM_Fast/judgmentllm-ci/* ]] || fail 'state root must be beneath /mnt/VM_Fast/judgmentllm-ci/'
if [[ "$MODE" == prepare ]]; then
  [[ -f "$UV_ARCHIVE" && "$UV_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail 'prepare requires --uv-archive and --uv-sha256'
  [[ "$UV_SHA256" == "$UV_RELEASE_ARCHIVE_SHA256" ]] || fail 'expected pinned official uv 0.13.6 darwin-x86_64 archive SHA-256'
elif [[ -n "$UV_ARCHIVE" || -n "$UV_SHA256" ]]; then
  fail 'uv bootstrap arguments apply only to prepare'
fi

# The pairwise runner uses this same lock. Keep it through collection/shutdown.
exec 9>"$SHARED_LOCK"
flock -n 9 || fail 'another macOS CI operation holds the existing shared lock'
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${REF:0:12}-$MODE-$$"
HOST_RUN="$STATE_ROOT/runs/$RUN_ID"
mkdir -p "$STATE_ROOT/runs"
mkdir "$HOST_RUN"
exec > >(tee "$HOST_RUN/console.log") 2>&1

SSH=(ssh -T -i "$SSH_KEY" -p 22230 -o BatchMode=yes -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes -o ConnectTimeout=5
  -o ServerAliveInterval=30 -o ServerAliveCountMax=4)
ssh_guest() {
  if [[ "${1-}" == -n ]]; then
    shift
    "${SSH[@]}" -n pairwiseci@127.0.0.1 "$@"
  else
    "${SSH[@]}" pairwiseci@127.0.0.1 "$@"
  fi
}
vm_running() { pgrep -x "$VM_NAME" >/dev/null 2>&1; }

collect_results() {
  ((GUEST_STAGED && !RESULTS_COPIED)) || return 0
  if ssh_guest -n "cd \"\$HOME/judgmentllm-ci/runs/$RUN_ID\" && tar -cf - results" >"$HOST_RUN/guest-results.tar"; then
    mkdir "$HOST_RUN/guest"
    if tar -xf "$HOST_RUN/guest-results.tar" -C "$HOST_RUN/guest"; then
      RESULTS_COPIED=1
    else
      return 1
    fi
  else
    printf 'Guest results could not be collected; guest workspace is preserved.\n' >&2
    return 1
  fi
}

finish() {
  local status=$? shutdown=not_started attempt
  trap - EXIT INT TERM
  set +e
  if ! collect_results; then ((status != 0)) || status=2; fi
  if ((STARTED && STOP_STARTED)) && vm_running; then
    shutdown=requested
    printf 'Requesting graceful shutdown of the VM started by this adapter.\n'
    if ! ssh_guest -n 'sudo -n /sbin/shutdown -h now' >/dev/null 2>&1; then
      # This existing socket is an HMP monitor, not QMP. ACPI powerdown is the
      # only permitted fallback; never send quit, stop or process signals.
      python3 - "$VM_ROOT/$VM_NAME/$VM_NAME-monitor.socket" <<'PY'
import socket
import sys
try:
    with socket.socket(socket.AF_UNIX) as monitor:
        monitor.settimeout(3)
        monitor.connect(sys.argv[1])
        monitor.sendall(b"system_powerdown\n")
except OSError:
    pass
PY
    fi
    for ((attempt=0; attempt<36; attempt++)); do
      if ! vm_running; then shutdown=stopped; break; fi
      if ((attempt % 6 == 0)); then printf 'Waiting for graceful macOS shutdown (%ss).\n' "$((attempt * 5))"; fi
      sleep 5
    done
    if vm_running; then
      shutdown=environment_limitation_vm_preserved
      printf 'ENVIRONMENT LIMITATION: graceful shutdown did not finish; no force-kill attempted.\n' >&2
      ((status != 0)) || status=2
    fi
  elif ((STARTED)); then shutdown=already_exited
  elif vm_running; then shutdown=preexisting_vm_preserved
  fi
  python3 - "$HOST_RUN" "$status" "$REF" "$ARCHIVE_SHA256" "$MODE" "$PYTHON_VERSION" "$STARTED" "$shutdown" "$RESULTS_COPIED" <<'PY'
import json
import pathlib
import sys
root, code, commit, digest, mode, versions, started, shutdown, copied = sys.argv[1:]
data = {"schema_version": 1, "platform": "macos", "mode": mode,
        "commit": commit, "archive_sha256": digest or None,
        "python_version": versions, "exit_code": int(code),
        "status": "passed" if code == "0" else "failed",
        "started_vm": started == "1", "shutdown": shutdown,
        "guest_results_copied": copied == "1"}
(pathlib.Path(root) / "adapter.json").write_text(json.dumps(data, indent=2) + "\n")
PY
  printf 'macOS adapter results: %s (exit %s)\n' "$HOST_RUN" "$status"
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$MODE" != doctor ]]; then
  python3 "$SCRIPT_DIR/source.py" --repo "$REPO" --ref "$REF" --output "$HOST_RUN/archive"
  ARCHIVE_SHA256=$(python3 - "$HOST_RUN/archive/source.tar" <<'PY'
import hashlib
import pathlib
import sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
  )
fi

UV_BINARY_SHA256=none
if [[ "$MODE" == prepare ]]; then
  mkdir "$HOST_RUN/bootstrap"
  UV_BINARY_SHA256=$(python3 - "$UV_ARCHIVE" "$UV_SHA256" "$HOST_RUN/bootstrap/uv" "$UV_RELEASE_BINARY_SHA256" <<'PY'
import hashlib
import pathlib
import sys
import tarfile
archive, expected, output, binary_expected = sys.argv[1:]
if hashlib.sha256(pathlib.Path(archive).read_bytes()).hexdigest() != expected:
    raise SystemExit("uv bootstrap archive SHA-256 does not match")
with tarfile.open(archive) as package:
    member = package.getmember("uv-x86_64-apple-darwin/uv")
    if not member.isfile():
        raise SystemExit("uv archive must contain a regular darwin-x86_64 uv binary")
    stream = package.extractfile(member)
    if stream is None:
        raise SystemExit("uv binary is unavailable")
    data = stream.read()
if hashlib.sha256(data).hexdigest() != binary_expected:
    raise SystemExit("uv binary does not match pinned official release bytes")
path = pathlib.Path(output)
path.write_bytes(data)
path.chmod(0o700)
print(hashlib.sha256(data).hexdigest())
PY
)
fi

if vm_running; then
  printf 'Reusing existing macOS VM; it will remain running.\n'
else
  printf 'Starting existing macOS VM without changing host settings. ignore_msrs='
  cat /sys/module/kvm/parameters/ignore_msrs
  STARTED=1
  if ! (cd "$VM_ROOT" && "$QUICKEMU" --vm "$VM_NAME.conf" --display none --viewer none) >"$HOST_RUN/boot.log" 2>&1; then
    fail 'Quickemu startup failed; inspect preserved boot.log'
  fi
fi
deadline=$((SECONDS + BOOT_TIMEOUT))
until ssh_guest -n 'test -x /usr/local/bin/Rscript && test -x /usr/bin/python3' >/dev/null 2>&1; do
  vm_running || fail 'ENVIRONMENT LIMITATION: macOS exited while waiting for SSH'
  ((SECONDS < deadline)) || fail 'ENVIRONMENT LIMITATION: macOS SSH readiness timed out'
  printf 'Waiting for macOS SSH (elapsed %ss).\n' "$SECONDS"
  sleep 5
done
ssh_guest -n 'sw_vers -productVersion; uname -m; /usr/bin/python3 --version; /usr/local/bin/Rscript --version; df -h "$HOME"' | tee "$HOST_RUN/guest-inventory.txt"
if [[ "$MODE" == doctor ]]; then
  ssh_guest /bin/bash -s <<'DOCTOR'
set -eu
ROOT="$HOME/judgmentllm-ci"
PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
for tool in python3.11 python3.12 uv; do command -v "$tool" || true; done
if test -x "$ROOT/tools/uv-0.13.6/uv"; then "$ROOT/tools/uv-0.13.6/uv" --version; fi
for python in "$ROOT"/python/cpython-3.11.18-*/bin/python3.11 "$ROOT"/python/cpython-3.12.16-*/bin/python3.12; do
  if test -x "$python"; then "$python" --version; fi
done
/usr/local/bin/Rscript --vanilla -e 'p <- file.path(Sys.getenv("HOME"), c("judgmentllm-ci/r-library", "pairwise-ci/library/R-4.6-x86_64")); .libPaths(c(p, .libPaths())); stopifnot(requireNamespace("jsonlite", quietly=TRUE)); cat("jsonlite", as.character(packageVersion("jsonlite")), "from", find.package("jsonlite"), "\n")'
DOCTOR
  exit 0
fi

ssh_guest -n "mkdir -p \"\$HOME/judgmentllm-ci/runs/$RUN_ID\" && mkdir \"\$HOME/judgmentllm-ci/runs/$RUN_ID/source\" \"\$HOME/judgmentllm-ci/runs/$RUN_ID/results\""
GUEST_STAGED=1
tar -cf - -C "$HOST_RUN/archive" source.tar manifest.json |
  ssh_guest "tar -xf - -C \"\$HOME/judgmentllm-ci/runs/$RUN_ID\""
if [[ "$MODE" == prepare ]]; then
  tar -cf - -C "$HOST_RUN/bootstrap" uv |
    ssh_guest "mkdir \"\$HOME/judgmentllm-ci/runs/$RUN_ID/bootstrap\" && tar -xf - -C \"\$HOME/judgmentllm-ci/runs/$RUN_ID/bootstrap\""
fi

ssh_guest /bin/bash -s -- "$RUN_ID" "$MODE" "$PYTHON_VERSION" "$REF" "$ARCHIVE_SHA256" "$UV_BINARY_SHA256" "${UV_SHA256:-none}" <<'GUEST'
set -euo pipefail
RUN_ID=$1 MODE=$2 PYTHON_VERSION=$3 REF=$4 ARCHIVE_SHA256=$5 UV_BINARY_SHA256=$6 UV_ARCHIVE_SHA256=$7
ROOT="$HOME/judgmentllm-ci"
RUN="$ROOT/runs/$RUN_ID"
SOURCE="$RUN/source"
UV="$ROOT/tools/uv-0.13.6/uv"
export PATH="$ROOT/tools/uv-0.13.6:/usr/local/bin:$PATH"
export UV_CACHE_DIR="$ROOT/cache/uv"
export UV_PYTHON_INSTALL_DIR="$ROOT/python"
export UV_PYTHON_BIN_DIR="$ROOT/bin"
export UV_NO_PROGRESS=1
export R_LIBS_USER="$ROOT/r-library"
export JUDGMENTLLM_R_STRICT=1
export PYTHONDONTWRITEBYTECODE=1
export NO_COLOR=1
export TERM=dumb
actual=$(/usr/bin/shasum -a 256 "$RUN/source.tar" | /usr/bin/cut -d ' ' -f 1)
[[ "$actual" == "$ARCHIVE_SHA256" ]] || { echo 'Transferred source archive hash mismatch' >&2; exit 2; }
tar -xf "$RUN/source.tar" -C "$SOURCE"
cp "$RUN/manifest.json" "$SOURCE/.judgmentllm-source.json"
test -f "$SOURCE/ci/local/gates.py"
if [[ "$MODE" == prepare ]]; then
  actual=$(/usr/bin/shasum -a 256 "$RUN/bootstrap/uv" | /usr/bin/cut -d ' ' -f 1)
  [[ "$actual" == "$UV_BINARY_SHA256" ]] || { echo 'Transferred uv hash mismatch' >&2; exit 2; }
  mkdir -p "$ROOT/tools/uv-0.13.6" "$ROOT/cache/uv" "$ROOT/python" "$ROOT/r-library"
  if [[ -f "$UV" ]]; then
    installed=$(/usr/bin/shasum -a 256 "$UV" | /usr/bin/cut -d ' ' -f 1)
    [[ "$installed" == "$UV_BINARY_SHA256" ]] || { echo 'Existing isolated uv bytes differ; refusing overwrite' >&2; exit 2; }
  else
    cp "$RUN/bootstrap/uv" "$UV"
    chmod 700 "$UV"
  fi
  [[ "$("$UV" --version)" == 'uv 0.13.6'* ]] || { echo 'Expected uv 0.13.6' >&2; exit 2; }
  /usr/bin/python3 - "$ROOT" "$UV_ARCHIVE_SHA256" "$UV_BINARY_SHA256" "$RUN_ID" <<'PY'
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
root = pathlib.Path(sys.argv[1])
library = root / "r-library"
target = library / "jsonlite"
provenance = library / "jsonlite-provenance.json"
def contents(path):
    return {p.relative_to(path).as_posix():
            {"symlink": p.readlink().as_posix()} if p.is_symlink() else
            {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(path.rglob("*")) if p.is_file() or p.is_symlink()}
if not target.exists():
    source = pathlib.Path(subprocess.check_output([
        "/usr/local/bin/Rscript", "--vanilla", "-e",
        'p <- file.path(Sys.getenv("HOME"), "pairwise-ci/library/R-4.6-x86_64"); '
        '.libPaths(c(p, .libPaths())); stopifnot(requireNamespace("jsonlite", quietly=TRUE)); '
        'stopifnot(as.character(packageVersion("jsonlite")) == "2.0.0"); cat(find.package("jsonlite"))'
    ], text=True).strip())
    before = contents(source)
    stage = library / ("jsonlite-stage-" + sys.argv[4])
    shutil.copytree(source, stage, symlinks=True)
    if contents(stage) != before or contents(source) != before:
        raise SystemExit("jsonlite changed during private copy")
    stage.rename(target)
    provenance.write_text(json.dumps({"source": str(source), "version": "2.0.0", "files": before}, indent=2) + "\n")
if not provenance.is_file() or contents(target) != json.loads(provenance.read_text())["files"]:
    raise SystemExit("Isolated jsonlite provenance does not match current package bytes")
(root / "tools/uv-0.13.6/provenance.json").write_text(json.dumps({
    "version": "0.13.6", "archive_sha256": sys.argv[2], "binary_sha256": sys.argv[3]
}, indent=2) + "\n")
PY
fi
test -x "$UV" || { echo 'Run prepare first: isolated uv is missing' >&2; exit 2; }
[[ "$("$UV" --version)" == 'uv 0.13.6'* ]] || { echo 'Expected uv 0.13.6' >&2; exit 2; }
/usr/bin/python3 - "$ROOT" <<'PY'
import hashlib
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
uv = root / "tools/uv-0.13.6/uv"
provenance = json.loads((uv.parent / "provenance.json").read_text())
expected_binary = "a94795fa09796e20b220c237410465fb40a334657816d58cf7c4f4df4bc8239e"
expected_archive = "63ba17fdda55ad1a230d4b37fc6017ffa10c23688c83818bd3bfdf914c652a02"
if (hashlib.sha256(uv.read_bytes()).hexdigest() != expected_binary
        or provenance.get("binary_sha256") != expected_binary
        or provenance.get("archive_sha256") != expected_archive
        or provenance.get("version") != "0.13.6"):
    raise SystemExit("Isolated uv does not match pinned official uv 0.13.6 bytes")
library = root / "r-library"
package = library / "jsonlite"
expected = json.loads((library / "jsonlite-provenance.json").read_text())["files"]
actual = {p.relative_to(package).as_posix():
          {"symlink": p.readlink().as_posix()} if p.is_symlink() else
          {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
          for p in sorted(package.rglob("*")) if p.is_file() or p.is_symlink()}
if actual != expected:
    raise SystemExit("Isolated jsonlite no longer matches prepared provenance")
PY
/usr/local/bin/Rscript --vanilla -e 'stopifnot(requireNamespace("jsonlite", quietly=TRUE)); stopifnot(as.character(packageVersion("jsonlite")) == "2.0.0")'
if [[ "$PYTHON_VERSION" == all ]]; then versions='3.11 3.12'; else versions=$PYTHON_VERSION; fi
status=0
for minor in $versions; do
  if [[ "$minor" == 3.11 ]]; then patch=3.11.18; else patch=3.12.16; fi
  if [[ "$MODE" == prepare ]]; then "$UV" python install --no-bin --no-registry "$patch"; fi
  export UV_PROJECT_ENVIRONMENT="$SOURCE/.venv"
  if [[ "$MODE" == check ]]; then export UV_OFFLINE=1; fi
  if "$UV" run --no-project --python "$patch" --offline python "$SOURCE/ci/local/gates.py" \
    --source "$SOURCE" --output "$RUN/results/python-$minor" --commit "$REF" \
    --archive-sha256 "$ARCHIVE_SHA256" --python-version "$minor" --mode "$MODE" --uv "$UV"; then
    printf 'macOS Python %s %s: PASS\n' "$minor" "$MODE"
  else
    status=1
    printf 'macOS Python %s %s: FAIL\n' "$minor" "$MODE" >&2
  fi
done
exit "$status"
GUEST
