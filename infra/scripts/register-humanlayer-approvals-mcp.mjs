// Register the `humanlayer-approvals` MCP server (HITL approvals) into ONE org's
// MCP-servers catalog — by default NOT org-wide.
//
// This is the MCP-catalog analogue of seed-humanlayer-ace.mjs: a DEDICATED,
// SCOPED registration keyed on the REQUIRED `SEED_ORG`. It writes exactly one
// org-scoped record and is wired into NOTHING that propagates org-wide (no
// starter.ts, no seed-all-orgs.mjs, no bundles.json).
//
// The server is a local (stdio) transport: the daemon spawns `humanlayer mcp
// claude_approvals`, which exposes the tool
// `mcp__humanlayer-approvals__request_permission` in a session. A project opts
// the server in (enabledMcpServers), or an agent carries it (mcpServers[]).
//
// Run `npm run build -w @harness/shared -w @harness/backend` first.
//
// Usage:
//   SEED_ORG=<org> SEED_DRY_RUN=1 node infra/scripts/register-humanlayer-approvals-mcp.mjs  # report only
//   SEED_ORG=<org> node infra/scripts/register-humanlayer-approvals-mcp.mjs                 # write to `harness`
import { registerMcpServer } from './lib/common.mjs';

await registerMcpServer({
  name: 'humanlayer-approvals',
  args: ['mcp', 'claude_approvals'],
  logTag: 'reg-hl-approvals',
});
