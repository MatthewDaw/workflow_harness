import { orgScope, userScope, skillSchema, type Skill } from '@harness/shared';
import type { Repo } from '../db/repo.js';

/**
 * Org-scope seed for the skills that ship bundled with Command HQ + claude+
 * (U22). A fresh deploy starts with an empty registry, so the Skills tab shows
 * nothing until something registers skills. This seed writes the repo's
 * `catalog/skills/` set into HQ so every user in the org sees the product
 * starter bundle out of the box — no device connected, no sync run.
 *
 * NOT every repo `catalog/skills/<name>` belongs in the org-wide default. Only
 * skills that are MEMBERS of a seeded bundle (the `command-hq-starter` members)
 * — plus the bundle record itself — seed at ORG scope (what every new account
 * sees). The remaining `catalog/skills/<name>` folders ship in the repo but are
 * NOT part of the org default; they would otherwise leak into every brand-new
 * account's catalog. We keep their SKILL.md in the repo and seed them at the
 * NARROWER user scope of a designated grant owner, so accounts granted that
 * owner can see them while a fresh org cannot. The grant owner is configurable
 * (see `DEFAULT_GRANT_OWNER` / the call sites' `SEED_GRANT_OWNER` env var).
 *
 * The builder is pure (no I/O) so it is trivially unit-tested; `seedSkills`
 * upserts the records through the existing `Repo.putSkill`, which makes the seed
 * idempotent (re-running leaves exactly one record per skill).
 */

/** A skill definition parsed from a repo `catalog/skills/<name>/SKILL.md`. */
export interface SeedSkillFile {
  name: string;
  description: string;
  /** Full SKILL.md body, so a daemon can materialize it locally on sync. */
  body: string;
  /**
   * WHOLE-DIRECTORY contents (U-Skill-Store): every file under the skill dir
   * keyed by its path RELATIVE to that dir (e.g. `SKILL.md`, `scripts/run.sh`),
   * so a skill ships SKILL.md PLUS sibling scripts/resources. Optional: when
   * absent, the record carries only `body` (legacy body-only back-compat).
   */
  files?: Record<string, string>;
}

/**
 * The product starter bundle's name. Kept as a named export because callers and
 * tests reference it, but it is no longer special-cased: it is just one entry in
 * the bundle manifest like any other.
 */
export const STARTER_BUNDLE_NAME = 'command-hq-starter';

/**
 * Default owner of the user-scoped grant for non-bundle skills. A fresh org
 * never sees these (they are not at org scope); only an account whose userId is
 * this owner does. The call sites override it from `SEED_GRANT_OWNER` so a deploy
 * can grant them to a real account; `'system'` is the inert default (matches the
 * seed's `createdBy`), so a vanilla seed parks them where no human account lands.
 */
export const DEFAULT_GRANT_OWNER = 'system';

/** One bundle's declaration in the manifest: a human description + its members. */
export interface BundleSpec {
  description: string;
  members: string[];
}

/**
 * The bundle manifest — the **single source of truth** for how seeded skills are
 * organized into bundles. Maps `<bundleName>` to its spec. A skill that no bundle
 * lists is seeded as a **standalone** catalog skill, so creating a new skill does
 * not bundle it with anything unless it is explicitly added here. Lives in the
 * repo at `catalog/skills/bundles.json`; the seed loads it and passes it in.
 */
export type BundleManifest = Record<string, BundleSpec>;

/**
 * Build the seed `Skill[]`: one `kind:'skill'` record per file, plus one
 * `kind:'bundle'` record per manifest entry. A bundle's `members` are the
 * manifest's declared members, intersected with the skills that actually exist
 * (so a stale manifest reference is dropped, not stored as a dangling member).
 *
 * Scope is the gate on what a brand-new account sees. A skill is at ORG scope
 * (the org-wide default) only if it is a MEMBER of a seeded bundle — i.e. it is
 * named by some bundle in the manifest. Every other file (`gstack`,
 * `playwright-cli` — folders that no bundle lists) is seeded at the
 * narrower USER scope of `grantOwner`, so it stays registered (its SKILL.md
 * stays in the repo) but is invisible to a fresh org. Bundle records themselves
 * are always org-scoped. Everything is `source:'built-in'`. Parsing each through
 * `skillSchema` applies defaults and guards the shape.
 */
export function buildSeedSkills(
  org: string,
  files: SeedSkillFile[],
  manifest: BundleManifest = {},
  grantOwner: string = DEFAULT_GRANT_OWNER,
): Skill[] {
  const orgRef = orgScope(org);
  const grantRef = userScope(grantOwner);
  const createdBy = { userId: 'system', name: 'system' } as const;
  const known = new Set(files.map((f) => f.name));

  // The org-wide default = the union of every seeded bundle's (existing) members.
  // A skill in this set seeds at org scope; anything else is granted user-narrow.
  const orgDefault = new Set<string>();
  for (const spec of Object.values(manifest)) {
    for (const m of spec.members) if (known.has(m)) orgDefault.add(m);
  }

  // Seeded records are the BASE variant of their name (rev 1, empty repo/user).
  // `variantId === baseName === name` for the base variant; `version: 1`.
  const skills = files.map((f) =>
    skillSchema.parse({
      name: f.name,
      scope: orgDefault.has(f.name) ? orgRef : grantRef,
      kind: 'skill',
      description: f.description,
      source: 'built-in',
      body: f.body,
      // Whole-directory storage: ship every file under the skill dir. Falls back
      // to a SKILL.md-only map when a caller passes only `body`.
      files: f.files ?? { 'SKILL.md': f.body },
      createdBy,
      baseName: f.name,
      variantId: f.name,
      version: 1,
    }),
  );

  const bundles = Object.entries(manifest).map(([name, spec]) =>
    skillSchema.parse({
      name,
      scope: orgRef,
      kind: 'bundle',
      description: spec.description,
      source: 'built-in',
      members: spec.members.filter((m) => known.has(m)),
      createdBy,
      baseName: name,
      variantId: name,
      version: 1,
    }),
  );

  return [...skills, ...bundles];
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
  manifest: BundleManifest = {},
  grantOwner: string = DEFAULT_GRANT_OWNER,
): Promise<Skill[]> {
  const records = buildSeedSkills(org, files, manifest, grantOwner);
  for (const record of records) {
    await repo.putSkill(record);
  }
  return records;
}
