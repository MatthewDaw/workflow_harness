// Seeds the skills bundled with Command HQ + claude+ into the deployed `harness`
// DynamoDB table at org scope, grouped as one bundle (plan U22). A fresh deploy
// starts with an empty registry, so without this the Skills tab is empty until a
// device connects and syncs. Running this makes the "command-hq-starter" bundle
// visible to every user in the org out of the box.
//
// Source of truth is the repo's `.claude/skills/<name>/SKILL.md`. The record
// shape + keys are reused from the compiled backend (packages/backend/dist) so
// the seed can never drift from how the REST layer reads skills — run
// `npm run build -w @harness/backend` first (the deploy workflow already does).
//
// Usage:
//   node infra/scripts/seed-skills.mjs            # org=acme, table=harness, region=us-east-1
//   SEED_ORG=acme HARNESS_TABLE=harness AWS_REGION=us-east-1 node infra/scripts/seed-skills.mjs
//
// Idempotent: each skill is upserted by key, so re-running converges.
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const skillsDir = path.join(repoRoot, '.claude', 'skills');
const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

const ORG = process.env.SEED_ORG ?? 'acme';
const TABLE = process.env.HARNESS_TABLE ?? 'harness';
const REGION = process.env.AWS_REGION ?? 'us-east-1';

if (!existsSync(path.join(backendDist, 'seed', 'skills.js'))) {
  console.error(
    `[seed-skills] missing ${backendDist}/seed/skills.js — run \`npm run build -w @harness/backend\` first.`,
  );
  process.exit(1);
}

// Reuse the canonical record builder + key scheme from the built backend.
const { buildSeedSkills } = await import(
  pathToFileURL(path.join(backendDist, 'seed', 'skills.js')).href
);
const { skillKey } = await import(
  pathToFileURL(path.join(backendDist, 'db', 'keys.js')).href
);

/**
 * Parse the `name` and (folded) `description` out of a SKILL.md YAML front
 * matter block. The repo skills use `description: >-` folded scalars indented
 * under the key, so we gather indented continuation lines until the next
 * top-level key or the closing `---`.
 */
function parseFrontmatter(md) {
  const lines = md.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') return { name: undefined, description: '' };
  let name;
  const descParts = [];
  let inDesc = false;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === '---') break;
    const top = /^([A-Za-z0-9_-]+):\s?(.*)$/.exec(line);
    if (top && !line.startsWith(' ')) {
      inDesc = false;
      const [, key, value] = top;
      if (key === 'name') name = value.trim();
      else if (key === 'description') {
        inDesc = true;
        const v = value.trim();
        if (v && v !== '>-' && v !== '>' && v !== '|' && v !== '|-') descParts.push(v);
      }
      continue;
    }
    if (inDesc && line.trim()) descParts.push(line.trim());
  }
  return { name, description: descParts.join(' ').trim() };
}

function readSkillFiles() {
  if (!existsSync(skillsDir)) {
    console.error(`[seed-skills] no skills dir at ${skillsDir}`);
    process.exit(1);
  }
  const files = [];
  for (const entry of readdirSync(skillsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const skillMd = path.join(skillsDir, entry.name, 'SKILL.md');
    if (!existsSync(skillMd)) continue;
    const body = readFileSync(skillMd, 'utf8');
    const { name, description } = parseFrontmatter(body);
    files.push({ name: name ?? entry.name, description, body });
  }
  return files.sort((a, b) => a.name.localeCompare(b.name));
}

async function main() {
  const files = readSkillFiles();
  if (files.length === 0) {
    console.error('[seed-skills] found no SKILL.md files to seed');
    process.exit(1);
  }
  const records = buildSeedSkills(ORG, files);

  if (process.env.SEED_DRY_RUN) {
    for (const r of records) {
      const desc = r.kind === 'bundle' ? `members=[${r.members.join(', ')}]` : `${r.description.slice(0, 60)}…`;
      console.log(`[dry-run] ${r.kind} ${r.name} @ org#${ORG} (${r.source}) ${desc}`);
    }
    console.log(`[seed-skills] DRY RUN — ${records.length} records, nothing written.`);
    return;
  }

  const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
  for (const record of records) {
    await doc.send(
      new PutCommand({
        TableName: TABLE,
        Item: { ...skillKey(record.scope, record.name), ...record },
      }),
    );
  }

  const skillNames = records.filter((r) => r.kind === 'skill').map((r) => r.name);
  console.log(
    `[seed-skills] seeded ${skillNames.length} skills + 1 bundle into ${TABLE} ` +
      `at org#${ORG}: ${skillNames.join(', ')}`,
  );
}

main().catch((err) => {
  console.error('[seed-skills] failed:', err);
  process.exit(1);
});
