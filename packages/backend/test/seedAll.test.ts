import { describe, expect, it } from 'vitest';
import { buildSeedAll, seedAll } from '../src/seed/all.js';
import { buildSeedMcpServers, seedMcpServers } from '../src/seed/mcpServers.js';
import { memRepoHarness } from './helpers/memtable.js';

/**
 * U-Ver-Seed: the GENERAL org-wide seed now covers all THREE pillars — skills,
 * agents, AND MCP servers — version-aware (base variant rev 1) and idempotent.
 */

const { repo } = memRepoHarness();

const ORG = 'acme';

const SKILLS = [{ name: 'reconcile', description: 'd', body: '# reconcile' }];
const MANIFEST = { 'starter-pack': { description: 'b', members: ['reconcile'] } };
const AGENTS = [
  { name: 'planner', description: 'plans', tools: ['Read'], model: 'sonnet', prompt: 'p' },
];
const MCP = [{ name: 'filesystem', transport: 'stdio' as const, command: 'npx' }];

describe('buildSeedMcpServers', () => {
  it('stamps each server as the base variant (rev 1) with defaults + system author', () => {
    const [s] = buildSeedMcpServers(ORG, MCP);
    expect(s!.name).toBe('filesystem');
    expect(s!.scope).toEqual({ tier: 'org', id: ORG });
    expect(s!.transport).toBe('stdio');
    expect((s as { args: string[] }).args).toEqual([]);
    expect(s!.baseName).toBe('filesystem');
    expect(s!.variantId).toBe('filesystem');
    expect(s!.version).toBe(1);
    expect(s!.createdBy).toEqual({ userId: 'system', name: 'system' });
  });
});

describe('seedMcpServers (idempotent)', () => {
  it('running twice leaves one record per server in the org catalog', async () => {
    await seedMcpServers(repo, ORG, MCP);
    await seedMcpServers(repo, ORG, MCP);
    const stored = await repo.listMcpServers(ORG);
    expect(stored.map((s) => s.name)).toEqual(['filesystem']);
  });
});

describe('buildSeedAll', () => {
  it('builds all three pillars version-aware (base variant rev 1)', () => {
    const all = buildSeedAll(ORG, {
      skills: SKILLS,
      manifest: MANIFEST,
      agents: AGENTS,
      mcpServers: MCP,
    });
    expect(all.skills.find((s) => s.name === 'reconcile')?.version).toBe(1);
    expect(all.agents[0]?.version).toBe(1);
    expect(all.mcpServers[0]?.version).toBe(1);
    // The skill bundle record is also present.
    expect(all.skills.some((s) => s.kind === 'bundle' && s.name === 'starter-pack')).toBe(true);
  });

  it('omits a pillar that was not provided', () => {
    const all = buildSeedAll(ORG, { agents: AGENTS });
    expect(all.skills).toEqual([]);
    expect(all.mcpServers).toEqual([]);
    expect(all.agents).toHaveLength(1);
  });
});

describe('seedAll (org-wide, all pillars, idempotent)', () => {
  it('populates skills + agents + mcp and re-running does not duplicate or reset', async () => {
    await seedAll(repo, ORG, {
      skills: SKILLS,
      manifest: MANIFEST,
      agents: AGENTS,
      mcpServers: MCP,
    });
    await seedAll(repo, ORG, {
      skills: SKILLS,
      manifest: MANIFEST,
      agents: AGENTS,
      mcpServers: MCP,
    });

    const skills = await repo.listSkills(ORG);
    const agents = await repo.listAgents(ORG);
    const mcp = await repo.listMcpServers(ORG);
    expect(skills.map((s) => s.name).sort()).toEqual(['reconcile', 'starter-pack']);
    expect(agents.map((a) => a.name)).toEqual(['planner']);
    expect(mcp.map((s) => s.name)).toEqual(['filesystem']);
  });
});
