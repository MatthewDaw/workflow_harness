// Read-only HumanLayer verifier: the automated half of U7's acceptance check.
// It inspects all three layers and prints a pass/fail checklist — it NEVER
// writes anything.
//
//   A. Catalog (org#SEED_ORG)     27-member humanlayer-ace bundle, 6 agents, 2 MCPs
//   B. Project opt-in (PROJECT_ID) enabledSkills/Agents/McpServers cover the set
//   C. Local ~/.claude+           skills/agents materialized, both MCPs in .mcp.json
//
// A and B are HARD checks (catalog/opt-in drift exits non-zero). C is SOFT: an
// empty ~/.claude+ just means the claude+ sync (`/hq-update-skills`) has not run
// yet — reported as a WARN with the fix, not a failure — since that step happens
// in the PTY, not from a script. It also WARNs if the pre-catalog hand-placed
// `humanlayer-approvals` still sits in ~/.claude+/.claude.json (a duplicate of the
// catalog-managed .mcp.json entry once sync lands).
//
// Reuses the compiled `scopePartition` + `projectKey` so it reads exactly the
// keys the REST layer writes. Run `npm run build -w @harness/backend` first.
//
// Usage:
//   SEED_ORG="test org" PROJECT_ID=workflow-harness node infra/scripts/verify-humanlayer.mjs
//   SEED_ORG="test org" node infra/scripts/verify-humanlayer.mjs   # catalog + local only (skips opt-in)
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { GetCommand } from '@aws-sdk/lib-dynamodb';
import {
  TABLE,
  makeDocClient,
  importBackendDist,
  requireBackendDist,
  queryByPrefix,
} from './lib/common.mjs';

const ORG = process.env.SEED_ORG;
const PROJECT_ID = process.env.PROJECT_ID; // optional
const BUNDLE = process.env.BUNDLE ?? 'humanlayer-ace';

const AGENTS_EXPECTED = [
  'codebase-analyzer',
  'codebase-locator',
  'codebase-pattern-finder',
  'thoughts-analyzer',
  'thoughts-locator',
  'web-search-researcher',
];
const MCP_EXPECTED = ['humanlayer-approvals', 'humanlayer-contact'];
const EXPECTED_SKILL_COUNT = 27;

if (!ORG) {
  console.error('[verify-hl] SEED_ORG is required (no default — refuses to guess the org).');
  process.exit(1);
}
requireBackendDist('verify-hl', 'db/keys.js');

const { projectKey, scopePartition } = await importBackendDist('db', 'keys.js');

const doc = makeDocClient();
const orgPart = scopePartition({ tier: 'org', id: ORG });

let hardFail = false;
const ok = (label) => console.log(`  ✓ ${label}`);
const bad = (label) => {
  hardFail = true;
  console.log(`  ✗ ${label}`);
};
const warn = (label) => console.log(`  ⚠ ${label}`);

const missing = (need, have) => need.filter((n) => !have.includes(n));
const queryNames = (skPrefix) => queryByPrefix(doc, TABLE, orgPart, skPrefix);

// ---- A. Catalog (hard) ----
console.log(`\n[A] Catalog — org#${ORG}`);
const bundleRes = await doc.send(
  new GetCommand({ TableName: TABLE, Key: { PK: orgPart, SK: `SKILL#${BUNDLE}` } }),
);
const bundleMembers = bundleRes.Item?.members ?? [];
if (!bundleRes.Item) bad(`bundle '${BUNDLE}' present`);
else if (bundleMembers.length !== EXPECTED_SKILL_COUNT)
  bad(`bundle '${BUNDLE}' has ${EXPECTED_SKILL_COUNT} members (found ${bundleMembers.length})`);
else ok(`bundle '${BUNDLE}' present with ${bundleMembers.length} members`);

const skillItems = (await queryNames('SKILL#')).filter((i) => i.name !== BUNDLE);
if (skillItems.length >= EXPECTED_SKILL_COUNT)
  ok(`${skillItems.length} member skills registered (>= ${EXPECTED_SKILL_COUNT})`);
else bad(`>= ${EXPECTED_SKILL_COUNT} member skills registered (found ${skillItems.length})`);

const agentNames = (await queryNames('AGENT#')).map((i) => i.name);
const agentsMissing = missing(AGENTS_EXPECTED, agentNames);
if (agentsMissing.length === 0) ok(`all 6 agents registered`);
else bad(`agents missing from catalog: ${agentsMissing.join(', ')}`);

