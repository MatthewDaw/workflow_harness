import { orgScope, skillSchema, type Skill } from '@harness/shared';
import type { Repo } from '../db/repo.js';

/**
 * Org-scope seed for the skills that ship bundled with Command HQ + claude+
 * (U22). A fresh deploy starts with an empty registry, so the Skills tab shows
 * nothing until something registers skills. This seed writes the repo's
 * `.claude/skills/` set into HQ at **org scope, grouped as one bundle**, so every
 * user in the org sees them out of the box — no device connected, no sync run.
 *
 * The builder is pure (no I/O) so it is trivially unit-tested; `seedSkills`
 * upserts the records through the existing `Repo.putSkill`, which makes the seed
 * idempotent (re-running leaves exactly one record per skill).
 */

/** A skill definition parsed from a repo `.claude/skills/<name>/SKILL.md`. */
export interface SeedSkillFile {
  name: string;
  description: string;
  /** Full SKILL.md body, so a daemon can materialize it locally on sync. */
  body: string;
}

/** The single named bundle the seeded skills are grouped under in the Skills tab. */
export const STARTER_BUNDLE_NAME = 'command-hq-starter';

/**
 * Build the org-scope `Skill[]` to seed: one `kind:'skill'` record per file plus
 * one `kind:'bundle'` record listing them as members. Everything is `source:
 * 'built-in'` to distinguish product-bundled skills from a user's own. Parsing
 * each through `skillSchema` applies defaults and guards the shape.
 */
export function buildSeedSkills(org: string, files: SeedSkillFile[]): Skill[] {
  const scope = orgScope(org);
  const createdBy = { userId: 'system', name: 'system' } as const;
  const skills = files.map((f) =>
    skillSchema.parse({
      name: f.name,
      scope,
      kind: 'skill',
      description: f.description,
      source: 'built-in',
      body: f.body,
      createdBy,
    }),
  );
  const bundle = skillSchema.parse({
    name: STARTER_BUNDLE_NAME,
    scope,
    kind: 'bundle',
    description: 'Skills bundled with Command HQ + claude+ (forge, weekly, progress).',
    source: 'built-in',
    members: files.map((f) => f.name),
    createdBy,
  });
  return [...skills, bundle];
}

/**
 * Upsert the seeded skills + bundle into HQ at org scope. Idempotent: `putSkill`
 * overwrites by key, so a second run converges to the same set. Returns the
 * records written.
 */
export async function seedSkills(
  repo: Repo,
  org: string,
  files: SeedSkillFile[],
): Promise<Skill[]> {
  const records = buildSeedSkills(org, files);
  for (const record of records) {
    await repo.putSkill(record);
  }
  return records;
}
