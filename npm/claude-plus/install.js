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
};

function platformKey() {
  return `${process.platform}-${process.arch}`;
}

// binaryPath returns the absolute path to the prebuilt binary, or null if the
// matching optionalDependency is not installed (unsupported platform).
function binaryPath() {
  const pkg = PLATFORM_PACKAGES[platformKey()];
  if (!pkg) return null;
  const exe = process.platform === "win32" ? "claude-plus.exe" : "claude-plus";
  try {
    // Resolve the package's directory, then the binary inside it.
    const pkgJson = require.resolve(`${pkg}/package.json`);
    const bin = path.join(path.dirname(pkgJson), "bin", exe);
    if (fs.existsSync(bin)) {
      fs.chmodSync(bin, 0o755);
      return bin;
    }
  } catch {
    return null;
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
