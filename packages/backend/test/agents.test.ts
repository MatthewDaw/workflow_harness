import { describe, expect, it } from 'vitest';
import { agentSchema, type Agent } from '@harness/shared';
import {
  addMember,
  createAgent,
  deleteAgent,
  dissolveAgentBundle,
  getAgent,
  handler as agentsHandler,
  promoteAgent,
  removeMember,
  resolveAgents,
} from '../src/rest/agents.js';
import { flattenBundle } from '../src/rest/bundles.js';
import { memRepoHarness } from './helpers/memtable.js';
import { adminEvent, bodyOf, httpEvent } from './helpers/httpevent.js';
import {
  MATT,
  ORG,
  SCOPE,
  makeAgent as agent,
  makeAgentBundle as bundle,
} from './helpers/factories.js';
import { describeOrgCatalogContract } from './helpers/catalog-contract.js';

/**
 * Org-catalog agents (3-tier scope retired). The generic REST contract (list,
 * admin-gated writes, createdBy, versioning + promote, built-ins fork-only)
 * runs via describeOrgCatalogContract; this file keeps the agent-specific
 * behavior: schema defaults, bundles, dissolve, the bundle-overwrite guard,
 * and the retired scope route (410).
 */

const { repo } = memRepoHarness();
const deps = { repo };

describeOrgCatalogContract<Agent>({
  noun: 'agent',
  plural: 'agents',
  entity: 'AGENT',
  repo,
  sampleNames: ['builder', 'analyst'],
  builtinName: 'forge',
  make: (name) => agent(name),
  makeBuiltin: (name) => ({ ...agent(name), createdBy: { userId: 'system', name: 'system' } }),
  mutate: (a) => ({ ...a, model: 'sonnet' }),
  assertMutated: (stored) => expect(stored?.model).toBe('sonnet'),
  create: (e) => createAgent(e, deps),
  remove: (e) => deleteAgent(e, deps),
  get: (e) => getAgent(e, deps),
  list: (e) => resolveAgents(e, deps),
  promote: (e) => promoteAgent(e, deps),
  put: (a) => repo.putAgent(a),
  read: (name) => repo.getAgent(SCOPE, name),
  nonAdminPromote: 'allowed',
});

describe('agentSchema description default', () => {
  it("fills description '' when omitted and preserves a provided description", () => {
    // The agent() helper omits description; it must still validate via default ''.
    const omitted = agentSchema.parse(agent('builder'));
    expect(omitted.description).toBe('');

    const provided = agentSchema.parse({
      ...agent('builder'),
      description: 'use when refactoring legacy code',
    });
    expect(provided.description).toBe('use when refactoring legacy code');
  });
});

describe('GET /agents annotates a bundle with resolvedMembers', () => {
  it('flattens nested agent bundles transitively', async () => {
    await repo.putAgent(agent('a'));
    await repo.putAgent(agent('b'));
    await repo.putAgent(bundle('inner', ['a', 'b']));
    await repo.putAgent(bundle('outer', ['inner']));
    const res = await resolveAgents(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { agents } = bodyOf<{ agents: Agent[] }>(res);
    const outer = agents.find((a) => a.name === 'outer');
    expect(outer?.resolvedMembers?.sort()).toEqual(['a', 'b']);
  });
});

describe('nested agent bundles (pure flatten)', () => {
  it('flattens transitively through a nested bundle', () => {
    const inner = bundle('inner', ['a', 'b']);
    const outer = bundle('outer', ['inner', 'c']);
    const byName = new Map<string, Agent>([
      ['inner', inner],
      ['outer', outer],
      ['a', agent('a')],
      ['b', agent('b')],
      ['c', agent('c')],
    ]);
    expect(flattenBundle(outer, byName).sort()).toEqual(['a', 'b', 'c']);
  });
});

describe('agent bundle membership (admin)', () => {
  it('adds a standalone agent into a bundle, leaving it standalone', async () => {
    await repo.putAgent(agent('reviewer'));
    await repo.putAgent(bundle('pack', []));
    const res = await addMember(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'pack' },
        body: { member: 'reviewer' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getAgent(SCOPE, 'pack'))?.members).toEqual(['reviewer']);
    expect(await repo.getAgent(SCOPE, 'reviewer')).toBeDefined();
  });

  it('ejects a member, leaving it standalone', async () => {
    await repo.putAgent(agent('reviewer'));
    await repo.putAgent(bundle('pack', ['reviewer']));
    const res = await removeMember(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'pack', member: 'reviewer' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getAgent(SCOPE, 'pack'))?.members).toEqual([]);
    expect(await repo.getAgent(SCOPE, 'reviewer')).toBeDefined();
  });

  it('400s adding a member to a non-bundle agent', async () => {
    await repo.putAgent(agent('builder'));
    const res = await addMember(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'builder' },
        body: { member: 'x' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });
});

describe('dissolve agent bundle', () => {
  it('removes the bundle but leaves members standalone', async () => {
    await repo.putAgent(agent('a'));
    await repo.putAgent(agent('b'));
    await repo.putAgent(bundle('pack', ['a', 'b']));
    const res = await dissolveAgentBundle(
      adminEvent({
        method: 'POST',
        userId: MATT,
        rawPath: '/agents/pack/dissolve',
        path: { name: 'pack' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ members: string[] }>(res).members.sort()).toEqual(['a', 'b']);
    expect(await repo.getAgent(SCOPE, 'pack')).toBeUndefined();
    expect(await repo.getAgent(SCOPE, 'a')).toBeDefined();
  });
});

describe('agent bundle-overwrite guard (agent push must not clobber a bundle)', () => {
  it('rejects a POST agent that collides with an existing bundle name (409), leaving members intact', async () => {
    await repo.putAgent(bundle('pack', ['a', 'b']));
    const res = await createAgent(
      adminEvent({ method: 'POST', userId: MATT, body: agent('pack') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
    const stored = await repo.getAgent(SCOPE, 'pack');
    expect(stored?.kind).toBe('bundle');
    expect(stored?.members).toEqual(['a', 'b']);
  });
});

describe('POST /agents/:name/scope (retired)', () => {
  it('responds 410 Gone', async () => {
    const res = await agentsHandler(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: ORG,
        admin: true,
        rawPath: '/agents/builder/scope',
        path: { name: 'builder' },
        body: { scope: { tier: 'org', id: ORG } },
      }),
    );
    expect(res).toMatchObject({ statusCode: 410 });
  });
});
