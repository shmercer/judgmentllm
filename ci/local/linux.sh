#!/usr/bin/env bash
# Isolated Ubuntu checks using a derivative of the existing local R CI image.
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(cd -- "$script_dir/../.." && pwd -P)
ref=''
mode=check
python_version=all
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
                [--python-version 3.11|3.12|all] [--state-root PATH]
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
    --repo|--ref|--mode|--python-version|--state-root|--uv-archive|--uv-sha256|--jsonlite-archive|--jsonlite-sha256)
      (( $# >= 2 )) || fatal "$1 requires a value"
      case "$1" in
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
[[ $python_version =~ ^(3\.11|3\.12|all)$ ]] || fatal 'Invalid Python version'
[[ $mode == doctor || $ref =~ ^[0-9a-f]{40}$ ]] || fatal 'Use a full proposed commit SHA'
for program in docker python3 mountpoint realpath flock sha256sum tee; do
  command -v "$program" >/dev/null || fatal "Missing host program: $program"
done
mountpoint -q /mnt/VM_Fast || fatal '/mnt/VM_Fast must be mounted'
repo=$(cd -- "$repo" && pwd -P)
state_root=$(realpath -m -- "$state_root")
[[ $state_root == /mnt/VM_Fast/judgmentllm-ci/* ]] || fatal 'State must be beneath /mnt/VM_Fast/judgmentllm-ci/'
base_id=$(docker image inspect --format '{{.Id}}' "$base_image") || fatal 'Existing Linux R CI image is unavailable'
if [[ $mode == doctor ]]; then
  printf 'Base image: %s\n' "$base_id"
  docker run --rm --network none --entrypoint bash "$base_id" -lc \
    'cat /etc/os-release; python3 --version; Rscript --version; command -v cc; command -v git'
  exit 0
fi
mkdir -p -- "$state_root/runs" "$state_root/cache" "$state_root/locks"
exec 9>"$state_root/locks/grid.lock"
flock -n 9 || fatal 'Another JudgmentLLM Linux run is active'
run_id="linux-$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
run_root="$state_root/runs/$run_id"
mkdir -- "$run_root"
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
versions=(3.11 3.12)
[[ $python_version == all ]] || versions=("$python_version")
overall=0
for version in "${versions[@]}"; do
  patch=3.11.18; [[ $version == 3.11 ]] || patch=3.12.16
  lane="$run_root/python-$version"
  mkdir -- "$lane"
  network=none; [[ $mode == check ]] || network=bridge
  printf '[judgmentllm-linux] %s Python %s: %s\n' "$mode" "$patch" "$lane"
  status=0
  docker run --rm --network "$network" --cpus 6 --memory 16g \
    --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$state_root,dst=/state" \
    --env UV_CACHE_DIR=/state/cache --env XDG_CACHE_HOME=/state/cache \
    --env UV_PYTHON_INSTALL_DIR=/opt/judgmentllm/python \
    --env R_LIBS_USER=/opt/judgmentllm/r-library \
    --entrypoint bash "$image" -c '
      set -euo pipefail
      bundle=$1; lane=$2; mode=$3; minor=$4; patch=$5; commit=$6; archive_hash=$7
      actual=$(sha256sum "$bundle/source.tar")
      [[ ${actual%% *} == "$archive_hash" ]] || { echo "Source archive changed before extraction" >&2; exit 2; }
      mkdir "$lane/source"
      tar -xf "$bundle/source.tar" -C "$lane/source"
      cp "$bundle/manifest.json" "$lane/source/.judgmentllm-source.json"
      /opt/judgmentllm/tools/uv run --no-project --python "$patch" --offline python \
        "$lane/source/ci/local/gates.py" --source "$lane/source" --output "$lane/results" \
        --commit "$commit" --archive-sha256 "$archive_hash" --python-version "$minor" \
        --mode "$mode" --uv /opt/judgmentllm/tools/uv
    ' judgmentllm \
    "/state/runs/$run_id/bundle" "/state/runs/$run_id/python-$version" \
    "$mode" "$version" "$patch" "$ref" "$archive_hash" \
    2>&1 | tee "$lane/console.log" || status=$?
  printf '%s\n' "$status" >"$lane/exit-code"
  (( status == 0 )) || overall=1
done
printf '[judgmentllm-linux] Results: %s\n' "$run_root"
exit "$overall"
