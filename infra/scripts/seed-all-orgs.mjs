// Backfill the Command HQ `command-hq-starter` skills into EVERY existing org in
// the deployed `harness` table — not just the one template org `seed-skills.mjs`
// seeds. New orgs get the starter set on create (the orgs handler clones it from
// the template org), but orgs that existed BEFORE that wiring — or that were
// created while the template org was unseeded — show an empty Skills tab. This
// enumerates every org and seeds each at org scope so the catalog is populated
// for all of them.
//
// It ALSO (re)seeds the template org (`acme` by default) so the create-time
// clone has a canonical source to copy from going forward.
//
// Source of truth is the repo's `catalog/skills/<name>/SKILL.md` + `bundles.json`,
// run through the SAME `buildSeedSkills` builder + `skillKey` scheme the REST
// layer reads, loaded from the compiled backend (packages/backend/dist) so the
// seed can never drift. Run `npm run build -w @harness/backend` first.
//
// Usage:
//   node infra/scripts/seed-all-orgs.mjs                 # table=harness, region=us-east-1
//   SEED_DRY_RUN=1 node infra/scripts/seed-all-orgs.mjs  # enumerate + report, write nothing
//   HARNESS_TABLE=harness AWS_REGION=us-east-1 node infra/scripts/seed-all-orgs.mjs
//
// Idempotent: each record is upserted by key, so re-running converges.
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand, ScanCommand } from '@aws-sdk/lib-dynamodb';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const skillsDir = path.join(repoRoot, 'catalog', 'skills');
const bundlesManifest = path.join(skillsDir, 'bundles.json');
const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

const TABLE = process.env.HARNESS_TABLE ?? 'harness';
const REGION = process.env.AWS_REGION ?? 'us-east-1';
// The template org new-org creation clones the starter bundle from. We always
// (re)seed it so the clone-on-create path has a canonical source even if no real
// org named this exists yet. Keep in sync with starter.ts's STARTER_TEMPLATE_ORG.
const TEMPLATE_ORG = process.env.STARTER_TEMPLATE_ORG ?? 'acme';
// Owner of the user-scoped grant for the non-bundle skills (compound-engineering,
// gstack, playwright-cli). They are NOT part of any org default; they seed once at
// this user's scope so a granted account can see them while a fresh org cannot.
const GRANT_OWNER = process.env.SEED_GRANT_OWNER ?? 'system';

if (!existsSync(path.join(backendDist, 'seed', 'skills.js'))) {
  console.error(
    `[seed-all-orgs] missing ${backendDist}/seed/skills.js — run \`npm run build -w @harness/backend\` first.`,
  );
  process.exit(1);
}
if (!existsSync(path.join(backendDist, 'seed', 'workflows.js'))) {
  console.error(
    `[seed-all-orgs] missing ${backendDist}/seed/workflows.js — run \`npm run build -w @harness/backend\` first.`,
  );
  process.exit(1);
}

// Reuse the canonical record builder + key scheme from the built backend so this
// seed produces byte-identical records to what the REST layer reads/writes.
const { buildSeedSkills, assertBaseVariantOnly } = await import(
  pathToFileURL(path.join(backendDist, 'seed', 'skills.js')).href
);
// Workflows are structured data (no markdown tree), so their source of truth is
// the compiled STARTER_WORKFLOWS — seeded org-wide here so EVERY org gets the
// starter DAG (not a single SEED_ORG, per the all-orgs convention).
const { buildSeedWorkflows, STARTER_WORKFLOWS } = await import(
  pathToFileURL(path.join(backendDist, 'seed', 'workflows.js')).href
);
const { skillKey, workflowKey } = await import(
  pathToFileURL(path.join(backendDist, 'db', 'keys.js')).href
);

/** Parse `name` + (folded) `description` from a SKILL.md YAML front matter block. */
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
 * (U-Skill-Store): SKILL.md PLUS sibling scripts/resources. POSIX-relative paths.
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
    console.error(`[seed-all-orgs] no skills dir at ${skillsDir}`);
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
    files.push({ name: name ?? entry.name, description, body, files: readSkillDir(dir) });
  }
  return files.sort((a, b) => a.name.localeCompare(b.name));
}

function readBundleManifest() {
  if (!existsSync(bundlesManifest)) {
    console.warn(
      `[seed-all-orgs] no bundle manifest at ${bundlesManifest} — seeding all skills standalone.`,
    );
    return {};
  }
  try {
    return JSON.parse(readFileSync(bundlesManifest, 'utf8'));
  } catch (err) {
    console.error(`[seed-all-orgs] could not parse ${bundlesManifest}:`, err);
    process.exit(1);
  }
}

/**
 * Enumerate every org by scanning for its META record (`PK = ORG#<name>`,
 * `SK = META`). Paginated so it survives a table larger than one scan page.
 */
async function listOrgNames(doc) {
  const names = [];
  let ExclusiveStartKey;
  do {
    const res = await doc.send(
      new ScanCommand({
        TableName: TABLE,
        FilterExpression: 'SK = :meta AND begins_with(PK, :orgp)',
        ExpressionAttributeValues: { ':meta': 'META', ':orgp': 'ORG#' },
        ProjectionExpression: 'PK',
        ExclusiveStartKey,
      }),
    );
    for (const item of res.Items ?? []) {
      if (typeof item.PK === 'string' && item.PK.startsWith('ORG#')) {
        names.push(item.PK.slice('ORG#'.length));
      }
    }
    ExclusiveStartKey = res.LastEvaluatedKey;
  } while (ExclusiveStartKey);
  return names;
}

