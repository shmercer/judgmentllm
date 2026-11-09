#!/usr/bin/env bash
# Isolated Ubuntu checks using a derivative of the existing local R CI image.
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(cd -- "$script_dir/../.." && pwd -P)
ref=''
mode=check
python_version=all
workers=0
max_lanes=3
resource_slots=3
cpu_limit=6
suite_stage=full
benchmark_workers=''
distribution=loadfile
state_root=/mnt/VM_Fast/judgmentllm-ci/linux
base_image=pairwisellm-linux-ci:release
uv_archive=''
uv_sha256=''
jsonlite_archive=''
jsonlite_sha256=''
fatal() { printf '[judgmentllm-linux] ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'HELP'
Usage: linux.sh --ref COMMIT [--repo PATH] [--mode doctor|prepare|check]
                [--python-version 3.11|3.12|3.15|all] [--state-root PATH]
                [--uv-archive PATH --uv-sha256 SHA256]
                [--jsonlite-archive PATH --jsonlite-sha256 SHA256]

prepare builds a separate JudgmentLLM image and warms its own dependency cache.
check runs the committed harness in fresh containers with --network none.
The original R image/tag, source checkout and other projects are not modified.
All cases retain the project's existing resource thresholds and timeouts.
HELP
}
while (( $# )); do
  case "$1" in
    --repo|--ref|--mode|--python-version|--state-root|--uv-archive|--uv-sha256|--jsonlite-archive|--jsonlite-sha256|--workers|--max-lanes|--resource-slots|--cpus|--suite-stage|--benchmark-workers|--distribution)
      (( $# >= 2 )) || fatal "$1 requires a value"
      case "$1" in
        --workers) workers=$2 ;; --max-lanes) max_lanes=$2 ;;
        --resource-slots) resource_slots=$2 ;; --cpus) cpu_limit=$2 ;;
        --suite-stage) suite_stage=$2 ;;
        --benchmark-workers) benchmark_workers=$2 ;; --distribution) distribution=$2 ;;
        --repo) repo=$2 ;; --ref) ref=$2 ;; --mode) mode=$2 ;;
        --python-version) python_version=$2 ;; --state-root) state_root=$2 ;;
        --uv-archive) uv_archive=$2 ;; --uv-sha256) uv_sha256=$2 ;;
        --jsonlite-archive) jsonlite_archive=$2 ;; --jsonlite-sha256) jsonlite_sha256=$2 ;;
      esac
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fatal "Unknown option: $1" ;;
  esac
done
[[ $mode =~ ^(doctor|prepare|check)$ ]] || fatal 'Invalid mode'
[[ $workers =~ ^[0-9]+$ ]] && (( workers <= 32 )) || fatal 'Invalid worker count'
[[ $max_lanes =~ ^[1-3]$ ]] || fatal 'Invalid Linux lane limit'
[[ $resource_slots =~ ^[1-3]$ ]] || fatal 'Invalid Linux resource slots'
[[ $cpu_limit =~ ^[0-9]+$ ]] && (( cpu_limit >= 1 && cpu_limit <= 16 )) || fatal 'Invalid disposable container CPU quota'
[[ $suite_stage =~ ^(full|ordinary|resource)$ ]] || fatal 'Invalid suite stage'
[[ $distribution =~ ^(loadfile|worksteal)$ ]] || fatal 'Invalid pytest distribution'
[[ -z "$benchmark_workers" || $benchmark_workers =~ ^([0-9]|1[0-6])(,([0-9]|1[0-6]))*$ ]] || fatal 'Invalid worker sweep'
(( workers <= cpu_limit )) || fatal 'Workers exceed the disposable CPU quota'
if [[ -n "$benchmark_workers" ]]; then
  IFS=',' read -r -a sweep_counts <<< "$benchmark_workers"
  for count in "${sweep_counts[@]}"; do (( count <= cpu_limit )) || fatal 'Invalid worker sweep'; done
fi
[[ $python_version =~ ^(3\.11|3\.12|3\.15|all)$ ]] || fatal 'Invalid Python version'
[[ $mode == doctor || $ref =~ ^[0-9a-f]{40}$ ]] || fatal 'Use a full proposed commit SHA'
for program in docker python3 mountpoint realpath flock sha256sum tee; do
  command -v "$program" >/dev/null || fatal "Missing host program: $program"
