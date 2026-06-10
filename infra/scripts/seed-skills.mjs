// Seeds the skills bundled with Command HQ + claude+ into the deployed `harness`
// DynamoDB table at org scope, grouped as one bundle (plan U22). A fresh deploy
// starts with an empty registry, so without this the Skills tab is empty until a
// device connects and syncs. Running this makes the "command-hq-starter" bundle
// visible to every user in the org out of the box.
//
// Source of truth is the repo's `catalog/skills/<name>/SKILL.md`. The record
// shape + keys are reused from the compiled backend (packages/backend/dist) so
// the seed can never drift from how the REST layer reads skills — run
// `npm run build -w @harness/backend` first (the deploy workflow already does).
//
// Usage:
//   node infra/scripts/seed-skills.mjs            # org=acme, table=harness, region=us-east-1
//   SEED_ORG=acme HARNESS_TABLE=harness AWS_REGION=us-east-1 node infra/scripts/seed-skills.mjs
//
// Idempotent: each skill is upserted by key, so re-running converges.
import path from 'node:path';
import { PutCommand } from '@aws-sdk/lib-dynamodb';
import {
  repoRoot,
  TABLE,
  makeDocClient,
  importBackendDist,
  requireBackendDist,
} from './lib/common.mjs';
import { readSkillFiles, readBundleManifest } from './lib/catalog.mjs';

const skillsDir = path.join(repoRoot, 'catalog', 'skills');
const bundlesManifest = path.join(skillsDir, 'bundles.json');

const ORG = process.env.SEED_ORG ?? 'acme';
// Owner of the user-scoped grant for non-bundle skills (compound-engineering,
// gstack, playwright-cli). They are NOT in the org default — they seed at this
// user's scope so a granted account can see them while a fresh org cannot.
// Defaults to the inert 'system' owner (no human account lands there) unless a
// deploy points it at a real account. buildSeedSkills applies the same default.
const GRANT_OWNER = process.env.SEED_GRANT_OWNER ?? 'system';

requireBackendDist('seed-skills', 'seed/skills.js');

// Reuse the canonical record builder + key scheme from the built backend.
const { buildSeedSkills } = await importBackendDist('seed', 'skills.js');
const { skillKey } = await importBackendDist('db', 'keys.js');

async function main() {
  const files = readSkillFiles(skillsDir, 'seed-skills');
  if (files.length === 0) {
    console.error('[seed-skills] found no SKILL.md files to seed');
    process.exit(1);
  }
  const manifest = readBundleManifest(bundlesManifest, 'seed-skills');
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

  const doc = makeDocClient();
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
