// Register the `humanlayer-approvals` MCP server (HITL approvals) into ONE org's
// MCP-servers catalog — by default NOT org-wide.
//
// This is the MCP-catalog analogue of seed-humanlayer-ace.mjs: a DEDICATED,
// SCOPED registration keyed on the REQUIRED `SEED_ORG`. It writes exactly one
// record (org-scoped) and is wired into NOTHING that propagates org-wide — no
// starter.ts, no seed-all-orgs.mjs, no bundles.json. A human runs the real write
// later; this never auto-writes without an explicit (non-dry) invocation.
//
// The server is a local (stdio) transport: the daemon spawns `humanlayer mcp
// claude_approvals`, which exposes the tool
// `mcp__humanlayer-approvals__request_permission` in a session. A project opts
// the server in (enabledMcpServers), or an agent carries it (mcpServers[]).
//
// Reuses the compiled `mcpServerSchema` + `orgScope` from @harness/shared (the
// exact schema the REST layer validates with) and `mcpServerKey` from the
// compiled backend, so the written record is byte-identical to what the REST
// layer reads. Run `npm run build -w @harness/shared -w @harness/backend` first.
//
// Usage:
//   SEED_ORG=<org> SEED_DRY_RUN=1 node infra/scripts/register-humanlayer-approvals-mcp.mjs  # report only
//   SEED_ORG=<org> node infra/scripts/register-humanlayer-approvals-mcp.mjs                 # write to `harness`
import { pathToFileURL } from 'node:url';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { PutCommand } from '@aws-sdk/lib-dynamodb';
import { repoRoot, TABLE, makeDocClient, importBackendDist } from './lib/common.mjs';

const sharedDist = path.join(repoRoot, 'packages', 'shared', 'dist');
const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

const ORG = process.env.SEED_ORG;

if (!ORG) {
  console.error('[reg-hl-approvals] SEED_ORG is required (no default — refuses to guess the org).');
  process.exit(1);
}

if (!existsSync(path.join(sharedDist, 'index.js'))) {
  console.error(
    `[reg-hl-approvals] missing ${sharedDist}/index.js — run \`npm run build -w @harness/shared\` first.`,
  );
  process.exit(1);
}
if (!existsSync(path.join(backendDist, 'db', 'keys.js'))) {
  console.error(
    `[reg-hl-approvals] missing ${backendDist}/db/keys.js — run \`npm run build -w @harness/backend\` first.`,
  );
  process.exit(1);
}

// Compiled schema + scope helper (what the REST layer uses) and the key builder.
const { mcpServerSchema, orgScope } = await import(
  pathToFileURL(path.join(sharedDist, 'index.js')).href
);
const { mcpServerKey } = await importBackendDist('db', 'keys.js');

// The single stdio MCP server record. Validate it through the compiled schema so
// the on-disk shape matches exactly what `POST /mcp-servers` would accept.
const candidate = {
  name: 'humanlayer-approvals',
  scope: orgScope(ORG),
  transport: 'stdio',
  command: 'humanlayer',
  args: ['mcp', 'claude_approvals'],
  env: {},
  createdBy: { userId: 'system', name: 'system' },
};

const parsed = mcpServerSchema.safeParse(candidate);
if (!parsed.success) {
  console.error('[reg-hl-approvals] record failed mcpServerSchema validation:');
  console.error(parsed.error.message);
  process.exit(1);
}
const server = parsed.data;

if (process.env.SEED_DRY_RUN) {
  const key = mcpServerKey(server.scope, server.name);
  console.log(`[dry-run] mcp-server ${server.name} @ ${server.scope.tier}#${server.scope.id}`);
  console.log(`[dry-run] key ${JSON.stringify(key)}`);
  console.log(`[dry-run] item ${JSON.stringify({ ...key, ...server })}`);
  console.log(
    `[reg-hl-approvals] DRY RUN — 1 MCP-server record (${server.name}) targeting org#${ORG} in ${TABLE}. Nothing written.`,
  );
} else {
  const doc = makeDocClient();
  await doc.send(
    new PutCommand({
      TableName: TABLE,
      Item: { ...mcpServerKey(server.scope, server.name), ...server },
    }),
  );
  console.log(
    `[reg-hl-approvals] wrote 1 MCP-server record (${server.name}) to ${TABLE} at org#${ORG}.`,
  );
}
