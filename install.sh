#!/bin/bash
# Embervoice source installer.
# Verifies the Mac, checks ffmpeg/ffprobe, creates .venv from the pinned
# lockfile with uv. Safe to re-run. Nothing is copied to ~/Applications:
# the app runs in place from this directory.
#
# Usage:  ./install.sh            interactive install
#         ./install.sh --check    preflight checks only, installs nothing
set -uo pipefail

SELF="$0"
case "$SELF" in
  */*) HERE="${SELF%/*}" ;;
  *) HERE="." ;;
esac
HERE="$(cd "$HERE" && pwd)"
cd "$HERE" || exit 1

UV_VERSION="0.11.26"
UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz"
UV_SHA256="8f7fbf1708399b921857bce71e1d60f0d3ccf52a30caebc1c1a2f175dce13ab6"
unset PYTHONPATH PYTHONHOME || true
export UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=2
PYTHON_VERSION="${AUDIOBOOK_STUDIO_PYTHON_VERSION:-3.13}"
MIN_RAM_BYTES="${AUDIOBOOK_STUDIO_MIN_RAM_BYTES:-12884901888}"   # 12 GiB
HOST_OS="${AUDIOBOOK_STUDIO_OS:-$(uname -s)}"
HOST_ARCH="${AUDIOBOOK_STUDIO_ARCH:-$(uname -m)}"
HOST_VERSION="${AUDIOBOOK_STUDIO_MACOS_VERSION:-$(/usr/bin/sw_vers -productVersion 2>/dev/null || printf 0)}"
HOST_RAM="${AUDIOBOOK_STUDIO_RAM_BYTES:-$(/usr/sbin/sysctl -n hw.memsize 2>/dev/null || echo 0)}"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 2; }

# ---------------------------------------------------------------- preflight
preflight_platform() {
  [ "$HOST_OS" = "Darwin" ] || die "Embervoice supports macOS only (this machine reports '$HOST_OS')."
  [ "$HOST_ARCH" = "arm64" ] || die "Embervoice needs an Apple Silicon (arm64) Mac; this machine reports '$HOST_ARCH'."
  major="${HOST_VERSION%%.*}"
  case "$major" in ''|*[!0-9]*) die "Could not read macOS version." ;; esac
  [ "$major" -ge 26 ] || die "The pinned MLX runtime requires macOS 26 or later (found $HOST_VERSION)."
}

preflight_memory() {
  local ram="${HOST_RAM:-0}"
  case "$ram" in
    ''|*[!0-9]*) die "Could not read total memory; refusing to guess." ;;
  esac
  [ "$ram" -ge "$MIN_RAM_BYTES" ] ||
    die "Embervoice needs at least 12 GiB of memory (this Mac reports $((ram / 1073741824)) GiB)."
}

find_tool() { # $1 name -> prints path
  local dir
  for dir in ${AUDIOBOOK_STUDIO_TOOL_PATHS:-/opt/homebrew/bin /usr/local/bin /usr/bin /bin}; do
    [ -x "$dir/$1" ] && { printf '%s\n' "$dir/$1"; return 0; }
  done
  command -v "$1" 2>/dev/null && return 0
  return 1
}

preflight_media_tools() {
  FFMPEG_PATH="$(find_tool ffmpeg)" || die "ffmpeg was not found. Install it with Homebrew:  brew install ffmpeg"
  FFPROBE_PATH="$(find_tool ffprobe)" || die "ffprobe was not found. Install it with Homebrew:  brew install ffmpeg"
  say "ffmpeg  : $FFMPEG_PATH"
  say "ffprobe : $FFPROBE_PATH"
}

find_uv() { find_tool uv; }
uv_version_matches() {
  local version
  version="$("$1" --version)" || return 1
  version="${version#uv }"; version="${version%% *}"
  [ "$version" = "$UV_VERSION" ]
}

# Reuse the exact uv version or bootstrap its verified official arm64 binary.
resolve_uv() {
  if UV_BIN="$(command -v uv || find_uv)"; then
    if uv_version_matches "$UV_BIN"; then
      say "uv       : $UV_BIN ($UV_VERSION)"
      return 0
    fi
  fi
  UV_BIN="$HERE/.local/tools/uv"
  if [ -x "$UV_BIN" ] && uv_version_matches "$UV_BIN"; then
    say "uv       : $UV_BIN ($UV_VERSION)"
    return 0
  fi
  if [ "$CHECK_ONLY" = 1 ]; then
    say "uv       : pinned version NOT FOUND; installation can download it after permission."
    say "Alternatively: brew install uv"
    return 1
  fi
  say "uv $UV_VERSION is needed. Download its official Apple Silicon archive?"
  say "URL: $UV_URL"
  say "SHA-256: $UV_SHA256"
  say "It stays inside this app folder; no shell profile or system package is changed."
  printf 'Download and verify uv now? [y/N] '
  read -r reply || reply=n
  case "$reply" in y|Y|yes|YES) ;; *) die "Cancelled before downloading uv." ;; esac
  local tmp got
  tmp="$(mktemp -d)" || die "Could not create a temporary download folder."
  /usr/bin/curl --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 180 --retry 2 -fSL "$UV_URL" -o "$tmp/uv.tar.gz" || {
    rm -rf "$tmp"; die "uv download failed. Check your connection and retry."
  }
  got="$(/usr/bin/shasum -a 256 "$tmp/uv.tar.gz")"; got="${got%% *}"
  if [ "$got" != "$UV_SHA256" ]; then
    rm -rf "$tmp"; die "uv checksum mismatch. Nothing was executed."
  fi
  /usr/bin/tar -xzf "$tmp/uv.tar.gz" -C "$tmp" uv-aarch64-apple-darwin/uv || {
    rm -rf "$tmp"; die "Could not extract the verified uv archive."
  }
  mkdir -p "$HERE/.local/tools" || die "Cannot write the app folder. Move it to a writable folder."
  cp "$tmp/uv-aarch64-apple-darwin/uv" "$UV_BIN" || die "Could not save uv."
  chmod 755 "$UV_BIN"
  rm -rf "$tmp"
  uv_version_matches "$UV_BIN" || die "The verified uv binary did not run."
  say "uv       : $UV_BIN ($UV_VERSION), checksum verified"
}

# ------------------------------------------------------------------ install
step "Checking this Mac"
preflight_platform
say "macOS $(sw_vers -productVersion 2>/dev/null || echo '?') on arm64"
preflight_memory
say "memory  : $((HOST_RAM / 1073741824)) GiB"

step "Checking audio tools"
preflight_media_tools

step "Checking the installer for uv"
if ! resolve_uv; then
  say "Re-run ./install.sh to opt in to the checksum-pinned uv download."
  exit 3
fi

if [ "$CHECK_ONLY" = 1 ]; then
  step "Check complete"
  say "This Mac can run Embervoice. Nothing was installed."
  exit 0
fi

[ -f "$HERE/pyproject.toml" ] || die "pyproject.toml is missing; this does not look like an Embervoice source folder."

step "Installing Python $PYTHON_VERSION and pinned dependencies"
say "This downloads Python $PYTHON_VERSION and the packages pinned in uv.lock."
printf 'Continue with the network download? [y/N] '
read -r reply || reply=n
case "$reply" in y|Y|yes|YES) ;; *) die "Cancelled before any dependency was downloaded." ;; esac

if ! "$UV_BIN" python find --no-python-downloads "$PYTHON_VERSION" >/dev/null 2>&1; then
  "$UV_BIN" python install "$PYTHON_VERSION" || die "Python download failed. Retry later, or install Python 3.13 with Homebrew (brew install python@3.13), then retry."
fi
export UV_PROJECT_ENVIRONMENT="$HERE/.venv"
"$UV_BIN" sync --locked --extra voice --no-dev ||
  die "Dependency install failed. If uv.lock is missing or out of date, the project owner must regenerate it."

step "Verifying"
[ -x "$HERE/.venv/bin/audiobook-studio" ] || die "Install finished but .venv/bin/audiobook-studio is missing."
"$HERE/.venv/bin/python" -c 'import sys; print("python  :", sys.version.split()[0])' || die "The installed interpreter does not run."

step "Done"
say "Embervoice is installed in this folder."
say "Start it by double-clicking:  Start Embervoice.command"
say "Model downloads and licence acceptance happen on first run, in the app's own screen."