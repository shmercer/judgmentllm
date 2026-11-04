#!/usr/bin/env bash
# Source this only inside the existing Intel macOS guest's isolated CI workspace.
# Official references:
# https://cryptography.io/en/50.0.2/installation/#building-cryptography-on-macos
# https://docs.rs/openssl/0.10.81/openssl/#manual
# https://openssl-library.org/source/ (3.5.9 LTS, published 2026-09-29)
# https://forge.rust-lang.org/infra/other-installation-methods.html#standalone-installers
# Archive pins below come from each official release's .sha256 endpoint.

judgmentllm_native_toolchain() {
  local mode=$1 root=$2 output=$3 helper=${BASH_SOURCE[0]}
  local assets work prefix component archive filename expected url
  [[ "$mode" == prepare || "$mode" == check ]] || return 2
  [[ "$root" == "$HOME/judgmentllm-ci" && "$(uname -s)" == Darwin && "$(uname -m)" == x86_64 ]] || {
    printf 'Native bootstrap requires the isolated JudgmentLLM Intel macOS root.\n' >&2
    return 2
  }
  mkdir "$output"
  assets="$root/cache/native-assets"
  export XDG_CACHE_HOME="$root/cache"
  export CARGO_HOME="$root/cache/cargo"
  export RUSTUP_HOME="$root/cache/rustup"
  export CARGO_TARGET_DIR="$root/cache/cargo-target/cryptography-50.0.2-rust-1.99.1-openssl-3.5.9"
  export TMPDIR="$root/cache/tmp/"
  export CARGO_BUILD_JOBS=2
  export MATURIN_NO_INSTALL_RUST=1
  export OPENSSL_DIR="$root/tools/openssl-3.5.9"
  export OPENSSL_INCLUDE_DIR="$OPENSSL_DIR/include"
  export OPENSSL_LIB_DIR="$OPENSSL_DIR/lib"
  export OPENSSL_STATIC=1
  export OPENSSL_NO_VENDOR=1
  export OPENSSL_NO_PKG_CONFIG=1
  export PATH="$root/tools/rust-1.99.1/bin:$PATH"
  if [[ "$mode" == check ]]; then export CARGO_NET_OFFLINE=true; else unset CARGO_NET_OFFLINE; fi
  mkdir -p "$assets" "$CARGO_HOME" "$RUSTUP_HOME" "$CARGO_TARGET_DIR" "$TMPDIR"

  # No backend may fall back to a user-level rustup or global package manager.
  for component in openssl rustc rust-std cargo; do
    case "$component" in
      openssl)
        filename=openssl-3.5.9.tar.gz
        expected=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
        url="https://github.com/openssl/openssl/releases/download/openssl-3.5.9/$filename" ;;
      rustc)
        filename=rustc-1.99.1-x86_64-apple-darwin.tar.xz
        expected=203feef2c2165d7dda1f30ccf75590f72a6abc52ce259ed1c418de84a6887698
        url="https://static.rust-lang.org/dist/$filename" ;;
      rust-std)
        filename=rust-std-1.99.1-x86_64-apple-darwin.tar.xz
        expected=484d5b88b424ddd29df196e1eb9420c8335bb5fac90a3c37140253345a8c65c4
        url="https://static.rust-lang.org/dist/$filename" ;;
      cargo)
        filename=cargo-1.99.1-x86_64-apple-darwin.tar.xz
        expected=6636dabcec164909d1c6eb9e2c596148787c84f7d3a02ff68a141569077f80b5
        url="https://static.rust-lang.org/dist/$filename" ;;
    esac
    archive="$assets/$filename"
    if [[ ! -f "$archive" ]]; then
      [[ "$mode" == prepare ]] || { printf 'Missing prepared native asset: %s\n' "$filename" >&2; return 2; }
      local temporary
      temporary=$(mktemp "$assets/.download-XXXXXX")
      printf 'Downloading pinned native asset: %s\n' "$filename"
      curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 20 \
        --max-time 600 --retry 2 --output "$temporary" "$url" >"$output/download-$component.log" 2>&1
      [[ "$(shasum -a 256 "$temporary" | cut -d ' ' -f 1)" == "$expected" ]] || {
        printf 'Native archive SHA-256 mismatch: %s; download preserved at %s\n' "$filename" "$temporary" >&2
        return 2
      }
      mv -n "$temporary" "$archive"
    fi
    [[ "$(shasum -a 256 "$archive" | cut -d ' ' -f 1)" == "$expected" ]] || {
      printf 'Cached native archive changed: %s; refusing replacement\n' "$filename" >&2
      return 2
    }
  done

  # Fresh work and final prefixes are never reused after an incomplete install.
  mkdir -p "$root/cache/native-build"
  work=$(mktemp -d "$root/cache/native-build/work-XXXXXX")
  prefix="$root/tools/rust-1.99.1"
  if [[ ! -d "$prefix" ]]; then
    [[ "$mode" == prepare ]] || { printf 'Run prepare first: Rust is missing.\n' >&2; return 2; }
    mkdir "$prefix"
    for component in rustc rust-std cargo; do
      filename="$component-1.99.1-x86_64-apple-darwin"
      tar -xf "$assets/$filename.tar.xz" -C "$work"
      printf 'Installing pinned Rust component: %s\n' "$component"
      /bin/bash "$work/$filename/install.sh" --prefix="$prefix" \
        --sysconfdir="$prefix/etc" --disable-ldconfig >"$output/install-$component.log" 2>&1
    done
  fi
  # Compare installed Rust payloads directly with the fixed-hash archives;
  # mutable local provenance alone is never sufficient to trust a compiler.
  /usr/bin/python3 - "$prefix" "$assets" <<'PY'
