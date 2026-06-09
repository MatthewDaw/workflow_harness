import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import {
  orgScope,
  type Agent,
  type Project,
  type Skill,
  type Workflow,
  type WorkflowNode,
} from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import {
  createWorkflow,
  deleteWorkflow,
  getWorkflow,
  promoteWorkflow,
  resolveWorkflows,
} from '../src/rest/workflows.js';
import {
  disableProjectWorkflow,
  enableProjectWorkflow,
  type ProjectsDeps,
} from '../src/rest/projects.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * Org-catalog workflows (mirrors agents.test.ts). GET lists the org catalog;
 * writes are admin-gated, force org scope, and stamp createdBy; promote (NOT
 * admin-gated) only repoints the org-wide TRUE variant. A workflow is a DAG of
 * catalog agents, so the project opt-in unions every node's agent — and
 * transitively its skills — into the project's enabled sets.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');
const deps = { repo };
const projectDeps: ProjectsDeps = { repo };

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const MATT = 'matt';
const ORG = 'acme';
const PROJ = 'weekly-compass';
const SCOPE = orgScope(ORG);

function node(id: string, agent: string, dependsOn: string[] = []): WorkflowNode {
  return { id, agent, label: '', prompt: '', dependsOn };
}
function workflow(name: string, nodes: WorkflowNode[] = []): Workflow {
  return { name, scope: SCOPE, kind: 'workflow', description: '', nodes };
}
function agent(name: string, skills: string[] = []): Agent {
  return { name, scope: SCOPE, model: 'opus', prompt: '', skills, tools: [] };
}
function skill(name: string): Skill {
  return { name, scope: SCOPE, kind: 'skill', description: '', source: 'local', members: [], body: '' };
}
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

function adminEvent(opts: Parameters<typeof httpEvent>[0]) {
  return httpEvent({ org: ORG, admin: true, ...opts });
}
function ownerEvent(opts: Parameters<typeof httpEvent>[0]) {
  return httpEvent({ userId: MATT, org: ORG, ...opts });
}

