import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { orgScope, skillSchema, type Skill } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  buildSeedSkills,
  STARTER_BUNDLE_NAME,
  type BundleManifest,
  type SeedSkillFile,
} from './skills.js';

/**
 * Pre-load a brand-new org's catalog with the product **command-hq-starter**
 * bundle + its member skills, so the Skills tab is populated the moment the org
 * is created (rather than empty until a device syncs). Called by `POST /orgs`.
 *
 * Two sources, tried in order so it works everywhere:
 *  1. CLONE from a template org's catalog already in DynamoDB (the deploy seeds
 *     `org#acme` via infra/scripts/seed-skills.mjs). This is the deployed/Lambda
 *     path — the Lambda has no repo files — and is deterministic for tests.
 *  2. DISK fallback: read the repo's `.claude/skills/` set (local dev, where the
 *     dev server exports its path as `HQ_REPO_SKILLS_DIR`). Used when no template
 *     org is seeded yet.
 *
 * Only the ORG-scoped records (the starter bundle + its members) are copied — the
 * user-granted built-ins (gstack, playwright-cli) are NOT
 * part of a new org's default. Idempotent: `putSkill` upserts by key.
 */

/** The org whose catalog we clone the starter set from when present. */
const TEMPLATE_ORG = process.env.STARTER_TEMPLATE_ORG ?? 'acme';

/** Build the org-scoped starter records by cloning the template org's catalog. */
async function cloneStarterRecords(repo: Repo, templateOrg: string, org: string): Promise<Skill[]> {
  const catalog = await repo.listSkills(templateOrg);
  const bundle = catalog.find((s) => s.kind === 'bundle' && s.name === STARTER_BUNDLE_NAME);
  if (!bundle) return [];
  const members = new Set(bundle.members);
  const orgRef = orgScope(org);
  // The bundle record itself + every member skill, re-scoped to the new org.
  // Parse through skillSchema so the source items' table attributes (PK/SK/GSI,
  // carried on the raw DynamoDB rows) are stripped — otherwise putSkill's
  // `{...key, ...record}` spread would let the TEMPLATE's PK/SK clobber the new
  // org's key and the clone would land back in the template partition.
  return catalog
    .filter((s) => s.name === STARTER_BUNDLE_NAME || members.has(s.name))
    .map((s) => skillSchema.parse({ ...s, scope: orgRef }));
}

/** Repo `.claude/skills` dir, exported by the dev server as `HQ_REPO_SKILLS_DIR`. */
function repoSkillsDir(): string | undefined {
  const dir = process.env.HQ_REPO_SKILLS_DIR;
  return dir && existsSync(dir) ? dir : undefined;
}

/** Minimal SKILL.md front-matter parse (name + folded description), matching the
 * deploy seeder. Only used on the disk-fallback path. */
function parseFrontmatter(md: string): { name?: string; description: string } {
  const lines = md.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') return { description: '' };
  let name: string | undefined;
  const desc: string[] = [];
  let inDesc = false;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i] ?? '';
    if (line.trim() === '---') break;
    const top = /^([A-Za-z0-9_-]+):\s?(.*)$/.exec(line);
    if (top && !line.startsWith(' ')) {
      inDesc = false;
      const [, key, value] = top;
      if (key === 'name') name = value!.trim();
      else if (key === 'description') {
        inDesc = true;
        const v = value!.trim();
        if (v && v !== '>-' && v !== '>' && v !== '|' && v !== '|-') desc.push(v);
      }
      continue;
    }
    if (inDesc && line.trim()) desc.push(line.trim());
  }
  return { name, description: desc.join(' ').trim() };
}

/** Build the org-scoped starter records from the repo's `.claude/skills` on disk. */
function diskStarterRecords(dir: string, org: string): Skill[] {
  const files: SeedSkillFile[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const md = path.join(dir, entry.name, 'SKILL.md');
    if (!existsSync(md)) continue;
    const body = readFileSync(md, 'utf8');
    const { name, description } = parseFrontmatter(body);
    files.push({ name: name ?? entry.name, description, body });
  }
  let manifest: BundleManifest = {};
  const manifestPath = path.join(dir, 'bundles.json');
  if (existsSync(manifestPath)) {
    try {
      manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as BundleManifest;
    } catch {
      manifest = {};
    }
  }
  // Keep ONLY the org-scoped records (starter bundle + members); the user-granted
  // built-ins are not part of a new org's default.
  return buildSeedSkills(org, files, manifest).filter((s) => s.scope.tier === 'org');
}

/**
 * Seed `org`'s catalog with the starter bundle + members. Returns the number of
 * records written (0 when no source is available — callers treat seeding as
 * best-effort so org creation never fails on it).
 */
export async function seedStarterForOrg(
  repo: Repo,
  org: string,
  opts: { templateOrg?: string } = {},
): Promise<number> {
  const templateOrg = opts.templateOrg ?? TEMPLATE_ORG;
  // The template org already carries the canonical seed; never clone onto itself.
  let records = org === templateOrg ? [] : await cloneStarterRecords(repo, templateOrg, org);
  if (records.length === 0) {
    const dir = repoSkillsDir();
    if (dir) records = diskStarterRecords(dir, org);
  }
  for (const record of records) await repo.putSkill(record);
  return records.length;
}