const mcpNames = (await queryNames('MCPSERVER#')).map((i) => i.name);
const mcpMissing = missing(MCP_EXPECTED, mcpNames);
if (mcpMissing.length === 0) ok(`both MCP servers registered (${MCP_EXPECTED.join(', ')})`);
else bad(`MCP servers missing from catalog: ${mcpMissing.join(', ')}`);

// ---- B. Project opt-in (hard, when PROJECT_ID given) ----
if (PROJECT_ID) {
  console.log(`\n[B] Project opt-in — '${PROJECT_ID}'`);
  const projRes = await doc.send(new GetCommand({ TableName: TABLE, Key: projectKey(PROJECT_ID) }));
  const project = projRes.Item;
  if (!project) bad(`project '${PROJECT_ID}' exists`);
  else {
    const enS = project.enabledSkills ?? [];
    const enA = project.enabledAgents ?? [];
    const enM = project.enabledMcpServers ?? [];
    const sMiss = missing(bundleMembers, enS);
    const aMiss = missing(AGENTS_EXPECTED, enA);
    const mMiss = missing(MCP_EXPECTED, enM);
    sMiss.length === 0
      ? ok(`all ${bundleMembers.length} skills enabled`)
      : bad(`skills not enabled: ${sMiss.length} (e.g. ${sMiss.slice(0, 3).join(', ')})`);
    aMiss.length === 0
      ? ok(`all 6 agents enabled`)
      : bad(`agents not enabled: ${aMiss.join(', ')}`);
    mMiss.length === 0
      ? ok(`both MCP servers enabled`)
      : bad(`MCP servers not enabled: ${mMiss.join(', ')}`);
  }
} else {
  console.log(`\n[B] Project opt-in — skipped (no PROJECT_ID)`);
}

// ---- C. Local ~/.claude+ materialization (soft) ----
console.log(`\n[C] Local ~/.claude+ materialization`);
const plus = path.join(os.homedir(), '.claude+');
const countDir = (p, pred) => {
  if (!existsSync(p)) return null;
  return readdirSync(p).filter((n) => pred(path.join(p, n), n)).length;
};
const skillsDir = path.join(plus, 'skills');
const skillCount = countDir(
  skillsDir,
  (full) => statSync(full).isDirectory() && existsSync(path.join(full, 'SKILL.md')),
);
const agentsDir = path.join(plus, 'agents');
const agentCount = countDir(agentsDir, (_full, n) => n.endsWith('.md'));

if (skillCount === null || skillCount === 0)
  warn(`skills not synced into ~/.claude+ yet — run \`/hq-update-skills\` in a claude+ session`);
else if (skillCount >= EXPECTED_SKILL_COUNT) ok(`${skillCount} skills materialized`);
else warn(`only ${skillCount} skills materialized (expected >= ${EXPECTED_SKILL_COUNT}) — re-sync`);

if (agentCount === null || agentCount === 0) warn(`agents not synced into ~/.claude+ yet`);
else if (agentCount >= AGENTS_EXPECTED.length) ok(`${agentCount} agents materialized`);
else warn(`only ${agentCount} agents materialized (expected ${AGENTS_EXPECTED.length})`);

const mcpJsonPath = path.join(plus, '.mcp.json');
if (existsSync(mcpJsonPath)) {
  let servers = {};
  try {
    servers = JSON.parse(readFileSync(mcpJsonPath, 'utf8')).mcpServers ?? {};
  } catch {
    warn(`.mcp.json present but unparseable`);
  }
  const mMiss = missing(MCP_EXPECTED, Object.keys(servers));
  mMiss.length === 0
    ? ok(`both MCP servers in ~/.claude+/.mcp.json`)
    : warn(`.mcp.json missing: ${mMiss.join(', ')} — re-sync after opt-in`);
} else {
  warn(`~/.claude+/.mcp.json absent — MCP servers not synced yet`);
}

// Legacy hand-placed approvals (pre-catalog) — duplicate once .mcp.json lands.
const claudeJsonPath = path.join(plus, '.claude.json');
if (existsSync(claudeJsonPath)) {
  try {
    const top = JSON.parse(readFileSync(claudeJsonPath, 'utf8')).mcpServers ?? {};
    if (top['humanlayer-approvals'])
      warn(
        `legacy 'humanlayer-approvals' still in ~/.claude+/.claude.json — remove it so the ` +
          `catalog-managed .mcp.json entry is the single source`,
      );
  } catch {
    /* ignore unparseable personal config */
  }
}

console.log(
  `\n[verify-hl] ${hardFail ? 'FAIL — catalog/opt-in drift above (exit 1).' : 'PASS — catalog + opt-in correct.'}` +
    ` Local (C) warnings are the manual /hq-update-skills sync, not failures.`,
);
process.exit(hardFail ? 1 : 0);
