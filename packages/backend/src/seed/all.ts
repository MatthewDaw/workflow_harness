import type { Agent, McpServer, Skill } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  buildSeedSkills,
  seedSkills,
  type BundleManifest,
  type SeedSkillFile,
} from './skills.js';
import { buildSeedAgents, seedAgents, type SeedAgentFile } from './agents.js';
import { buildSeedMcpServers, seedMcpServers, type SeedMcpServerFile } from './mcpServers.js';

/**
 * The GENERAL org-wide seed (U-Ver-Seed). The original seed only populated
 * SKILLS + bundles org-wide; this aggregates all THREE catalog pillars — skills,
 * agents, AND MCP servers — into one version-aware, idempotent pass so a fresh
 * org (or a backfill across every org) ends up with the full bundled catalog.
 *
 * Every builder stamps its records as the BASE variant of their name (rev 1,
 * empty repo/user), so:
 *  - a later edit FORKS/ADVANCES the variant cleanly (it never starts at rev 1
 *    again for the base), and
 *  - a RE-RUN converges by key (no reset): re-seeding writes the same base-variant
 *    rev-1 record, and existing forks + the TRUE pointer are untouched (the seed
 *    writes the live `*Key` records, not the per-variant revision history).
 *
 * Pure (`buildSeedAll`) for unit tests; `seedAll` upserts via the existing
 * idempotent per-pillar seed writers.
 */

export interface SeedAllInput {
  skills?: SeedSkillFile[];
  manifest?: BundleManifest;
  grantOwner?: string;
  agents?: SeedAgentFile[];
  mcpServers?: SeedMcpServerFile[];
}

export interface SeedAllRecords {
  skills: Skill[];
  agents: Agent[];
  mcpServers: McpServer[];
}

/** Build (no I/O) every pillar's org-wide seed records for `org`, version-aware. */
export function buildSeedAll(org: string, input: SeedAllInput): SeedAllRecords {
  return {
    skills: input.skills
      ? buildSeedSkills(org, input.skills, input.manifest ?? {}, input.grantOwner)
      : [],
    agents: input.agents ? buildSeedAgents(org, input.agents) : [],
    mcpServers: input.mcpServers ? buildSeedMcpServers(org, input.mcpServers) : [],
  };
}

/**
 * Upsert every pillar's seed into `org`'s catalog. Idempotent across all three
 * (each per-pillar writer overwrites by key). Returns the records written.
 */
export async function seedAll(
  repo: Repo,
  org: string,
  input: SeedAllInput,
): Promise<SeedAllRecords> {
  const skills = input.skills
    ? await seedSkills(repo, org, input.skills, input.manifest ?? {}, input.grantOwner)
    : [];
  const agents = input.agents ? await seedAgents(repo, org, input.agents) : [];
  const mcpServers = input.mcpServers ? await seedMcpServers(repo, org, input.mcpServers) : [];
  return { skills, agents, mcpServers };
}
