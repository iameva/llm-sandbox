#!/usr/bin/env bash
set -euo pipefail

os="$(uname -s)"
arch="$(uname -m)"

case "$arch" in
  x86_64|amd64) arch="x86_64" ;;
  aarch64|arm64) arch="aarch64" ;;
  *) echo "Unsupported arch: $arch" >&2; exit 1 ;;
esac

case "$os" in
  Linux) os="unknown-linux-musl" ;;
  Darwin) os="apple-darwin" ;;
  MINGW*|MSYS*|CYGWIN*) os="pc-windows-msvc" ;;
  *) echo "Unsupported OS: $os" >&2; exit 1 ;;
esac

if [ "$os" = "pc-windows-msvc" ]; then
  asset="codex-${arch}-pc-windows-msvc.exe.zip"
else
  asset="codex-${arch}-${os}.tar.gz"
fi

url="https://github.com/openai/codex/releases/latest/download/${asset}"

# Use workspace by default to avoid small tmpfs mounts such as /tmp in containers.
tmp_base="${CODEX_TMPDIR:-$PWD}"
tmpdir="$(mktemp -d -p "$tmp_base" codex.XXXXXX)"
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

archive="${tmpdir}/${asset}"
extract_dir="${tmpdir}/extract"
mkdir -p "$extract_dir"

if command -v curl >/dev/null 2>&1; then
  curl -fL "$url" -o "$archive"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$archive" "$url"
else
  echo "Need curl or wget" >&2
  exit 1
fi

if [[ "$asset" == *.zip ]]; then
  command -v unzip >/dev/null 2>&1 || { echo "Need unzip" >&2; exit 1; }
  unzip -q "$archive" -d "$extract_dir"
  expected_bin="${extract_dir}/codex-${arch}-pc-windows-msvc.exe"
  if [ -f "$expected_bin" ]; then
    bin="$expected_bin"
  else
    bin="$(find "$extract_dir" -type f -name 'codex.exe' -print -quit)"
  fi
else
  tar -xzf "$archive" -C "$extract_dir"
  expected_bin="${extract_dir}/codex-${arch}-${os}"
  if [ -f "$expected_bin" ]; then
    bin="$expected_bin"
  else
    bin="$(find "$extract_dir" -type f -name 'codex-*' -print -quit)"
  fi
fi

if [ -z "${bin:-}" ]; then
  echo "Failed to find codex binary in archive" >&2
  exit 1
fi

mv -f "$bin" ./codex
chmod +x ./codex
echo "Installed ./codex"
