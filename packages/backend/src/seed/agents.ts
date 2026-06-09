import { orgScope, agentSchema, type Agent } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { BundleManifest } from './skills.js';

/**
 * Org-scope seed for the agents that ship bundled with Command HQ + claude+
 * (U3). Mirrors `seed/skills.ts`'s `buildSeedSkills`/`seedSkills`: a fresh
 * deploy starts with an empty agent catalog, so the Agents tab shows nothing
 * until something registers agents. This seed writes the repo's
 * `.claude/agents/<name>.md` set into HQ so an org sees the bundled subagents.
 *
 * The builder is pure (no I/O) so it is trivially unit-tested; `seedAgents`
 * upserts the records through the existing `Repo.putAgent`, which makes the seed
 * idempotent (re-running by key leaves exactly one record per agent).
 *
 * NOTE: this builder is org-only by construction (every record is at
 * `orgScope(org)`), but the SCOPED registration script that calls it
 * (`infra/scripts/seed-humanlayer-agents.mjs`) is keyed on a REQUIRED `SEED_ORG`
 * and is deliberately NOT wired into starter.ts / seed-all-orgs.mjs, so the set
 * lands in exactly one org's catalog and never propagates org-wide.
 */

/** An agent definition parsed from a repo `.claude/agents/<name>.md` file. */
export interface SeedAgentFile {
  name: string;
  /** Delegation trigger — renders to the subagent file's `description:`. */
  description: string;
  /** Allowed tools (already split/trimmed from the frontmatter `tools:` CSV). */
  tools: string[];
  /** Model id from the frontmatter `model:`. */
  model: string;
  /** Full Markdown body below the frontmatter — the agent's system prompt. */
  prompt: string;
}

/**
 * Build the seed `Agent[]`: one `kind:'agent'` org-scoped record per file, PLUS
 * one `kind:'bundle'` record per manifest entry (mirrors `buildSeedSkills`). Each
 * agent record carries the parsed `{name, description, tools, model, prompt}`, an
 * empty `skills: []`, and a `system` authorship stamp. A bundle's `members` are
 * the manifest's declared members intersected with the agents that actually exist
 * (so a stale manifest reference is dropped, not stored as a dangling member);
 * bundle records carry no model/prompt. Parsing each through `agentSchema` applies
 * defaults and guards the shape, so a record here is byte-compatible with what the
 * agents REST layer reads/writes.
 */
export function buildSeedAgents(
  org: string,
  files: SeedAgentFile[],
  manifest: BundleManifest = {},
): Agent[] {
  const scope = orgScope(org);
  const createdBy = { userId: 'system', name: 'system' } as const;
  const known = new Set(files.map((f) => f.name));

  // Seeded records are the BASE variant of their name (rev 1, empty repo/user):
  // variantId === baseName === name, version 1 — so a later edit forks/advances
  // cleanly and a re-seed never resets the catalog.
  const agents = files.map((f) =>
    agentSchema.parse({
      name: f.name,
      scope,
      kind: 'agent',
      model: f.model,
      prompt: f.prompt,
      description: f.description,
      tools: f.tools,
      skills: [],
      createdBy,
      baseName: f.name,
      variantId: f.name,
      version: 1,
    }),
  );

  const bundles = Object.entries(manifest).map(([name, spec]) =>
    agentSchema.parse({
      name,
      scope,
      kind: 'bundle',
      // A bundle is a grouping record — no model/prompt to run.
      model: '',
      members: spec.members.filter((m) => known.has(m)),
      description: spec.description,
      createdBy,
      baseName: name,
      variantId: name,
      version: 1,
    }),
  );

  return [...agents, ...bundles];
}

/**
 * Upsert the seeded agents + bundles into HQ at org scope. Idempotent: `putAgent`
 * overwrites by key, so a second run converges to the same set. Returns the
 * records written.
 */
export async function seedAgents(
  repo: Repo,
  org: string,
  files: SeedAgentFile[],
  manifest: BundleManifest = {},
): Promise<Agent[]> {
  const records = buildSeedAgents(org, files, manifest);
  for (const record of records) {
    await repo.putAgent(record);
  }
  return records;
}
