// Bundles each backend Lambda handler from packages/backend/dist into a
// self-contained CommonJS asset under infra/cdk.bundles/<key>/index.js, which
// the ApiStack references via lambda.Code.fromAsset.
//
// We bundle from dist (not src) because the handlers use .js import extensions
// (our ESM tsc convention) which esbuild only resolves once tsc has emitted the
// matching .js files. @aws-sdk/* is externalized — the Node 20 Lambda runtime
// ships the AWS SDK v3; everything else (zod, jose, aws-jwt-verify,
// @harness/shared) is bundled in.
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';
import { rmSync } from 'node:fs';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const backendDist = path.resolve(repoRoot, 'packages', 'backend', 'dist');
const outRoot = path.resolve(here, '..', 'cdk.bundles');

// We bundle from dist, so the backend (and its @harness/shared dep) must be
// compiled first. Workspace (-w) commands only resolve from the repo root, so
// build there. This keeps the bundler self-sufficient: a fresh checkout can run
// it (or `npm test` in infra) without a separate manual build step.
execSync('npm run build -w @harness/shared -w @harness/backend', {
  cwd: repoRoot,
  stdio: 'inherit',
});

const handlers = [
  'rest/projects',
  'rest/sessions',
  'rest/agents',
  'rest/skills',
  'rest/mcpServers',
  'rest/objectives',
  'rest/weekly',
  'rest/memories',
  'rest/device',
  'rest/dod',
  'rest/orgs',
  'ws/connect',
  'ws/disconnect',
  'ws/default',
  'ws/event',
  'ws/subscribe',
  'ws/control',
  'ws/authorizer',
  'ws/streamConsumer',
];

rmSync(outRoot, { recursive: true, force: true });

for (const h of handlers) {
  const key = h.replace('/', '_');
  await build({
    entryPoints: [path.join(backendDist, `${h}.js`)],
    bundle: true,
    platform: 'node',
    target: 'node20',
    format: 'cjs',
    outfile: path.join(outRoot, key, 'index.js'),
    external: ['@aws-sdk/*'],
    logLevel: 'warning',
    legalComments: 'none',
  });
}

console.log(`bundled ${handlers.length} handlers -> ${outRoot}`);
