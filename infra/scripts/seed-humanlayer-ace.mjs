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
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import path from 'node:path';
import { PutCommand } from '@aws-sdk/lib-dynamodb';
import { repoRoot, TABLE, makeDocClient, importBackendDist } from './lib/common.mjs';

const skillsDir = path.join(repoRoot, 'catalog', 'skills');
const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

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

if (!existsSync(path.join(backendDist, 'seed', 'skills.js'))) {
  console.error(
    `[seed-hl-ace] missing ${backendDist}/seed/skills.js — run \`npm run build -w @harness/backend\` first.`,
  );
  process.exit(1);
}
const { buildSeedSkills } = await importBackendDist('seed', 'skills.js');
const { skillKey } = await importBackendDist('db', 'keys.js');

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