async function main() {
  const files = readSkillFiles();
  if (files.length === 0) {
    console.error('[seed-all-orgs] found no SKILL.md files to seed');
    process.exit(1);
  }
  const manifest = readBundleManifest();

  const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));

  const discovered = await listOrgNames(doc);
  // Union the discovered orgs with the template org so the clone-on-create source
  // is always seeded, even if no real org named TEMPLATE_ORG exists yet.
  const orgs = Array.from(new Set([TEMPLATE_ORG, ...discovered])).sort();

  console.log(
    `[seed-all-orgs] ${discovered.length} org(s) discovered; seeding ${orgs.length} ` +
      `(incl. template '${TEMPLATE_ORG}'): ${orgs.join(', ')}`,
  );

  // Per-org we write only the ORG-scoped records (the bundle + its members) — the
  // org-wide default catalog. The user-scoped grant records (the non-bundle
  // built-ins) do not depend on the org, so we write them exactly once below.
  let orgRecordCount = 0;
  let workflowRecordCount = 0;
  for (const org of orgs) {
    const records = buildSeedSkills(org, files, manifest, GRANT_OWNER).filter(
      (r) => r.scope.tier === 'org',
    );
    // U19 — SEED-SAFE PROMOTION. Re-assert the base-variant-only contract on the
    // exact records this loop is about to write (buildSeedSkills already asserts,
    // but the filter could in principle drop the failing record; re-assert post-
    // filter so the script can NEVER write a fork). Combined with the fact that we
    // only ever PutCommand `skillKey(...)` (the BASE variant's live record) and
    // NEVER a `#TRUE` pointer row below, a fork promoted to #TRUE survives re-seed.
    assertBaseVariantOnly(records);
    // The starter workflow(s) — org-scoped, one record each — alongside skills.
    const workflows = buildSeedWorkflows(org, STARTER_WORKFLOWS);
    if (process.env.SEED_DRY_RUN) {
      const skills = records.filter((r) => r.kind === 'skill').map((r) => r.name);
      const bundles = records.filter((r) => r.kind === 'bundle').map((r) => r.name);
      console.log(
        `  [dry-run] org#${org}: ${records.length} records ` +
          `(${skills.length} skills + ${bundles.length} bundle[s]: ${bundles.join(', ')}) ` +
          `+ ${workflows.length} workflow[s]: ${workflows.map((w) => w.name).join(', ')}`,
      );
      orgRecordCount += records.length;
      workflowRecordCount += workflows.length;
      continue;
    }
    for (const record of records) {
      const key = skillKey(record.scope, record.name);
      // Defense in depth: the seed writes the live BASE-variant record only and
      // must never touch a per-baseName `#TRUE` pointer or a `#r<N>` revision row.
      // skillKey() produces `SKILL#<name>`, never those side-record SKs — assert it
      // so a key-scheme change can't silently let the seed clobber a promotion.
      if (key.SK.endsWith('#TRUE') || /#r\d+$/.test(key.SK)) {
        throw new Error(`[seed-all-orgs] refusing to write a version side-record SK: ${key.SK}`);
      }
      await doc.send(
        new PutCommand({
          TableName: TABLE,
          Item: { ...key, ...record },
        }),
      );
    }
    for (const wf of workflows) {
      await doc.send(
        new PutCommand({
          TableName: TABLE,
          Item: { ...workflowKey(wf.scope, wf.name), ...wf },
        }),
      );
    }
    orgRecordCount += records.length;
    workflowRecordCount += workflows.length;
    console.log(
      `  seeded org#${org}: ${records.length} skill records + ${workflows.length} workflow records`,
    );
  }

  // The user-granted built-ins (compound-engineering, gstack, playwright-cli) at
  // user#GRANT_OWNER scope — identical regardless of org, so write them once.
  const granted = buildSeedSkills(TEMPLATE_ORG, files, manifest, GRANT_OWNER).filter(
    (r) => r.scope.tier === 'user',
  );
  if (process.env.SEED_DRY_RUN) {
    console.log(
      `[seed-all-orgs] DRY RUN — would write ${orgRecordCount} org-scoped skill records + ` +
        `${workflowRecordCount} workflow records across ${orgs.length} org(s) + ` +
        `${granted.length} user-granted records (user#${GRANT_OWNER}). Nothing written.`,
    );
    return;
  }
  for (const record of granted) {
    await doc.send(
      new PutCommand({
        TableName: TABLE,
        Item: { ...skillKey(record.scope, record.name), ...record },
      }),
    );
  }

  console.log(
    `[seed-all-orgs] done: ${orgRecordCount} org-scoped skill records + ${workflowRecordCount} ` +
      `workflow records across ${orgs.length} org(s) + ${granted.length} user-granted records ` +
      `(user#${GRANT_OWNER}) into ${TABLE}.`,
  );
}

main().catch((err) => {
  console.error('[seed-all-orgs] failed:', err);
  process.exit(1);
});
