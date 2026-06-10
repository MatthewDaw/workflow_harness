import { describe, expect, it } from 'vitest';
import { orgScope, type Agent, type McpServer, type Project, type Skill } from '@harness/shared';
import {
  disableProjectAgent,
  disableProjectAgentBundle,
  disableProjectMcpServer,
  disableProjectSkill,
  enableProjectAgent,
  enableProjectAgentBundle,
  enableProjectMcpServer,
  enableProjectSkill,
  type ProjectsDeps,
} from '../src/rest/projects.js';
import { memRepoHarness } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * Project opt-in for the org catalog: enable/disable skills + agents on a
 * project. Idempotent skill add; enabling an agent unions its skills (bundles
 * flattened to leaves) into enabledSkills; disabling an agent never prunes
 * enabledSkills; the admin-or-owner gate; 404 for catalog names that do not exist.
 */

const { repo } = memRepoHarness();
const deps: ProjectsDeps = { repo };

const MATT = 'matt';
const ALICE = 'alice';
const ORG = 'acme';
const PROJ = 'weekly-compass';
const SCOPE = orgScope(ORG);

function project(id: string, owner: string): Project {
  return {
    id,
    name: id,
    repo: `gh/acme/${id}`,
    ownerUserId: owner,
    liveSessionCount: 0,
    enabledSkills: [],
    enabledAgents: [],
    enabledMcpServers: [],
  };
}
function skill(name: string): Skill {
  return {
    name,
    scope: SCOPE,
    kind: 'skill',
    description: '',
    source: 'local',
    members: [],
    body: '',
  };
}
function bundle(name: string, members: string[]): Skill {
  return {
    name,
    scope: SCOPE,
    kind: 'bundle',
    description: '',
    source: 'local',
    members,
    body: '',
  };
}
function agent(name: string, skills: string[]): Agent {
  return { name, scope: SCOPE, model: 'opus', prompt: '', skills, tools: [] };
}
function agentBundle(name: string, members: string[]): Agent {
  return { name, scope: SCOPE, kind: 'bundle', model: '', prompt: '', skills: [], tools: [], members };
}
function mcpServer(name: string): McpServer {
  return { name, scope: SCOPE, transport: 'stdio', command: 'node', args: [], env: {} };
}

function ownerEvent(opts: Parameters<typeof httpEvent>[0]) {
  return httpEvent({ userId: MATT, org: ORG, ...opts });
}

