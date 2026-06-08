// One-shot HumanLayer setup orchestrator: runs the four scoped catalog
// registrations + the per-project opt-in in the correct order, so closing the
// U7 gaps is a single reproducible command instead of five hand-run scripts.
//
// It SPAWNS the existing standalone scripts verbatim (no duplicated logic) — each
// is idempotent and SEED_DRY_RUN-aware, and those properties carry through here:
//
//   1. seed-humanlayer-ace.mjs            27 skills + the humanlayer-ace bundle
//   2. seed-humanlayer-agents.mjs         6 research agents
//   3. register-humanlayer-approvals-mcp  approvals MCP (request_permission)
//   4. register-humanlayer-contact-mcp    contact MCP (contact_human)
//   5. optin-humanlayer.mjs               enable all of the above on PROJECT_ID
//
// Steps 1-4 need SEED_ORG; step 5 also needs PROJECT_ID. Containment is unchanged
// — every child targets the explicit SEED_ORG and touches nothing org-wide. The
// remaining U7 steps are NOT scriptable from here (they need the claude+ PTY):
// run `/hq-update-skills` to sync into ~/.claude+, then `verify-humanlayer.mjs`.
//
// SEED_DRY_RUN=1 is forwarded to every child (report-only, no writes). Without it,
// the children write to the live `harness` table. Run
// `npm run build -w @harness/shared -w @harness/backend` first (children guard on
// the compiled dist and will say so if it is missing).
//
// Usage:
//   SEED_ORG="test org" PROJECT_ID=workflow-harness SEED_DRY_RUN=1 node infra/scripts/setup-humanlayer.mjs
//   SEED_ORG="test org" PROJECT_ID=workflow-harness node infra/scripts/setup-humanlayer.mjs
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));

const ORG = process.env.SEED_ORG;
const PROJECT_ID = process.env.PROJECT_ID;

if (!ORG) {
  console.error('[setup-hl] SEED_ORG is required (no default — refuses to guess the org).');
  process.exit(1);
}
if (!PROJECT_ID) {
  console.error('[setup-hl] PROJECT_ID is required (the project to opt into the HumanLayer set).');
  process.exit(1);
}

const dry = Boolean(process.env.SEED_DRY_RUN);

const steps = [
  { label: '1/5 skills + bundle', script: 'seed-humanlayer-ace.mjs' },
  { label: '2/5 agents', script: 'seed-humanlayer-agents.mjs' },
  { label: '3/5 approvals MCP', script: 'register-humanlayer-approvals-mcp.mjs' },
  { label: '4/5 contact MCP', script: 'register-humanlayer-contact-mcp.mjs' },
  { label: '5/5 project opt-in', script: 'optin-humanlayer.mjs' },
];

console.log(
  `[setup-hl] ${dry ? 'DRY RUN — ' : ''}org#${ORG}, project '${PROJECT_ID}' — ${steps.length} steps\n`,
);

for (const step of steps) {
  console.log(`\n================= ${step.label} (${step.script}) =================`);
  const res = spawnSync(process.execPath, [path.join(here, step.script)], {
    stdio: 'inherit',
    env: process.env, // SEED_ORG / PROJECT_ID / SEED_DRY_RUN / AWS_* all flow through
  });
  if (res.status !== 0) {
    console.error(
      `\n[setup-hl] ABORTED at ${step.label} (${step.script}) — exit ${res.status}. ` +
        `Earlier steps are idempotent; fix the cause and re-run (safe to repeat).`,
    );
    process.exit(res.status ?? 1);
  }
}

console.log(
  `\n[setup-hl] ${dry ? 'DRY RUN complete — nothing written.' : `done — org#${ORG} catalog + project '${PROJECT_ID}' opt-in are current.`}`,
);
if (!dry) {
  console.log(
    '[setup-hl] NEXT (claude+ PTY, not scriptable here): run `/hq-update-skills` to sync into ~/.claude+, ' +
      'then `SEED_ORG=… PROJECT_ID=… node infra/scripts/verify-humanlayer.mjs`.',
  );
}
