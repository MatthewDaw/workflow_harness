#!/bin/sh
# claude+ installer (U18) — curl | sh on bare hosts, no Go toolchain needed.
#
#   curl -fsSL https://raw.githubusercontent.com/workflow-harness/claude-plus/main/scripts/install.sh | sh
#
# Detects OS/arch, downloads the matching release archive, verifies the
# checksum, and installs the `claude-plus` binary (symlinked as `claude+`) into
# a directory on PATH. Outbound-only: a single HTTPS GET, suitable over SSH.
set -eu

REPO="workflow-harness/claude-plus"
BIN_NAME="claude-plus"
# Override with INSTALL_DIR=... ; defaults to a user-writable location.
INSTALL_DIR="${INSTALL_DIR:-${HOME}/.local/bin}"
VERSION="${VERSION:-latest}"

err() { printf 'claude+ install: %s\n' "$1" >&2; exit 1; }

detect_os() {
  os="$(uname -s)"
  case "$os" in
    Linux) echo "linux" ;;
    Darwin) echo "darwin" ;;
    *) err "unsupported OS: $os (use npm i -g claude-plus or build from source)" ;;
  esac
}

detect_arch() {
  arch="$(uname -m)"
  case "$arch" in
    x86_64 | amd64) echo "amd64" ;;
    arm64 | aarch64) echo "arm64" ;;
    *) err "unsupported arch: $arch" ;;
  esac
}

resolve_version() {
  if [ "$VERSION" != "latest" ]; then
    echo "$VERSION"
    return
  fi
  # Resolve the latest tag via the GitHub API (no auth needed for public repos).
  api="https://api.github.com/repos/${REPO}/releases/latest"
  tag="$(curl -fsSL "$api" | grep -m1 '"tag_name"' | cut -d'"' -f4)"
  [ -n "$tag" ] || err "could not resolve latest version"
  echo "$tag"
}

main() {
  command -v curl >/dev/null 2>&1 || err "curl is required"

  OS="$(detect_os)"
  ARCH="$(detect_arch)"
  TAG="$(resolve_version)"
  VER="${TAG#v}"

  archive="claude-plus_${VER}_${OS}_${ARCH}.tar.gz"
  base="https://github.com/${REPO}/releases/download/${TAG}"
  url="${base}/${archive}"
  sums="${base}/checksums.txt"

  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT

  printf 'claude+ install: downloading %s\n' "$archive"
  curl -fsSL "$url" -o "${tmp}/${archive}" || err "download failed: $url"

  # Verify checksum when available.
  if curl -fsSL "$sums" -o "${tmp}/checksums.txt" 2>/dev/null; then
    ( cd "$tmp" && grep " ${archive}\$" checksums.txt | sha256sum -c - ) \
      || err "checksum verification failed"
  fi

  tar -xzf "${tmp}/${archive}" -C "$tmp"
  mkdir -p "$INSTALL_DIR"
  install -m 0755 "${tmp}/${BIN_NAME}" "${INSTALL_DIR}/${BIN_NAME}"
  # Provide the `claude+` alias via a symlink (PATH-friendly name).
  ln -sf "${INSTALL_DIR}/${BIN_NAME}" "${INSTALL_DIR}/claude+" 2>/dev/null || true

  printf 'claude+ install: installed to %s\n' "${INSTALL_DIR}/${BIN_NAME}"
  case ":${PATH}:" in
    *":${INSTALL_DIR}:"*) : ;;
    *) printf 'claude+ install: add %s to your PATH\n' "$INSTALL_DIR" ;;
  esac
  "${INSTALL_DIR}/${BIN_NAME}" --version || true
}

main "$@"
