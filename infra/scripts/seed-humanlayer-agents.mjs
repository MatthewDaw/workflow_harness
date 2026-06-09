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
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const agentsDir = path.join(repoRoot, '.claude', 'agents');
const bundlesManifestPath = path.join(repoRoot, 'catalog', 'agents', 'bundles.json');
const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

const ORG = process.env.SEED_ORG;
const TABLE = process.env.HARNESS_TABLE ?? 'harness';
const REGION = process.env.AWS_REGION ?? 'us-east-1';

if (!ORG) {
  console.error('[seed-hl-agents] SEED_ORG is required (no default — refuses to guess the org).');
  process.exit(1);
}

if (!existsSync(path.join(backendDist, 'seed', 'agents.js'))) {
  console.error(
    `[seed-hl-agents] missing ${backendDist}/seed/agents.js — run \`npm run build -w @harness/backend\` first.`,
  );
  process.exit(1);
}
const { buildSeedAgents } = await import(
  pathToFileURL(path.join(backendDist, 'seed', 'agents.js')).href
);
const { agentKey } = await import(pathToFileURL(path.join(backendDist, 'db', 'keys.js')).href);

// Frontmatter parser extended from seed-humanlayer-ace.mjs: also extracts
// `tools` (CSV -> trimmed string[]) and `model`, plus the body (the prompt).
function parseFrontmatter(md) {
  const lines = md.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') {
    return { name: undefined, description: '', tools: [], model: '', prompt: md };
  }
  let name;
  let model = '';
  let tools = [];
  const descParts = [];
  let inDesc = false;
  let bodyStart = lines.length;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === '---') {
      bodyStart = i + 1;
      break;
    }
    const top = /^([A-Za-z0-9_-]+):\s?(.*)$/.exec(line);
    if (top && !line.startsWith(' ')) {
      inDesc = false;
      const [, key, value] = top;
      if (key === 'name') name = value.trim();
      else if (key === 'model') model = value.trim();
      else if (key === 'tools') {
        tools = value
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean);
      } else if (key === 'description') {
        inDesc = true;
        const v = value.trim();
        if (v && v !== '>-' && v !== '>' && v !== '|' && v !== '|-') descParts.push(v);
      }
      continue;
    }
    if (inDesc && line.trim()) descParts.push(line.trim());
  }
  const prompt = lines.slice(bodyStart).join('\n').trim();
  return { name, description: descParts.join(' ').trim(), tools, model, prompt };
}

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
  const raw = readFileSync(md, 'utf8');
  const fm = parseFrontmatter(raw);
  const fallback = file.replace(/\.md$/, '');
  files.push({
    name: fm.name ?? fallback,
    description: fm.description,
    tools: fm.tools,
    model: fm.model,
    prompt: fm.prompt,
  });
}

// Load the agent-bundle manifest (single source of truth for how seeded agents
// are grouped, mirroring catalog/skills/bundles.json). Absent/invalid -> no bundles.
let manifest = {};
if (existsSync(bundlesManifestPath)) {
  try {
    manifest = JSON.parse(readFileSync(bundlesManifestPath, 'utf8'));
  } catch (err) {
    console.error(`[seed-hl-agents] failed to parse ${bundlesManifestPath}: ${err.message}`);
    process.exit(1);
  }
}

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
  const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
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
