// Register the HumanLayer ACE skill set (the 27 imported command-derived skills)
// + a `humanlayer-ace` bundle into ONE org's catalog — by default NOT org-wide.
//
// Unlike seed-skills.mjs / seed-all-orgs.mjs, this does NOT read the shared
// `catalog/skills/bundles.json` (which feeds the acme template + every-org
// backfill and would leak the set org-wide). It builds an in-memory manifest for
// just these 27 skills and seeds them at org scope for the REQUIRED `SEED_ORG`,
// so the set lands in that org's catalog and nowhere else.
//
// Reuses the compiled backend's `buildSeedSkills` + `skillKey` so records are
// byte-identical to what the REST layer reads. Run `npm run build -w @harness/backend` first.
//
// Usage:
//   SEED_ORG=<org> SEED_DRY_RUN=1 node infra/scripts/seed-humanlayer-ace.mjs   # report only
//   SEED_ORG=<org> node infra/scripts/seed-humanlayer-ace.mjs                  # write to `harness`
import { readFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { PutCommand } from '@aws-sdk/lib-dynamodb';
import {
  repoRoot,
  TABLE,
  makeDocClient,
  importBackendDist,
  requireBackendDist,
} from './lib/common.mjs';
import { parseFrontmatter } from './lib/catalog.mjs';

const skillsDir = path.join(repoRoot, 'catalog', 'skills');

const ORG = process.env.SEED_ORG;

if (!ORG) {
  console.error('[seed-hl-ace] SEED_ORG is required (no default — refuses to guess the org).');
  process.exit(1);
}

// The 27 HumanLayer command-derived skills (the ACE workflow). Explicit list so
// this never accidentally sweeps in other skills under catalog/skills/.
const ACE_SKILLS = [
  'ci_commit',
  'ci_describe_pr',
  'commit',
  'create_handoff',
  'create_plan',
  'create_plan_generic',
  'create_plan_nt',
  'create_worktree',
  'debug',
  'describe_pr',
  'describe_pr_nt',
  'founder_mode',
  'implement_plan',
  'iterate_plan',
  'iterate_plan_nt',
  'linear',
  'local_review',
  'oneshot',
  'oneshot_plan',
  'ralph_impl',
  'ralph_plan',
  'ralph_research',
  'research_codebase',
  'research_codebase_generic',
  'research_codebase_nt',
  'resume_handoff',
  'validate_plan',
];

requireBackendDist('seed-hl-ace', 'seed/skills.js');
const { buildSeedSkills } = await importBackendDist('seed', 'skills.js');
const { skillKey } = await importBackendDist('db', 'keys.js');

const files = [];
for (const name of ACE_SKILLS) {
  const md = path.join(skillsDir, name, 'SKILL.md');
  if (!existsSync(md)) {
    console.error(`[seed-hl-ace] missing ${md} — did the conversion run?`);
    process.exit(1);
  }
  const body = readFileSync(md, 'utf8');
  const fm = parseFrontmatter(body);
  files.push({ name: fm.name ?? name, description: fm.description, body });
}

const manifest = {
  'humanlayer-ace': {
    description:
      "HumanLayer's Advanced Context Engineering workflow — research, plan, implement, validate, commit (imported from humanlayer CLI).",
    members: ACE_SKILLS,
  },
};

// buildSeedSkills: members of a seeded bundle => org scope; the bundle record => org scope.
const records = buildSeedSkills(ORG, files, manifest);

if (process.env.SEED_DRY_RUN) {
  for (const r of records) {
    const what = r.kind === 'bundle' ? `bundle members=[${r.members.length}]` : 'skill';
    console.log(`[dry-run] ${what} ${r.name} @ ${r.scope.tier}#${r.scope.id}`);
  }
  console.log(
    `[seed-hl-ace] DRY RUN — ${records.length} records (${ACE_SKILLS.length} skills + 1 bundle) targeting org#${ORG} in ${TABLE}. Nothing written.`,
  );
} else {
  const doc = makeDocClient();
  for (const record of records) {
    await doc.send(
      new PutCommand({
        TableName: TABLE,
        Item: { ...skillKey(record.scope, record.name), ...record },
      }),
    );
  }
  console.log(
    `[seed-hl-ace] wrote ${records.length} records (27 skills + humanlayer-ace bundle) to ${TABLE} at org#${ORG}.`,
  );
}