describe('POST /projects/:projectId/skills/:skillName', () => {
  it('idempotently enables a catalog skill on the project', async () => {
    await repo.putProject(project(PROJ, MATT));
    await repo.putSkill(skill('reconcile'));

    const ev = ownerEvent({ method: 'POST', path: { projectId: PROJ, skillName: 'reconcile' } });
    const first = await enableProjectSkill(ev, deps);
    expect(first).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ project: Project }>(first).project.enabledSkills).toEqual([
      'reconcile',
    ]);

    // Second add is a no-op (idempotent) — still a single entry.
    const second = await enableProjectSkill(ev, deps);
    expect(bodyOf<{ project: Project }>(second).project.enabledSkills).toEqual([
      'reconcile',
    ]);
  });

  it('404s a skill that is not in the org catalog', async () => {
    await repo.putProject(project(PROJ, MATT));
    const res = await enableProjectSkill(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, skillName: 'ghost' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('allows an org admin who does not own the project', async () => {
    await repo.putProject(project(PROJ, ALICE));
    await repo.putSkill(skill('reconcile'));
    const res = await enableProjectSkill(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: ORG,
        admin: true,
        path: { projectId: PROJ, skillName: 'reconcile' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
  });

  it('forbids a non-owner non-admin', async () => {
    await repo.putProject(project(PROJ, ALICE));
    await repo.putSkill(skill('reconcile'));
    const res = await enableProjectSkill(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, skillName: 'reconcile' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });
});

describe('DELETE /projects/:projectId/skills/:skillName', () => {
  it('removes a skill from enabledSkills', async () => {
    await repo.putProject({ ...project(PROJ, MATT), enabledSkills: ['reconcile', 'forecast'] });
    const res = await disableProjectSkill(
      ownerEvent({ method: 'DELETE', path: { projectId: PROJ, skillName: 'reconcile' } }),
      deps,
    );
    expect(bodyOf<{ project: Project }>(res).project.enabledSkills).toEqual([
      'forecast',
    ]);
  });
});

describe('POST /projects/:projectId/agents/:agentName (unions agent skills)', () => {
  it('enables the agent and unions its declared skills, flattening bundles to leaves', async () => {
    await repo.putProject(project(PROJ, MATT));
    await repo.putSkill(skill('a'));
    await repo.putSkill(skill('b'));
    await repo.putSkill(skill('c'));
    await repo.putSkill(bundle('pack', ['b', 'c']));
    // builder declares a leaf skill `a` and a bundle `pack` (-> b, c).
    await repo.putAgent(agent('builder', ['a', 'pack']));

    const res = await enableProjectAgent(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, agentName: 'builder' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const updated = bodyOf<{ project: Project }>(res).project;
    expect(updated.enabledAgents).toEqual(['builder']);
    expect(updated.enabledSkills.sort()).toEqual(['a', 'b', 'c']);
  });

  it('does not duplicate skills already enabled directly', async () => {
    await repo.putProject({ ...project(PROJ, MATT), enabledSkills: ['a'] });
    await repo.putSkill(skill('a'));
    await repo.putSkill(skill('b'));
    await repo.putAgent(agent('builder', ['a', 'b']));

    const res = await enableProjectAgent(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, agentName: 'builder' } }),
      deps,
    );
    const updated = bodyOf<{ project: Project }>(res).project;
    expect(updated.enabledSkills.sort()).toEqual(['a', 'b']);
  });

  it('404s an agent not in the org catalog', async () => {
    await repo.putProject(project(PROJ, MATT));
    const res = await enableProjectAgent(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, agentName: 'ghost' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('POST /projects/:projectId/mcp-servers/:name', () => {
  it('idempotently enables a catalog server on the project', async () => {
    await repo.putProject(project(PROJ, MATT));
    await repo.putMcpServer(mcpServer('filesystem'));

    const ev = ownerEvent({ method: 'POST', path: { projectId: PROJ, name: 'filesystem' } });
    const first = await enableProjectMcpServer(ev, deps);
    expect(first).toMatchObject({ statusCode: 200 });
    expect(
      bodyOf<{ project: Project }>(first).project.enabledMcpServers,
    ).toEqual(['filesystem']);

    // Second add is a no-op (idempotent) — still a single entry.
    const second = await enableProjectMcpServer(ev, deps);
    expect(
      bodyOf<{ project: Project }>(second).project.enabledMcpServers,
    ).toEqual(['filesystem']);
  });

  it('404s a server that is not in the org catalog', async () => {
    await repo.putProject(project(PROJ, MATT));
    const res = await enableProjectMcpServer(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, name: 'ghost' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('allows an org admin who does not own the project', async () => {
    await repo.putProject(project(PROJ, ALICE));
    await repo.putMcpServer(mcpServer('filesystem'));
    const res = await enableProjectMcpServer(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: ORG,
        admin: true,
        path: { projectId: PROJ, name: 'filesystem' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
  });

  it('forbids a non-owner non-admin', async () => {
    await repo.putProject(project(PROJ, ALICE));
    await repo.putMcpServer(mcpServer('filesystem'));
    const res = await enableProjectMcpServer(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, name: 'filesystem' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });
});

describe('DELETE /projects/:projectId/mcp-servers/:name', () => {
  it('removes a server and returns the hydrated project with enabledMcpServers', async () => {
    await repo.putProject({
      ...project(PROJ, MATT),
      enabledMcpServers: ['filesystem', 'weather'],
    });
    const res = await disableProjectMcpServer(
      ownerEvent({ method: 'DELETE', path: { projectId: PROJ, name: 'filesystem' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ project: Project }>(res).project.enabledMcpServers).toEqual(
      ['weather'],
    );
  });
});

describe('DELETE /projects/:projectId/agents/:agentName (does NOT prune skills)', () => {
  it('removes only the agent, leaving enabledSkills intact', async () => {
    await repo.putProject({
      ...project(PROJ, MATT),
      enabledAgents: ['builder'],
      enabledSkills: ['a', 'b'],
    });
    const res = await disableProjectAgent(
      ownerEvent({ method: 'DELETE', path: { projectId: PROJ, agentName: 'builder' } }),
      deps,
    );
    const updated = bodyOf<{ project: Project }>(res).project;
    expect(updated.enabledAgents).toEqual([]);
    // Skills brought by the agent are NOT pruned.
    expect(updated.enabledSkills.sort()).toEqual(['a', 'b']);
  });
});

describe('POST /projects/:projectId/agent-bundles/:bundleName (enable a whole agent bundle)', () => {
  it('records the bundle intent + unions its member agents (and their skills) into the project', async () => {
    await repo.putProject(project(PROJ, MATT));
    await repo.putSkill(skill('gh'));
    await repo.putSkill(skill('kit'));
    await repo.putAgent(agent('reviewer', ['gh']));
    await repo.putAgent(agent('builder', ['kit']));
    await repo.putAgent(agentBundle('squad', ['reviewer', 'builder']));

    const res = await enableProjectAgentBundle(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, bundleName: 'squad' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const updated = bodyOf<{ project: Project }>(res).project;
    expect(updated.enabledAgentBundles).toEqual(['squad']);
    expect(updated.enabledAgents.sort()).toEqual(['builder', 'reviewer']);
    expect(updated.enabledSkills.sort()).toEqual(['gh', 'kit']);
  });

  it('404s a name that is not a bundle (a plain agent)', async () => {
    await repo.putProject(project(PROJ, MATT));
    await repo.putAgent(agent('reviewer', []));
    const res = await enableProjectAgentBundle(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, bundleName: 'reviewer' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('DELETE /projects/:projectId/agent-bundles/:bundleName (disable a whole agent bundle)', () => {
  it('drops the intent + member agents, but keeps a member shared with another enabled bundle', async () => {
    await repo.putAgent(agent('reviewer', []));
    await repo.putAgent(agent('builder', []));
    await repo.putAgent(agentBundle('squad', ['reviewer', 'builder']));
    await repo.putAgent(agentBundle('crew', ['builder']));
    // Both bundles enabled; builder is shared, reviewer only via squad.
    await repo.putProject({
      ...project(PROJ, MATT),
      enabledAgentBundles: ['squad', 'crew'],
      enabledAgents: ['reviewer', 'builder'],
    });

    const res = await disableProjectAgentBundle(
      ownerEvent({ method: 'DELETE', path: { projectId: PROJ, bundleName: 'squad' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const updated = bodyOf<{ project: Project }>(res).project;
    expect(updated.enabledAgentBundles).toEqual(['crew']);
    // reviewer removed (only squad had it); builder kept (still in crew).
    expect(updated.enabledAgents).toEqual(['builder']);
  });
});
