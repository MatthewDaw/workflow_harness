// Register the `humanlayer-contact` MCP server (HITL OUTBOUND contact) into ONE
// org's MCP-servers catalog — by default NOT org-wide.
//
// This is the sibling of register-humanlayer-approvals-mcp.mjs. Where the
// approvals server GATES a permission the agent is about to use
// (`request_permission`), this server lets the agent PROACTIVELY REACH A HUMAN
// out of band for a decision — HumanLayer's headline `contact_human` capability
// (Slack / email / web, per the deployment's HUMANLAYER_* env). It is most
// valuable in autonomous / headless runs (ralph_*, founder_mode, oneshot) where
// no human is at the terminal but a judgment call still needs one.
//
// Same containment rules as its sibling: a DEDICATED, SCOPED registration keyed
// on the REQUIRED `SEED_ORG`, writing exactly one org-scoped record, wired into
// NOTHING that propagates org-wide (no starter.ts, no seed-all-orgs.mjs, no
// bundles.json). A human runs the real write later.
//
// The server is a local (stdio) transport: the daemon spawns `humanlayer mcp
// serve`, which exposes the tool `mcp__humanlayer-contact__contact_human` in a
// session. A project opts the server in (enabledMcpServers), or an agent carries
// it (mcpServers[]).
//
// env: contact routing needs `HUMANLAYER_API_KEY` and a channel
// (`HUMANLAYER_SLACK_CHANNEL` or `HUMANLAYER_EMAIL_ADDRESS`); with none set,
// HumanLayer falls back to its web UI. Those are deployment secrets, so this
// script registers `env: {}` (matching the approvals record) and leaves the
// secret/channel to be supplied where the daemon runs. SECURITY: `env` values
// are stored in DynamoDB as plaintext (see mcpServerSchema's note) — do NOT bake
// a real API key into this catalog record.
//
// Reuses the compiled `mcpServerSchema` + `orgScope` from @harness/shared and
// `mcpServerKey` from the compiled backend, so the written record is
// byte-identical to what the REST layer reads. Run
// `npm run build -w @harness/shared -w @harness/backend` first.
//
// Usage:
//   SEED_ORG=<org> SEED_DRY_RUN=1 node infra/scripts/register-humanlayer-contact-mcp.mjs  # report only
//   SEED_ORG=<org> node infra/scripts/register-humanlayer-contact-mcp.mjs                 # write to `harness`
import { fileURLToPath, pathToFileURL } from 'node:url';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const sharedDist = path.join(repoRoot, 'packages', 'shared', 'dist');
const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

const ORG = process.env.SEED_ORG;
const TABLE = process.env.HARNESS_TABLE ?? 'harness';
const REGION = process.env.AWS_REGION ?? 'us-east-1';

if (!ORG) {
  console.error('[reg-hl-contact] SEED_ORG is required (no default — refuses to guess the org).');
  process.exit(1);
}

if (!existsSync(path.join(sharedDist, 'index.js'))) {
  console.error(
    `[reg-hl-contact] missing ${sharedDist}/index.js — run \`npm run build -w @harness/shared\` first.`,
  );
  process.exit(1);
}
if (!existsSync(path.join(backendDist, 'db', 'keys.js'))) {
  console.error(
    `[reg-hl-contact] missing ${backendDist}/db/keys.js — run \`npm run build -w @harness/backend\` first.`,
  );
  process.exit(1);
}

// Compiled schema + scope helper (what the REST layer uses) and the key builder.
const { mcpServerSchema, orgScope } = await import(
  pathToFileURL(path.join(sharedDist, 'index.js')).href
);
const { mcpServerKey } = await import(pathToFileURL(path.join(backendDist, 'db', 'keys.js')).href);

// The single stdio MCP server record. Validate it through the compiled schema so
// the on-disk shape matches exactly what `POST /mcp-servers` would accept.
const candidate = {
  name: 'humanlayer-contact',
  scope: orgScope(ORG),
  transport: 'stdio',
  command: 'humanlayer',
  args: ['mcp', 'serve'],
  env: {},
  createdBy: { userId: 'system', name: 'system' },
};

const parsed = mcpServerSchema.safeParse(candidate);
if (!parsed.success) {
  console.error('[reg-hl-contact] record failed mcpServerSchema validation:');
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
    `[reg-hl-contact] DRY RUN — 1 MCP-server record (${server.name}) targeting org#${ORG} in ${TABLE}. Nothing written.`,
  );
} else {
  const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
  await doc.send(
    new PutCommand({
      TableName: TABLE,
      Item: { ...mcpServerKey(server.scope, server.name), ...server },
    }),
  );
  console.log(
    `[reg-hl-contact] wrote 1 MCP-server record (${server.name}) to ${TABLE} at org#${ORG}.`,
  );
}
