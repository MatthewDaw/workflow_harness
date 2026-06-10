// Register the `humanlayer-contact` MCP server (HITL OUTBOUND contact) into ONE
// org's MCP-servers catalog — by default NOT org-wide.
//
// Sibling of register-humanlayer-approvals-mcp.mjs. Where the approvals server
// GATES a permission the agent is about to use (`request_permission`), this one
// lets the agent PROACTIVELY REACH A HUMAN out of band for a decision —
// HumanLayer's `contact_human` capability (Slack / email / web, per the
// deployment's HUMANLAYER_* env). Most valuable in autonomous / headless runs
// (ralph_*, founder_mode, oneshot) where no human is at the terminal.
//
// Same containment as its sibling: one org-scoped record keyed on the REQUIRED
// `SEED_ORG`, wired into nothing org-wide.
//
// The server is a local (stdio) transport: the daemon spawns `humanlayer mcp
// serve`, which exposes the tool `mcp__humanlayer-contact__contact_human` in a
// session. A project opts the server in (enabledMcpServers), or an agent
// carries it (mcpServers[]).
//
// Contact routing needs `HUMANLAYER_API_KEY` and a channel
// (`HUMANLAYER_SLACK_CHANNEL` or `HUMANLAYER_EMAIL_ADDRESS`); with none set,
// HumanLayer falls back to its web UI. Those are deployment secrets supplied
// where the daemon runs — the catalog record carries `env: {}` because env
// values are stored in DynamoDB as plaintext.
//
// Run `npm run build -w @harness/shared -w @harness/backend` first.
//
// Usage:
//   SEED_ORG=<org> SEED_DRY_RUN=1 node infra/scripts/register-humanlayer-contact-mcp.mjs  # report only
//   SEED_ORG=<org> node infra/scripts/register-humanlayer-contact-mcp.mjs                 # write to `harness`
import { registerMcpServer } from './lib/common.mjs';

await registerMcpServer({
  name: 'humanlayer-contact',
  args: ['mcp', 'serve'],
  logTag: 'reg-hl-contact',
});