describe('POST /workflows (admin-gated org write + createdBy)', () => {
  it('forbids a non-admin create', async () => {
    const res = await createWorkflow(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, body: workflow('pipeline') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('creates at org scope for an admin, ignoring a client scope, and stamps createdBy', async () => {
    const res = await createWorkflow(
      adminEvent({
        method: 'POST',
        userId: MATT,
        body: { ...workflow('pipeline'), scope: { tier: 'user', id: 'someone' } },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const stored = await repo.getWorkflow(SCOPE, 'pipeline');
    expect(stored?.scope).toEqual({ tier: 'org', id: ORG });
    expect(stored?.createdBy).toEqual({ userId: MATT, name: MATT });
  });
});

describe('PUT /workflows/:name (preserves createdBy)', () => {
  it('updates but keeps the original createdBy', async () => {
    await repo.putWorkflow({ ...workflow('pipeline'), createdBy: { userId: 'alice', name: 'Alice' } });
    const res = await createWorkflow(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'pipeline' },
        body: { ...workflow('pipeline'), description: 'updated' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const stored = await repo.getWorkflow(SCOPE, 'pipeline');
    expect(stored?.description).toBe('updated');
    expect(stored?.createdBy).toEqual({ userId: 'alice', name: 'Alice' });
  });
});

describe('GET /workflows (org catalog) — CRUD round-trip', () => {
  it('returns the org catalog', async () => {
    await repo.putWorkflow(workflow('pipeline'));
    await repo.putWorkflow(workflow('release'));
    const res = await resolveWorkflows(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { workflows } = bodyOf<{ workflows: Workflow[] }>(res as { body: string });
    expect(workflows.map((w) => w.name).sort()).toEqual(['pipeline', 'release']);
  });

  it('round-trips a created workflow through GET /workflows/:name', async () => {
    const created = await createWorkflow(
      adminEvent({
        method: 'POST',
        userId: MATT,
        body: workflow('pipeline', [node('build', 'builder'), node('test', 'tester', ['build'])]),
      }),
      deps,
    );
    expect(created).toMatchObject({ statusCode: 201 });

    const got = await getWorkflow(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { name: 'pipeline' } }),
      deps,
    );
    expect(got).toMatchObject({ statusCode: 200 });
    const read = bodyOf<{ workflow: Workflow }>(got as { body: string }).workflow;
    expect(read.nodes.map((n) => n.id)).toEqual(['build', 'test']);
    expect(read.nodes.find((n) => n.id === 'test')?.dependsOn).toEqual(['build']);
  });

  it('404s a missing workflow', async () => {
    const res = await getWorkflow(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { name: 'ghost' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('DELETE /workflows/:name', () => {
  it('deletes for an admin', async () => {
    await repo.putWorkflow(workflow('pipeline'));
    const res = await deleteWorkflow(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'pipeline' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getWorkflow(SCOPE, 'pipeline')).toBeUndefined();
  });

  it('forbids a non-admin delete', async () => {
    await repo.putWorkflow(workflow('pipeline'));
    const res = await deleteWorkflow(
      httpEvent({ method: 'DELETE', userId: MATT, org: ORG, path: { name: 'pipeline' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });
});

describe('versioning: create snapshots rev 1 + promote repoints TRUE', () => {
  it('stamps version fields on create and initializes the TRUE pointer', async () => {
    const res = await createWorkflow(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, admin: true, body: workflow('pipeline') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const created = bodyOf<{ workflow: Workflow }>(res as { body: string }).workflow;
    expect(created.variantId).toBe('pipeline');
    expect(created.version).toBe(1);
    const truth = await repo.getTrueVariant(SCOPE, 'WORKFLOW', 'pipeline');
    expect(truth).toMatchObject({ baseName: 'pipeline', variantId: 'pipeline', rev: 1 });
  });

  it('a non-admin member may promote (not admin-gated) — repoints TRUE', async () => {
    await createWorkflow(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, admin: true, body: workflow('pipeline') }),
      deps,
    );
    const res = await promoteWorkflow(
      httpEvent({
        method: 'POST',
        userId: 'bob',
        org: ORG,
        path: { name: 'pipeline' },
        rawPath: '/workflows/pipeline/promote',
        body: { variantId: 'pipeline#R#r#U#bob', rev: 1 },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getTrueVariant(SCOPE, 'WORKFLOW', 'pipeline'))?.variantId).toBe(
      'pipeline#R#r#U#bob',
    );
  });
});

/**
 * Canonical built-in workflows (seed-owned, `createdBy.userId === 'system'`) are
 * fork-only via REST: the base is updated only by the git seed; edits must fork.
 */
describe('built-in workflows are fork-only via REST (git-seed owned)', () => {
  const builtinWorkflow = (name: string): Workflow => ({
    ...workflow(name),
    createdBy: { userId: 'system', name: 'system' },
  });

  it('rejects an in-place PUT to a built-in workflow (409)', async () => {
    await repo.putWorkflow(builtinWorkflow('forge'));
    const res = await createWorkflow(
      adminEvent({ method: 'PUT', userId: MATT, path: { name: 'forge' }, body: workflow('forge') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
  });

  it('rejects deleting a built-in workflow (409)', async () => {
    await repo.putWorkflow(builtinWorkflow('forge'));
    const res = await deleteWorkflow(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'forge' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
  });

  it('ALLOWS forking a built-in workflow (repoId + authorUserId set)', async () => {
    await repo.putWorkflow(builtinWorkflow('forge'));
    const res = await createWorkflow(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'forge' },
        body: { ...workflow('forge'), repoId: 'repo1', authorUserId: MATT },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
  });
});

/**
 * Project opt-in: enabling a workflow records it in `enabledWorkflows` AND unions
 * every node's referenced agent into `enabledAgents` and (transitively) those
 * agents' skills into `enabledSkills` — the same machinery enabling an agent uses.
 */
describe('POST /projects/:projectId/workflows/:workflowName (unions agents + skills)', () => {
  it('enables the workflow and unions each node agent + its skills', async () => {
    await repo.putProject(project(PROJ, MATT));
    await repo.putSkill(skill('a'));
    await repo.putSkill(skill('b'));
    await repo.putAgent(agent('builder', ['a']));
    await repo.putAgent(agent('tester', ['b']));
    // A two-node DAG: build -> test, each running a distinct agent.
    await repo.putWorkflow(
      workflow('pipeline', [node('build', 'builder'), node('test', 'tester', ['build'])]),
    );

    const res = await enableProjectWorkflow(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, workflowName: 'pipeline' } }),
      projectDeps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const updated = bodyOf<{ project: Project }>(res as { body: string }).project;
    expect(updated.enabledWorkflows).toEqual(['pipeline']);
    expect(updated.enabledAgents.sort()).toEqual(['builder', 'tester']);
    expect(updated.enabledSkills.sort()).toEqual(['a', 'b']);
  });

  it('404s a workflow that is not in the org catalog', async () => {
    await repo.putProject(project(PROJ, MATT));
    const res = await enableProjectWorkflow(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, workflowName: 'ghost' } }),
      projectDeps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('DELETE /projects/:projectId/workflows/:workflowName (does NOT prune agents)', () => {
  it('removes only the workflow, leaving enabledAgents/enabledSkills intact', async () => {
    await repo.putProject({
      ...project(PROJ, MATT),
      enabledWorkflows: ['pipeline'],
      enabledAgents: ['builder'],
      enabledSkills: ['a'],
    });
    const res = await disableProjectWorkflow(
      ownerEvent({ method: 'DELETE', path: { projectId: PROJ, workflowName: 'pipeline' } }),
      projectDeps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const updated = bodyOf<{ project: Project }>(res as { body: string }).project;
    expect(updated.enabledWorkflows).toEqual([]);
    // Agents + skills brought by the workflow are NOT pruned.
    expect(updated.enabledAgents).toEqual(['builder']);
    expect(updated.enabledSkills).toEqual(['a']);
  });
});
