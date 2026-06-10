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
// Idempotent: each record is upserted by key, so re-running converges. No write
// throttling needed (U4): the stream consumer hash-skips unchanged skills (U3)
// and drains with bounded embed concurrency, so a re-seed burst can't storm
// Bedrock; on-demand `harness` absorbs the writes themselves fine.
import path from 'node:path';
import { PutCommand } from '@aws-sdk/lib-dynamodb';
import {
  repoRoot,
  TABLE,
  REGION,
  makeDocClient,
  importBackendDist,
  requireBackendDist,
  listOrgNames,
} from './lib/common.mjs';
import { readSkillFiles, readBundleManifest } from './lib/catalog.mjs';

const skillsDir = path.join(repoRoot, 'catalog', 'skills');
const bundlesManifest = path.join(skillsDir, 'bundles.json');

// The template org new-org creation clones the starter bundle from. We always
// (re)seed it so the clone-on-create path has a canonical source even if no real
// org named this exists yet. Keep in sync with starter.ts's STARTER_TEMPLATE_ORG.
const TEMPLATE_ORG = process.env.STARTER_TEMPLATE_ORG ?? 'acme';
// Owner of the user-scoped grant for the non-bundle skills (compound-engineering,
// gstack, playwright-cli). They are NOT part of any org default; they seed once at
// this user's scope so a granted account can see them while a fresh org cannot.
const GRANT_OWNER = process.env.SEED_GRANT_OWNER ?? 'system';

requireBackendDist('seed-all-orgs', 'seed/skills.js', 'seed/workflows.js');

// Reuse the canonical record builder + key scheme from the built backend so this
// seed produces byte-identical records to what the REST layer reads/writes.
const { buildSeedSkills, assertBaseVariantOnly } = await importBackendDist('seed', 'skills.js');
// Workflows are structured data (no markdown tree), so their source of truth is
// the compiled STARTER_WORKFLOWS — seeded org-wide here so EVERY org gets the
// starter DAG (not a single SEED_ORG, per the all-orgs convention).
const { buildSeedWorkflows, STARTER_WORKFLOWS } = await importBackendDist(
  'seed',
  'workflows.js',
);
const { skillKey, workflowKey } = await importBackendDist('db', 'keys.js');

async function main() {
  const files = readSkillFiles(skillsDir, 'seed-all-orgs');
  if (files.length === 0) {
    console.error('[seed-all-orgs] found no SKILL.md files to seed');
    process.exit(1);
  }
  const manifest = readBundleManifest(bundlesManifest, 'seed-all-orgs');

  const doc = makeDocClient();

  // U4 — confirm the seed targets the LIVE table. The default is `harness`
  // (us-east-1, acct 066756666605); a typo'd HARNESS_TABLE would silently seed
  // the wrong (or a non-existent) table. Surface the resolved target up front so
  // a re-seed is never misdirected, and warn if it is not the canonical live name.
  console.log(`[seed-all-orgs] target table='${TABLE}' region='${REGION}'`);
  if (TABLE !== 'harness') {
    console.warn(
      `[seed-all-orgs] WARNING: HARNESS_TABLE='${TABLE}' is not the live 'harness' table — ` +
        `seeding a non-canonical table. Set HARNESS_TABLE=harness (or unset it) to target live.`,
    );
  }

  const discovered = await listOrgNames(doc, TABLE);
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
    // U19 — re-assert the base-variant-only contract on the post-filter records
    // so this script can NEVER write a fork (buildSeedSkills already asserts,
    // but the filter could in principle drop the failing record).
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
      // Defense in depth: the seed writes the live BASE-variant record only —
      // never a `#TRUE` pointer or `#r<N>` revision row — so a promotion survives
      // re-seed even if the key scheme changes under us.
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
