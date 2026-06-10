// Register the HumanLayer-derived subagents (the 6 `.claude/agents/*.md` files)
// into ONE org's catalog — by default NOT org-wide.
//
// Modeled EXACTLY on seed-humanlayer-ace.mjs: it does NOT read any shared
// manifest and is NOT wired into starter.ts / seed-all-orgs.mjs, so the set
// lands in the REQUIRED `SEED_ORG`'s catalog and nowhere else.
//
// Reuses the compiled backend's `buildSeedAgents` + `agentKey` so records are
// byte-identical to what the REST layer reads. Run `npm run build -w @harness/backend` first.
//
// Usage:
//   SEED_ORG=<org> SEED_DRY_RUN=1 node infra/scripts/seed-humanlayer-agents.mjs   # report only
//   SEED_ORG=<org> node infra/scripts/seed-humanlayer-agents.mjs                  # write to `harness`
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import path from 'node:path';
import { PutCommand } from '@aws-sdk/lib-dynamodb';
import {
  repoRoot,
  TABLE,
  makeDocClient,
  importBackendDist,
  requireBackendDist,
} from './lib/common.mjs';
import { parseFrontmatter, readBundleManifest } from './lib/catalog.mjs';

const agentsDir = path.join(repoRoot, '.claude', 'agents');
const bundlesManifestPath = path.join(repoRoot, 'catalog', 'agents', 'bundles.json');

const ORG = process.env.SEED_ORG;

if (!ORG) {
  console.error('[seed-hl-agents] SEED_ORG is required (no default — refuses to guess the org).');
  process.exit(1);
}

requireBackendDist('seed-hl-agents', 'seed/agents.js');
const { buildSeedAgents } = await importBackendDist('seed', 'agents.js');
const { agentKey } = await importBackendDist('db', 'keys.js');

// Discover the agent files (explicit *.md scan under .claude/agents).
const mdFiles = existsSync(agentsDir)
  ? readdirSync(agentsDir)
      .filter((f) => f.endsWith('.md'))
      .sort()
  : [];
if (mdFiles.length === 0) {
  console.error(
    `[seed-hl-agents] no .claude/agents/*.md files found in ${agentsDir} — nothing to seed.`,
  );
  process.exit(1);
}

const files = [];
for (const file of mdFiles) {
  const md = path.join(agentsDir, file);
  // Extended front matter: agents also carry `tools`/`model` and the body prompt.
  const fm = parseFrontmatter(readFileSync(md, 'utf8'), { extended: true });
  const fallback = file.replace(/\.md$/, '');
  files.push({
    name: fm.name ?? fallback,
    description: fm.description,
    tools: fm.tools,
    model: fm.model,
    prompt: fm.prompt,
  });
}

// The agent-bundle manifest (single source of truth for how seeded agents are
// grouped, mirroring catalog/skills/bundles.json). Absent -> no bundles, silently.
const manifest = readBundleManifest(bundlesManifestPath, 'seed-hl-agents', {
  warnIfAbsent: false,
});

// buildSeedAgents: one org-scoped record per file (skills:[], system authorship),
// plus one kind:'bundle' record per manifest entry.
const records = buildSeedAgents(ORG, files, manifest);

if (process.env.SEED_DRY_RUN) {
  for (const r of records) {
    console.log(
      `[dry-run] agent ${r.name} @ ${r.scope.tier}#${r.scope.id} model=${r.model} tools=[${r.tools.join(', ')}]`,
    );
  }
  console.log(
    `[seed-hl-agents] DRY RUN — ${records.length} agent records targeting org#${ORG} in ${TABLE}. Nothing written.`,
  );
} else {
  const doc = makeDocClient();
  for (const record of records) {
    await doc.send(
      new PutCommand({
        TableName: TABLE,
        Item: { ...agentKey(record.scope, record.name), ...record },
      }),
    );
  }
  console.log(`[seed-hl-agents] wrote ${records.length} agent records to ${TABLE} at org#${ORG}.`);
}
