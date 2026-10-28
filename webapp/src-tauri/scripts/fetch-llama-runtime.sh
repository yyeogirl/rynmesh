#!/bin/sh
# Stage the pinned llama.cpp release under src-tauri/resources/llama so Tauri
# bundles it: the packaged desktop app then has a local inference runtime with
# no download and no Docker (issue #34).
#
# The release, asset name, and SHA-256 come from the Python source of truth
# (rynmesh/llm_package/runtime_native_install.py) so the pin lives in exactly
# one place. Pass a target triple as $1 to stage for a host other than this one.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
SRC_TAURI="$ROOT/webapp/src-tauri"
TARGET="$SRC_TAURI/resources/llama"
SERVER="llama-server"

TRIPLE="${1:-}"
if [ -z "$TRIPLE" ]; then
  RUSTC="$HOME/.cargo/bin/rustc"
  [ -x "$RUSTC" ] || RUSTC="$(command -v rustc || true)"
  [ -n "$RUSTC" ] && [ -x "$RUSTC" ] || { echo "rustc not found; pass a target triple" >&2; exit 1; }
  TRIPLE="$("$RUSTC" -Vv | sed -n 's/^host: //p')"
fi
[ -n "$TRIPLE" ] || { echo "could not resolve rust host triple" >&2; exit 1; }

case "$TRIPLE" in
  aarch64-apple-darwin) SYSTEM=Darwin; MACHINE=arm64 ;;
  x86_64-apple-darwin) SYSTEM=Darwin; MACHINE=x86_64 ;;
  x86_64-unknown-linux-*) SYSTEM=Linux; MACHINE=x86_64 ;;
  aarch64-unknown-linux-*) SYSTEM=Linux; MACHINE=arm64 ;;
  *) echo "no pinned llama.cpp runtime for target $TRIPLE" >&2; exit 1 ;;
esac

PIN="$(PYTHONPATH="$ROOT" python3 -c '
import sys
from rynmesh.llm_package.runtime_native_install import (
    RUNTIME_BASE_URL, RUNTIME_RELEASE, asset_for)

pinned = asset_for(sys.argv[1], sys.argv[2])
if pinned is None:
    raise SystemExit("no pinned llama.cpp asset for %s/%s" % (sys.argv[1], sys.argv[2]))
name, sha256, _size = pinned
print(RUNTIME_RELEASE)
print(name)
print(sha256)
print(RUNTIME_BASE_URL + name)
' "$SYSTEM" "$MACHINE")"

RELEASE="$(printf '%s\n' "$PIN" | sed -n 1p)"
ASSET="$(printf '%s\n' "$PIN" | sed -n 2p)"
SHA256="$(printf '%s\n' "$PIN" | sed -n 3p)"
URL="$(printf '%s\n' "$PIN" | sed -n 4p)"
[ -n "$RELEASE" ] && [ -n "$ASSET" ] && [ -n "$SHA256" ] && [ -n "$URL" ] ||
  { echo "could not read the pinned runtime asset" >&2; exit 1; }

# Already staged at this exact pin: nothing to download.
if [ -x "$TARGET/$SERVER" ] && [ -f "$TARGET/runtime.json" ] &&
   python3 -c '
import json, sys

try:
    marker = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
ok = marker.get("release") == sys.argv[2] and marker.get("sha256") == sys.argv[3]
raise SystemExit(0 if ok else 1)
' "$TARGET/runtime.json" "$RELEASE" "$SHA256"; then
  echo "LLAMA_RUNTIME_READY $TARGET $RELEASE"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT HUP INT TERM

curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 -o "$WORK/$ASSET" "$URL"

if command -v shasum >/dev/null 2>&1; then
  ACTUAL="$(shasum -a 256 "$WORK/$ASSET" | cut -d ' ' -f 1)"
elif command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$WORK/$ASSET" | cut -d ' ' -f 1)"
else
  echo "neither shasum nor sha256sum is available" >&2
  exit 1
fi
[ "$ACTUAL" = "$SHA256" ] ||
  { echo "checksum mismatch for $ASSET: expected $SHA256, got $ACTUAL" >&2; exit 1; }

mkdir -p "$WORK/unpacked"
case "$ASSET" in
  *.zip) unzip -q "$WORK/$ASSET" -d "$WORK/unpacked" ;;
  *) tar -xzf "$WORK/$ASSET" -C "$WORK/unpacked" ;;
esac

# Releases have moved the payload around (top-level dir today, build/bin
# before), so locate the server and flatten whatever directory holds it.
FOUND="$(find "$WORK/unpacked" -type f -name "$SERVER" -print | head -n 1)"
[ -n "$FOUND" ] || { echo "$ASSET did not contain $SERVER" >&2; exit 1; }
STAGE="$(dirname "$FOUND")"

# Keep the server, its shared libraries (ggml loads CPU variants by name at
# run time), and the licence; drop the other tools to keep the bundle small.
find "$STAGE" -mindepth 1 -maxdepth 1 \
  ! -name "$SERVER" ! -name 'LICENSE*' \
  ! -name '*.dylib' ! -name '*.so' ! -name '*.so.*' \
  -exec rm -rf {} +

rm -rf "$TARGET"
mkdir -p "$TARGET"
cp -R "$STAGE/." "$TARGET/"

find "$TARGET" -type f \
  \( -name "$SERVER" -o -name '*.dylib' -o -name '*.so' -o -name '*.so.*' \) \
  -exec chmod 0755 {} +

cat >"$TARGET/runtime.json" <<MARKER
{
  "release": "$RELEASE",
  "asset": "$ASSET",
  "sha256": "$SHA256"
}
MARKER

echo "LLAMA_RUNTIME_READY $TARGET $RELEASE"
