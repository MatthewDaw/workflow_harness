#!/usr/bin/env node
// Launcher shim for `claude+` (U18, the esbuild optionalDependencies pattern).
//
// Resolves the prebuilt binary for the current platform — installed as an
// optionalDependency `@claude-plus/<os>-<arch>` — and execs it transparently,
// forwarding argv, stdio, and the exit code. No Go toolchain required.
'use strict';

const { spawnSync } = require('node:child_process');
const { binaryPath } = require('../install.js');

function main() {
  const bin = binaryPath();
  if (!bin) {
    console.error(
      'claude+: no prebuilt binary for ' +
        process.platform +
        '-' +
        process.arch +
        '. Install via curl|sh (scripts/install.sh) or build from source (wrapper/).',
    );
    process.exit(1);
  }
  const result = spawnSync(bin, process.argv.slice(2), { stdio: 'inherit' });
  if (result.error) {
    console.error('claude+:', result.error.message);
    process.exit(1);
  }
  process.exit(result.status === null ? 1 : result.status);
}

main();
