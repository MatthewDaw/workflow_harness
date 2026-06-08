import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Agent } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { buildSeedAgents, seedAgents, type SeedAgentFile } from '../src/seed/agents.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * U3: org-scope seed for the subagents bundled with Command HQ + claude+. The
 * builder is pure; `seedAgents` is an idempotent upsert; after seeding, the
 * existing `listAgents` shows the agents to the org.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const ORG = 'acme';
// The 6 bundled subagents (frontmatter parsed into {name, description, tools,
// model, prompt}) — the same shape the scoped script extracts from
// `.claude/agents/*.md`.
const FILES: SeedAgentFile[] = [
  {
    name: 'codebase-locator',
    description: 'Locates files and components relevant to a feature or task.',
    tools: ['Grep', 'Glob', 'LS'],
    model: 'sonnet',
    prompt: 'You are a specialist at finding WHERE code lives in a codebase.',
  },
  {
    name: 'codebase-analyzer',
    description: 'Analyzes how specific code works in detail.',
    tools: ['Read', 'Grep', 'Glob', 'LS'],
    model: 'sonnet',
    prompt: 'You are a specialist at understanding HOW code works.',
  },
  {
    name: 'codebase-pattern-finder',
    description: 'Finds existing patterns and similar implementations to model after.',
    tools: ['Grep', 'Glob', 'Read', 'LS'],
    model: 'sonnet',
    prompt: 'You find similar implementations and usage examples.',
  },
  {
    name: 'thoughts-locator',
    description: 'Locates relevant documents in the thoughts directory.',
    tools: ['Grep', 'Glob', 'LS'],
    model: 'sonnet',
    prompt: 'You are a specialist at finding documents in the thoughts directory.',
  },
  {
    name: 'thoughts-analyzer',
    description: 'Extracts the relevant insight from a thoughts document.',
    tools: ['Read', 'Grep', 'Glob', 'LS'],
    model: 'sonnet',
    prompt: 'You deeply analyze a single thoughts document.',
  },
  {
    name: 'web-search-researcher',
    description: 'Researches a topic on the web and synthesizes findings.',
    tools: ['WebSearch', 'WebFetch', 'TodoWrite', 'Read', 'Grep', 'Glob', 'LS'],
    model: 'sonnet',
    prompt: 'You are a web research specialist.',
  },
];

describe('buildSeedAgents', () => {
  it('yields one org-scoped record per file with model/tools/description/prompt and skills:[]', () => {
    const records = buildSeedAgents(ORG, FILES);

    expect(records).toHaveLength(FILES.length);
    for (let i = 0; i < records.length; i++) {
      const r = records[i] as Agent;
      const f = FILES[i];
      expect(r.name).toBe(f.name);
      expect(r.scope).toEqual({ tier: 'org', id: ORG });
      expect(r.model).toBe(f.model);
      expect(r.tools).toEqual(f.tools);
      expect(r.description).toBe(f.description);
      expect(r.prompt).toBe(f.prompt);
      // Seeded agents carry no skills; authorship is the system stamp.
      expect(r.skills).toEqual([]);
      expect(r.createdBy).toEqual({ userId: 'system', name: 'system' });
      // Versioning: a seeded agent is the BASE variant of its name at rev 1.
      expect(r.baseName).toBe(f.name);
      expect(r.variantId).toBe(f.name);
      expect(r.version).toBe(1);
    }
  });

  it('defaults missing tools to []', () => {
    const [r] = buildSeedAgents(ORG, [
      { name: 'no-tools', description: 'd', tools: [], model: 'sonnet', prompt: 'p' },
    ]);
    expect(r.tools).toEqual([]);
  });

  it('preserves a provided description verbatim', () => {
    const desc = 'A very specific delegation trigger for WHEN to spawn this agent.';
    const [r] = buildSeedAgents(ORG, [
      { name: 'a', description: desc, tools: ['Read'], model: 'sonnet', prompt: 'p' },
    ]);
    expect(r.description).toBe(desc);
  });
});

describe('seedAgents', () => {
  it('is idempotent — running twice leaves one record per agent', async () => {
    // putAgent overwrites by agentKey, so a second run converges to the same set.
    await seedAgents(repo, ORG, FILES);
    await seedAgents(repo, ORG, FILES);

    const stored = await repo.listAgents(ORG);
    expect(stored).toHaveLength(FILES.length);
    const names = stored.map((a) => a.name).sort();
    expect(names).toEqual(FILES.map((f) => f.name).sort());
  });

  it('writes the seeded agents at org scope so listAgents shows them', async () => {
    await seedAgents(repo, ORG, FILES);

    const stored = await repo.listAgents(ORG);
    const locator = stored.find((a) => a.name === 'codebase-locator');
    expect(locator).toBeDefined();
    expect(locator?.scope).toEqual({ tier: 'org', id: ORG });
    expect(locator?.tools).toEqual(['Grep', 'Glob', 'LS']);
    expect(locator?.model).toBe('sonnet');
  });
});
