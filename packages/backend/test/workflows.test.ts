import { describe, expect, it } from 'vitest';
import type { Project, Workflow, WorkflowNode } from '@harness/shared';
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
import { memRepoHarness } from './helpers/memtable.js';
import { adminEvent, bodyOf, httpEvent } from './helpers/httpevent.js';
import { MATT, ORG, SCOPE, makeAgent, makeProject, makeSkill } from './helpers/factories.js';
import { describeOrgCatalogContract } from './helpers/catalog-contract.js';

/**
 * Org-catalog workflows. The generic REST contract runs via
 * describeOrgCatalogContract; this file keeps the workflow-specific behavior:
 * a workflow is a DAG of catalog agents, so the project opt-in unions every
 * node's agent — and transitively its skills — into the project's enabled sets.
 */

const { repo } = memRepoHarness();
const deps = { repo };
const projectDeps: ProjectsDeps = { repo };

const PROJ = 'weekly-compass';

function node(id: string, agent: string, dependsOn: string[] = []): WorkflowNode {
  return { id, agent, label: '', prompt: '', dependsOn };
}
function workflow(name: string, nodes: WorkflowNode[] = []): Workflow {
  return { name, scope: SCOPE, kind: 'workflow', description: '', nodes };
}

function ownerEvent(opts: Parameters<typeof httpEvent>[0]) {
  return httpEvent({ userId: MATT, org: ORG, ...opts });
}

describeOrgCatalogContract<Workflow>({
  noun: 'workflow',
  plural: 'workflows',
  entity: 'WORKFLOW',
  repo,
  sampleNames: ['pipeline', 'release'],
  builtinName: 'forge',
  make: (name) => workflow(name),
  makeBuiltin: (name) => ({ ...workflow(name), createdBy: { userId: 'system', name: 'system' } }),
  mutate: (w) => ({ ...w, description: 'updated' }),
  assertMutated: (stored) => expect(stored?.description).toBe('updated'),
  create: (e) => createWorkflow(e, deps),
  remove: (e) => deleteWorkflow(e, deps),
  get: (e) => getWorkflow(e, deps),
  list: (e) => resolveWorkflows(e, deps),
  promote: (e) => promoteWorkflow(e, deps),
  put: (w) => repo.putWorkflow(w),
  read: (name) => repo.getWorkflow(SCOPE, name),
  nonAdminPromote: 'allowed',
});

describe('workflow round-trip (DAG nodes preserved)', () => {
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
    const read = bodyOf<{ workflow: Workflow }>(got).workflow;
    expect(read.nodes.map((n) => n.id)).toEqual(['build', 'test']);
    expect(read.nodes.find((n) => n.id === 'test')?.dependsOn).toEqual(['build']);
  });
});

/**
 * Project opt-in: enabling a workflow records it in `enabledWorkflows` AND unions
 * every node's referenced agent into `enabledAgents` and (transitively) those
 * agents' skills into `enabledSkills` — the same machinery enabling an agent uses.
 */
describe('POST /projects/:projectId/workflows/:workflowName (unions agents + skills)', () => {
  it('enables the workflow and unions each node agent + its skills', async () => {
    await repo.putProject(makeProject(PROJ, MATT));
    await repo.putSkill(makeSkill('a'));
    await repo.putSkill(makeSkill('b'));
    await repo.putAgent(makeAgent('builder', ['a']));
    await repo.putAgent(makeAgent('tester', ['b']));
    // A two-node DAG: build -> test, each running a distinct agent.
    await repo.putWorkflow(
      workflow('pipeline', [node('build', 'builder'), node('test', 'tester', ['build'])]),
    );

    const res = await enableProjectWorkflow(
      ownerEvent({ method: 'POST', path: { projectId: PROJ, workflowName: 'pipeline' } }),
      projectDeps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const updated = bodyOf<{ project: Project }>(res).project;
    expect(updated.enabledWorkflows).toEqual(['pipeline']);
    expect(updated.enabledAgents.sort()).toEqual(['builder', 'tester']);
    expect(updated.enabledSkills.sort()).toEqual(['a', 'b']);
  });

  it('404s a workflow that is not in the org catalog', async () => {
    await repo.putProject(makeProject(PROJ, MATT));
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
      ...makeProject(PROJ, MATT),
      enabledWorkflows: ['pipeline'],
      enabledAgents: ['builder'],
      enabledSkills: ['a'],
    });
    const res = await disableProjectWorkflow(
      ownerEvent({ method: 'DELETE', path: { projectId: PROJ, workflowName: 'pipeline' } }),
      projectDeps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const updated = bodyOf<{ project: Project }>(res).project;
    expect(updated.enabledWorkflows).toEqual([]);
    // Agents + skills brought by the workflow are NOT pruned.
    expect(updated.enabledAgents).toEqual(['builder']);
    expect(updated.enabledSkills).toEqual(['a']);
  });
});