done
mountpoint -q /mnt/VM_Fast || fatal '/mnt/VM_Fast must be mounted'
repo=$(cd -- "$repo" && pwd -P)
state_root=$(realpath -m -- "$state_root")
[[ $state_root == /mnt/VM_Fast/judgmentllm-ci/* ]] || fatal 'State must be beneath /mnt/VM_Fast/judgmentllm-ci/'
base_id=$(docker image inspect --format '{{.Id}}' "$base_image") || fatal 'Existing Linux R CI image is unavailable'
mkdir -p -- "$state_root/runs" "$state_root/cache" "$state_root/locks"
exec 9>"$state_root/locks/grid.lock"
flock -n 9 || fatal 'Another JudgmentLLM Linux run is active'
run_id="linux-$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
run_root="$state_root/runs/$run_id"
mkdir -- "$run_root"
container_cidfiles=()
container_clients=()
run_label=org.judgmentllm.local-ci.run
spawning_client=0
pending_signal=0
handle_signal() {
  if ((spawning_client)); then pending_signal=$1; else exit "$1"; fi
}

stop_owned_container() {
  local candidate cidfile actual label running inspection candidates=''
  for cidfile in "${container_cidfiles[@]}"; do
    if [[ -s $cidfile ]]; then
      read -r candidate <"$cidfile" || true
      if [[ $candidate =~ ^[0-9a-f]{64}$ ]]; then
        candidates+=$'\n'$candidate
      else
        printf 'Incomplete container ID file; checking the exact owned run label.\n' >&2
      fi
    fi
  done
  # The exact run label also covers cancellation during container creation,
  # before the Docker client has finished writing its ID file.
  candidates+=$'\n'$(docker ps --all --quiet --no-trunc --filter "label=$run_label=$run_id") || return 1
  for candidate in $candidates; do
    [[ $candidate =~ ^[0-9a-f]{64}$ ]] || return 1
    inspection=$(docker inspect --format '{{.Id}} {{index .Config.Labels "org.judgmentllm.local-ci.run"}} {{.State.Running}}' "$candidate" 2>/dev/null) || continue
    read -r actual label running <<<"$inspection"
    [[ $actual == "$candidate" && $label == "$run_id" ]] || {
      printf 'Refusing cleanup of an unowned container: %s\n' "$candidate" >&2
      return 1
    }
    if [[ $running == true ]]; then
      printf '[judgmentllm-linux] Gracefully stopping owned container %s\n' "$candidate"
      # Negative timeout disables Docker's forced-kill fallback entirely.
      docker stop --timeout=-1 "$candidate" >>"$run_root/lifecycle.log" 2>&1 || return 1
    else
      # A cancelled create may never reach start, so --rm has not activated.
      # Remove only this label-verified, non-running disposable object, without
      # --force; an already auto-removed container needs no further cleanup.
      if ! docker rm "$candidate" >>"$run_root/lifecycle.log" 2>&1; then
        if docker inspect "$candidate" >/dev/null 2>&1; then return 1; fi
      fi
    fi
  done
}

cleanup() {
  local status=$? client
  trap - EXIT INT TERM
  set +e
  # Stop the daemon-side container, even if Ctrl-C already ended its client.
  stop_owned_container || { ((status != 0)) || status=1; }
  # A completed peer may already be reaped. Do not signal a reused host PID;
  # only the live Docker command carrying this exact ownership label is ours.
  python3 - "$run_id" "${container_clients[@]}" <<'PY'
import os
from pathlib import Path
import signal
import sys
marker = ("org.judgmentllm.local-ci.run=" + sys.argv[1]).encode()
for value in sys.argv[2:]:
    if not value:
        continue
    pid = int(value)
    process = Path("/proc") / value
    try:
        arguments = (process / "cmdline").read_bytes().split(b"\0")
        if process.stat().st_uid == os.getuid() and marker in arguments:
            os.kill(pid, signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError):
        pass
PY
  for client in "${container_clients[@]}"; do
    [[ -n $client ]] || continue
    wait "$client" 2>/dev/null || true
  done
  stop_owned_container || { ((status != 0)) || status=1; }
  exit "$status"
}
trap cleanup EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

start_container() {
  local console=$1 cidfile=$2 client published_cid job running
  container_cidfiles+=("$cidfile")
  shift 2
  # Wait on an asynchronous client so Bash handles INT/TERM immediately.
  # Tini forwards normal signals to the whole child group and reaps children.
  spawning_client=1
  docker run --rm --init --stop-signal SIGTERM --cidfile "$cidfile" \
    --label "$run_label=$run_id" --env TINI_KILL_PROCESS_GROUP=1 "$@" \
    > >(tee "$console") 2>&1 &
  container_clients+=("$!")
  client=${container_clients[${#container_clients[@]}-1]}
  # Do not cancel an ambiguous daemon-side create: wait for its CID acknowledgement
  # before honoring a pending signal. This also covers a partially written file.
  while true; do
    published_cid=''
    if [[ -s $cidfile ]]; then read -r published_cid <"$cidfile" || true; fi
    [[ $published_cid =~ ^[0-9a-f]{64}$ ]] && break
    running=0
    for job in $(jobs -pr); do
      if [[ $job == "$client" ]]; then running=1; break; fi
    done
    ((running)) || break
    sleep 0.02
  done
  spawning_client=0
  if ((pending_signal)); then exit "$pending_signal"; fi
}

if [[ $mode == doctor ]]; then
  printf 'Base image: %s\n' "$base_id"
  mkdir "$run_root/doctor"
  start_container "$run_root/doctor/console.log" "$run_root/doctor/container.cid" \
    --network none --entrypoint bash "$base_id" -lc \
    'cat /etc/os-release; python3 --version; Rscript --version; command -v cc; command -v git'
  status=0
  wait "${container_clients[0]}" || status=$?
  container_clients[0]=''
  exit "$status"
fi
python3 "$script_dir/source.py" --repo "$repo" --ref "$ref" --output "$run_root/bundle"
archive_hash=$(sha256sum -- "$run_root/bundle/source.tar")
archive_hash=${archive_hash%% *}
# Image identity includes the exact committed Dockerfile and existing base bytes.
python3 - "$run_root/bundle/source.tar" "$run_root/Dockerfile" <<'PY'
import pathlib
import sys
import tarfile
with tarfile.open(sys.argv[1]) as source:
    member = source.getmember('ci/local/Dockerfile')
    if not member.isfile():
        raise SystemExit('Committed Dockerfile is not a regular file')
    stream = source.extractfile(member)
    assert stream is not None
    pathlib.Path(sys.argv[2]).write_bytes(stream.read())
PY
dockerfile_hash=$(sha256sum -- "$run_root/Dockerfile")
image_key=$(printf '%s\n%s\n' "$base_id" "${dockerfile_hash%% *}" | sha256sum)
image="judgmentllm-local-ci:${image_key:0:24}"
printf '{"base_image":"%s","image":"%s","commit":"%s","archive_sha256":"%s"}\n' \
  "$base_id" "$image" "$ref" "$archive_hash" >"$run_root/adapter.json"
if [[ $mode == prepare ]]; then
  [[ -f $uv_archive && $uv_sha256 == f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645 ]] || fatal 'Supply the pinned Linux uv archive/checksum'
  [[ -f $jsonlite_archive && $jsonlite_sha256 == e947f4ce2c3a8194ae42c4015cdc557cec1c1cea06f85cd20da037aa93dfa5e4 ]] || fatal 'Supply the pinned jsonlite archive/checksum'
  actual=$(sha256sum -- "$uv_archive"); [[ ${actual%% *} == "$uv_sha256" ]] || fatal 'uv archive changed'
  actual=$(sha256sum -- "$jsonlite_archive"); [[ ${actual%% *} == "$jsonlite_sha256" ]] || fatal 'jsonlite archive changed'
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    mkdir -- "$run_root/build"
    cp -- "$run_root/Dockerfile" "$run_root/build/Dockerfile"
    cp -- "$uv_archive" "$run_root/build/uv.tar.gz"
    cp -- "$jsonlite_archive" "$run_root/build/jsonlite.tar.gz"
    # A namespaced immutable alias avoids racing the R project's mutable tag.
    base_alias="judgmentllm-local-ci-base:${base_id#sha256:}"
    docker image tag "$base_id" "$base_alias"
    docker build --build-arg "BASE_IMAGE=$base_alias" --tag "$image" "$run_root/build" \
      >"$run_root/image-build.log" 2>&1 || fatal "Image preparation failed: $run_root/image-build.log"
  fi
fi
docker image inspect "$image" >"$run_root/image.json" || fatal 'Image absent; run prepare first'
versions=(3.11 3.12 3.15)
[[ $python_version == all ]] || versions=("$python_version")
lanes=()
overall=0
for version in "${versions[@]}"; do
  case "$version" in
    3.11) patch=3.11.18 ;;
    3.12) patch=3.12.16 ;;
    3.15) patch=3.15.2 ;;
    *) fatal "Unpinned Python version: $version" ;;
  esac
  lane="$run_root/python-$version"
  mkdir -- "$lane"
  mkdir -- "$lane/scratch"
  lanes+=("$lane")
  network=none; [[ $mode == check ]] || network=bridge
  printf '[judgmentllm-linux] %s Python %s: %s\n' "$mode" "$patch" "$lane"
  start_container "$lane/console.log" "$lane/container.cid" \
    --network "$network" --cpus "$cpu_limit" --memory 16g \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$state_root,dst=/state" \
    --env UV_CACHE_DIR=/state/cache --env XDG_CACHE_HOME=/state/cache \
    --env "TMPDIR=/state/runs/$run_id/python-$version/scratch" \
    --env UV_PYTHON_INSTALL_DIR=/opt/judgmentllm/python \
    --env R_LIBS_USER=/opt/judgmentllm/r-library \
    --env "JCI_PYTEST_WORKERS=$workers" --env "JCI_RESOURCE_SLOTS=$resource_slots" \
    --env "JCI_BENCHMARK_WORKERS=$benchmark_workers" --env "JCI_DISTRIBUTION=$distribution" \
    --env "JCI_SUITE_STAGE=$suite_stage" --env "JCI_RESOURCE_ROOT=/state/runs/$run_id/resource-budget" \
    --entrypoint bash "$image" -c '
      set -euo pipefail
      bundle=$1; lane=$2; mode=$3; minor=$4; patch=$5; commit=$6; archive_hash=$7
      actual=$(sha256sum "$bundle/source.tar")
      [[ ${actual%% *} == "$archive_hash" ]] || { echo "Source archive changed before extraction" >&2; exit 2; }
      mkdir "$lane/source"
      tar -xf "$bundle/source.tar" -C "$lane/source"
      cp "$bundle/manifest.json" "$lane/source/.judgmentllm-source.json"
      exec /opt/judgmentllm/tools/uv run --no-project --python "$patch" --offline python \
        "$lane/source/ci/local/gates.py" --source "$lane/source" --output "$lane/results" \
        --commit "$commit" --archive-sha256 "$archive_hash" --python-version "$minor" \
        --mode "$mode" --uv /opt/judgmentllm/tools/uv \
        --workers "$JCI_PYTEST_WORKERS" --resource-slots "$JCI_RESOURCE_SLOTS" \
        --resource-root "$JCI_RESOURCE_ROOT" --suite-stage "$JCI_SUITE_STAGE" \
        "--benchmark-workers=$JCI_BENCHMARK_WORKERS" --distribution "$JCI_DISTRIBUTION"
    ' judgmentllm \
    "/state/runs/$run_id/bundle" "/state/runs/$run_id/python-$version" \
    "$mode" "$version" "$patch" "$ref" "$archive_hash"
  # Bound independent containers without cancelling failed peers. Versions in
  # one VM have their own shared allocation limit in the native adapters.
  index=$((${#container_clients[@]} - max_lanes))
  if (( index >= 0 )); then
    status=0
    wait "${container_clients[index]}" || status=$?
    container_clients[index]=''
    printf '%s\n' "$status" >"${lanes[index]}/exit-code"
    (( status == 0 )) || overall=1
  fi
done
# Start all three fresh interpreters before waiting. A failed lane never cancels
# its siblings; only explicit interruption/adapter exit invokes whole-run cleanup.
for index in "${!versions[@]}"; do
  [[ -n ${container_clients[index]} ]] || continue
  status=0
  wait "${container_clients[index]}" || status=$?
  container_clients[index]=''
  printf '%s\n' "$status" >"${lanes[index]}/exit-code"
  (( status == 0 )) || overall=1
done
printf '[judgmentllm-linux] Results: %s\n' "$run_root"
exit "$overall"
