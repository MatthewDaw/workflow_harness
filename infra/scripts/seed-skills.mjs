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
const bundlesManifest = path.join(skillsDir, 'bundles.json');
const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

const ORG = process.env.SEED_ORG ?? 'acme';
const TABLE = process.env.HARNESS_TABLE ?? 'harness';
const REGION = process.env.AWS_REGION ?? 'us-east-1';
// Owner of the user-scoped grant for non-bundle skills (compound-engineering,
// gstack, playwright-cli). They are NOT in the org default — they seed at this
// user's scope so a granted account can see them while a fresh org cannot.
// Defaults to the inert 'system' owner (no human account lands there) unless a
// deploy points it at a real account. buildSeedSkills applies the same default.
const GRANT_OWNER = process.env.SEED_GRANT_OWNER ?? 'system';

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
const { skillKey } = await import(pathToFileURL(path.join(backendDist, 'db', 'keys.js')).href);

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

/**
 * Read the WHOLE skill directory tree into a `{ relPath: contents }` map
 * (U-Skill-Store), so a skill ships SKILL.md PLUS sibling scripts/resources.
 * Paths are POSIX-relative to the skill dir. Skips obvious binary/oversize files
 * defensively (the catalog stores plaintext only).
 */
function readSkillDir(dir) {
  const out = {};
  const walk = (cur, rel) => {
    for (const entry of readdirSync(cur, { withFileTypes: true })) {
      const abs = path.join(cur, entry.name);
      const relPath = rel ? `${rel}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        walk(abs, relPath);
      } else if (entry.isFile()) {
        out[relPath] = readFileSync(abs, 'utf8');
      }
    }
  };
  walk(dir, '');
  return out;
}

function readSkillFiles() {
  if (!existsSync(skillsDir)) {
    console.error(`[seed-skills] no skills dir at ${skillsDir}`);
    process.exit(1);
  }
  const files = [];
  for (const entry of readdirSync(skillsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dir = path.join(skillsDir, entry.name);
    const skillMd = path.join(dir, 'SKILL.md');
    if (!existsSync(skillMd)) continue;
    const body = readFileSync(skillMd, 'utf8');
    const { name, description } = parseFrontmatter(body);
    const dirFiles = readSkillDir(dir);
    files.push({ name: name ?? entry.name, description, body, files: dirFiles });
  }
  return files.sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Load the bundle manifest (`.claude/skills/bundles.json`) — the single source of
 * truth for how skills are grouped into bundles. Absent manifest => no bundles
 * (every skill standalone). Each entry is `{ description, members[] }`.
 */
function readBundleManifest() {
  if (!existsSync(bundlesManifest)) {
    console.warn(
      `[seed-skills] no bundle manifest at ${bundlesManifest} — seeding all skills standalone.`,
    );
    return {};
  }
  try {
    return JSON.parse(readFileSync(bundlesManifest, 'utf8'));
  } catch (err) {
    console.error(`[seed-skills] could not parse ${bundlesManifest}:`, err);
    process.exit(1);
  }
}

async function main() {
  const files = readSkillFiles();
  if (files.length === 0) {
    console.error('[seed-skills] found no SKILL.md files to seed');
    process.exit(1);
  }
  const manifest = readBundleManifest();
  const records = buildSeedSkills(ORG, files, manifest, GRANT_OWNER);

  if (process.env.SEED_DRY_RUN) {
    for (const r of records) {
      const where = `${r.scope.tier}#${r.scope.id}`;
      const desc =
        r.kind === 'bundle'
          ? `members=[${r.members.join(', ')}]`
          : `${r.description.slice(0, 60)}…`;
      console.log(`[dry-run] ${r.kind} ${r.name} @ ${where} (${r.source}) ${desc}`);
    }
    const orgDefault = records.filter((r) => r.scope.tier === 'org').map((r) => r.name);
    const granted = records.filter((r) => r.scope.tier === 'user').map((r) => r.name);
    console.log(`[seed-skills] DRY RUN — ${records.length} records, nothing written.`);
    console.log(`  org-default (org#${ORG}): ${orgDefault.join(', ')}`);
    console.log(`  user-granted (user#${GRANT_OWNER}): ${granted.join(', ') || '(none)'}`);
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

  const orgSkills = records
    .filter((r) => r.kind === 'skill' && r.scope.tier === 'org')
    .map((r) => r.name);
  const grantedSkills = records
    .filter((r) => r.kind === 'skill' && r.scope.tier === 'user')
    .map((r) => r.name);
  const bundleNames = records.filter((r) => r.kind === 'bundle').map((r) => r.name);
  console.log(
    `[seed-skills] seeded ${records.length} records into ${TABLE}.\n` +
      `  org-default skills (org#${ORG}): ${orgSkills.join(', ')}\n` +
      `  user-granted skills (user#${GRANT_OWNER}): ${grantedSkills.join(', ') || '(none)'}\n` +
      `  bundles (org#${ORG}): ${bundleNames.map((b) => `${b}`).join(', ') || '(none)'}`,
  );
}

main().catch((err) => {
  console.error('[seed-skills] failed:', err);
  process.exit(1);
});
