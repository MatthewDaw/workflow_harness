// install.js — resolves the per-platform prebuilt binary for claude+ (U18).
//
// The platform-specific binary ships as an optionalDependency package named
// `@claude-plus/<os>-<arch>` containing a single `claude-plus` executable. npm
// installs only the package matching the host's os/cpu (via the `os`/`cpu`
// fields in those packages), so this module just resolves whichever one is
// present. Used by both the postinstall step (to verify) and the launcher shim.
"use strict";

const fs = require("node:fs");
const path = require("node:path");

// Map Node's process.platform/arch to the published package suffixes.
const PLATFORM_PACKAGES = {
  "darwin-arm64": "@claude-plus/darwin-arm64",
  "darwin-x64": "@claude-plus/darwin-x64",
  "linux-arm64": "@claude-plus/linux-arm64",
  "linux-x64": "@claude-plus/linux-x64",
  "win32-x64": "@claude-plus/win32-x64",
  "win32-arm64": "@claude-plus/win32-arm64",
};

function platformKey() {
  return `${process.platform}-${process.arch}`;
}

function exeName() {
  return process.platform === "win32" ? "claude-plus.exe" : "claude-plus";
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
      const bin = path.join(path.dirname(pkgJson), "bin", exe);
      if (fs.existsSync(bin)) {
        if (process.platform !== "win32") fs.chmodSync(bin, 0o755);
        return bin;
      }
    } catch {
      // fall through to the local fallback
    }
  }

  // 2) Local fallback: a binary bundled in this package (bin/<exe>).
  const local = path.join(__dirname, "bin", exe);
  if (fs.existsSync(local)) {
    if (process.platform !== "win32") fs.chmodSync(local, 0o755);
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
      "claude+: no prebuilt binary for " +
        platformKey() +
        " (optionalDependency not installed). Falling back to curl|sh or source build."
    );
    return;
  }
  console.log("claude+: using prebuilt binary at", bin);
}

if (require.main === module) {
  main();
}

module.exports = { binaryPath, platformKey, PLATFORM_PACKAGES };