import hashlib
import pathlib
import sys
import tarfile
prefix, assets = map(pathlib.Path, sys.argv[1:])
for name in ("rustc", "rust-std", "cargo"):
    filename = name + "-1.99.1-x86_64-apple-darwin"
    with tarfile.open(assets / (filename + ".tar.xz")) as package:
        stream = package.extractfile(filename + "/components")
        if stream is None:
            raise SystemExit("Rust component manifest missing")
        components = stream.read().decode().splitlines()
        for member in package:
            parts = pathlib.PurePosixPath(member.name).parts
            if len(parts) < 3 or parts[1] not in components:
                continue
            relative = pathlib.PurePosixPath(*parts[2:])
            if relative.as_posix() == "manifest.in" or member.isdir():
                continue
            if ".." in relative.parts or relative.is_absolute():
                raise SystemExit("Unsafe Rust payload path")
            installed = prefix / relative
            if member.issym():
                if not installed.is_symlink() or str(installed.readlink()) != member.linkname:
                    raise SystemExit("Installed Rust symlink differs: " + str(relative))
            elif member.isfile():
                stream = package.extractfile(member)
                if stream is None or not installed.is_file():
                    raise SystemExit("Installed Rust payload missing: " + str(relative))
                if hashlib.sha256(stream.read()).digest() != hashlib.sha256(installed.read_bytes()).digest():
                    raise SystemExit("Installed Rust payload differs: " + str(relative))
PY
  [[ "$(command -v cargo)" == "$prefix/bin/cargo" && "$(command -v rustc)" == "$prefix/bin/rustc" ]] || return 2
  [[ "$(rustc --version)" == 'rustc 1.99.1 '* && "$(cargo --version)" == 'cargo 1.99.1 '* ]] || return 2
  rustc --version | tee "$output/rustc-version.txt"
  cargo --version | tee "$output/cargo-version.txt"

  if [[ ! -d "$OPENSSL_DIR" ]]; then
    [[ "$mode" == prepare ]] || { printf 'Run prepare first: OpenSSL is missing.\n' >&2; return 2; }
    mkdir "$OPENSSL_DIR"
    tar -xf "$assets/openssl-3.5.9.tar.gz" -C "$work"
    printf 'Building isolated static OpenSSL 3.5.9 (two compiler jobs).\n'
    (
      cd "$work/openssl-3.5.9"
      ./Configure darwin64-x86_64-cc no-shared no-tests no-module \
        --prefix="$OPENSSL_DIR" --openssldir="$OPENSSL_DIR/ssl" --libdir=lib
      make -j2
      make install_sw
    ) >"$output/build-openssl.log" 2>&1
    /usr/bin/python3 - "$OPENSSL_DIR" "$helper" <<'PY'
import hashlib
import json
import pathlib
import subprocess
import sys
prefix, helper = map(pathlib.Path, sys.argv[1:])
def files(root):
    return {p.relative_to(root).as_posix():
            {"link": str(p.readlink())} if p.is_symlink() else
            {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "mode": p.stat().st_mode & 0o777}
            for p in sorted(root.rglob("*"))
            if (p.is_file() or p.is_symlink()) and p.name != "provenance.json"}
record = {"version": "3.5.9", "source_sha256": "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a",
          "recipe_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
          "clang": subprocess.check_output(["/usr/bin/clang", "--version"], text=True).strip(),
          "configure": ["darwin64-x86_64-cc", "no-shared", "no-tests", "no-module", "--libdir=lib"],
          "files": files(prefix)}
(prefix / "provenance.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
PY
  fi
  /usr/bin/python3 - "$OPENSSL_DIR" "$helper" <<'PY'
import hashlib
import json
import pathlib
import sys
prefix, helper = map(pathlib.Path, sys.argv[1:])
record = json.loads((prefix / "provenance.json").read_text())
expected = "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
actual = {p.relative_to(prefix).as_posix():
          {"link": str(p.readlink())} if p.is_symlink() else
          {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "mode": p.stat().st_mode & 0o777}
          for p in sorted(prefix.rglob("*"))
          if (p.is_file() or p.is_symlink()) and p.name != "provenance.json"}
if (record.get("version") != "3.5.9" or record.get("source_sha256") != expected
        or record.get("recipe_sha256") != hashlib.sha256(helper.read_bytes()).hexdigest()
        or record.get("files") != actual):
    raise SystemExit("Isolated OpenSSL bytes or build provenance changed; refusing reuse")
for required in ("lib/libssl.a", "lib/libcrypto.a", "include/openssl/opensslv.h", "bin/openssl"):
    if not (prefix / required).is_file():
        raise SystemExit("OpenSSL development payload missing: " + required)
PY
  "$OPENSSL_DIR/bin/openssl" version | tee "$output/openssl-version.txt"
  cp "$OPENSSL_DIR/provenance.json" "$output/openssl-provenance.json"
  printf 'Pinned native toolchain verified; backend Rust installation is disabled.\n'
}

judgmentllm_native_toolchain "$@"
