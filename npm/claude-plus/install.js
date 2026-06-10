// install.js — resolves the per-platform prebuilt binary for claude+.
//
// The platform-specific binary ships as an optionalDependency package named
// `@claude-plus/<os>-<arch>` containing a single `claude-plus` executable. npm
// installs only the package matching the host's os/cpu (via the `os`/`cpu`
// fields in those packages), so this module just resolves whichever one is
// present. Used by both the postinstall step (to verify) and the launcher shim.
'use strict';

const fs = require('node:fs');
const path = require('node:path');

// Map Node's process.platform/arch to the published package suffixes.
const PLATFORM_PACKAGES = {
  'darwin-arm64': '@claude-plus/darwin-arm64',
  'darwin-x64': '@claude-plus/darwin-x64',
  'linux-arm64': '@claude-plus/linux-arm64',
  'linux-x64': '@claude-plus/linux-x64',
  'win32-x64': '@claude-plus/win32-x64',
  'win32-arm64': '@claude-plus/win32-arm64',
};

function platformKey() {
  return `${process.platform}-${process.arch}`;
}

function exeName() {
  return process.platform === 'win32' ? 'claude-plus.exe' : 'claude-plus';
}

// ensureExecutable repairs a missing exec bit on the resolved binary (npm can
// drop it when unpacking). Guarded by a mode check so the common launch path is
// a stat, not a chmod, and wrapped in try/catch so a read-only install location
// never breaks launching an already-executable binary.
function ensureExecutable(bin) {
  if (process.platform === 'win32') return;
  try {
    const mode = fs.statSync(bin).mode;
    if ((mode & 0o111) !== 0o111) fs.chmodSync(bin, 0o755);
  } catch {
    // best-effort: if the binary truly isn't executable, spawn will say so
  }
}

// binaryPath returns the absolute path to the binary for the current platform.
// It prefers the published per-platform optionalDependency package, and falls
// back to a binary bundled directly in this package's bin/ directory (used by
// local installs / source builds where the per-platform packages aren't
// published). Returns null if neither is present.
function binaryPath() {
  const exe = exeName();

  // 1) Published per-platform package (the esbuild optionalDependencies pattern).
  const pkg = PLATFORM_PACKAGES[platformKey()];
  if (pkg) {
    try {
      const pkgJson = require.resolve(`${pkg}/package.json`);
      const bin = path.join(path.dirname(pkgJson), 'bin', exe);
      if (fs.existsSync(bin)) {
        ensureExecutable(bin);
        return bin;
      }
    } catch {
      // fall through to the local fallback
    }
  }

  // 2) Local fallback: a binary bundled in this package (bin/<exe>).
  const local = path.join(__dirname, 'bin', exe);
  if (fs.existsSync(local)) {
    ensureExecutable(local);
    return local;
  }

  return null;
}

function main() {
  const bin = binaryPath();
  if (!bin) {
    // Non-fatal: the platform may be unsupported by prebuilts; the shim prints a
    // clear message at run time. Don't fail the whole `npm i -g`.
    console.warn(
      'claude+: no prebuilt binary for ' +
        platformKey() +
        ' (optionalDependency not installed). Falling back to curl|sh or source build.',
    );
    return;
  }
  console.log('claude+: using prebuilt binary at', bin);
}

if (require.main === module) {
  main();
}

module.exports = { binaryPath, platformKey, PLATFORM_PACKAGES };
