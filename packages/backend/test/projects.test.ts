import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { LearningRecord, Project, SessionProjection, Skill } from '@harness/shared';
import { orgScope, skillSchema } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import {
  createProject,
  deleteProjectHandler,
  disableProjectBundle,
  enableProjectAgent,
  enableProjectBundle,
  enableProjectMcpServer,
  getProject,
  getProjectDocContent,
  getProjectDocs,
  getProjectLearnings,
  getProjectRequirements,
  getProjectWireframe,
  handler,
  listProjects,
  refreshProject,
  type ProjectsDeps,
} from '../src/rest/projects.js';
import type { GitHubApp } from '../src/github/app.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U8 REST: projects. Scoping to the caller uid; another user's project is 404;
 * live counts; POST forces ownership to the caller.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');
const deps = { repo };

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const MATT = 'matt';
const ALICE = 'alice';

function project(id: string, owner: string, org = 'acme'): Project {
  return { id, name: id, repo: `gh/acme/${id}`, ownerUserId: owner, org, liveSessionCount: 0 };
}

function session(
  id: string,
  projectId: string,
  owner: string,
  status: SessionProjection['status'],
): SessionProjection {
  return {
    sessionId: id,
    projectId,
    name: id,
    host: 'h',
    ownerUserId: owner,
    status,
    tokens: 0,
    startedAt: 1,
    lastEventAt: 1,
    maxSeq: 0,
  };
}

describe('GET /projects', () => {
  it("returns only the caller's projects, with live counts", async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await repo.putProject(project('side-quest', MATT));
    await repo.putProject(project('alices-thing', ALICE));
    await repo.putSessionProjection(session('s1', 'weekly-compass', MATT, 'active'));
    await repo.putSessionProjection(session('s2', 'weekly-compass', MATT, 'idle'));

    const res = await listProjects(httpEvent({ method: 'GET', userId: MATT }), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    const { projects } = bodyOf<{ projects: Project[] }>(res as { body: string });
    const ids = projects.map((p) => p.id).sort();
    expect(ids).toEqual(['side-quest', 'weekly-compass']);
    const wc = projects.find((p) => p.id === 'weekly-compass')!;
    expect(wc.liveSessionCount).toBe(1); // active counts, idle does not
  });

  it('scopes to the active org: a project owned in another org is excluded', async () => {
    // Same owner, two orgs. The caller's effective org is the token claim
    // ('acme' by default), so only the acme-org project comes back — the repo
    // connected under 'other-org' must NOT leak into the acme view.
    await repo.putProject(project('weekly-compass', MATT, 'acme'));
    await repo.putProject(project('other-repo', MATT, 'other-org'));

    const res = await listProjects(httpEvent({ method: 'GET', userId: MATT, org: 'acme' }), deps);
    const { projects } = bodyOf<{ projects: Project[] }>(res as { body: string });
    expect(projects.map((p) => p.id)).toEqual(['weekly-compass']);
  });

  it('returns an empty list for a user with no projects', async () => {
    const res = await listProjects(httpEvent({ method: 'GET', userId: 'nobody' }), deps);
    expect(bodyOf<{ projects: unknown[] }>(res as { body: string }).projects).toEqual([]);
  });

  it('401s without an authenticated principal', async () => {
    const res = await listProjects(httpEvent({ method: 'GET', userId: null }), deps);
    expect(res).toMatchObject({ statusCode: 401 });
  });
});

describe('GET /projects/:id', () => {
  it('returns the project for its owner', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await getProject(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ project: Project }>(res as { body: string }).project.id).toBe('weekly-compass');
  });

  it("404s another user's project (no enumeration, not 403)", async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await getProject(
      httpEvent({ method: 'GET', userId: ALICE, path: { id: 'weekly-compass' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('404s a missing project', async () => {
    const res = await getProject(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'ghost' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

// --- U7/U8: GitHub framing refresh + docs endpoints ----------------------

/**
 * A stub GitHub App: records call counts and returns canned framing/docs. Cast
 * to GitHubApp since the handlers only use the read methods. `failing` makes
 * every read throw (simulating GitHub unreachable / token expired).
 */
function stubGithub(opts: { failing?: boolean } = {}) {
  const calls = { listDocs: 0, readDoc: 0, framing: 0, readPrd: 0, readWireframe: 0 };
  const app = {
    async readFramingWithCompletion() {
      calls.framing++;
      if (opts.failing) throw new Error('github unreachable');
      return { goal: 'Ship the thing', supportingOutcomeIds: ['SO-A', 'SO-B'], progressPct: 58 };
    },
    async listDocs() {
      calls.listDocs++;
      if (opts.failing) throw new Error('github unreachable');
      return [{ path: 'docs/plans/a.md', title: 'Alpha', completion: 58 }];
    },
    async readDocContent(path: string) {
      calls.readDoc++;
      if (opts.failing) throw new Error('github unreachable');
      return path === 'docs/plans/a.md' ? '# Alpha\nbody' : undefined;
    },
    async readPrdDoc() {
      calls.readPrd++;
      if (opts.failing) throw new Error('github unreachable');
      return '# Project Requirements\n\n- ship the thing';
    },
    async readWireframe() {
      calls.readWireframe++;
      if (opts.failing) throw new Error('github unreachable');
      return '<!doctype html><title>WF</title><body>wireframe body</body>';
    },
  } as unknown as GitHubApp;
  return { app, calls };
}

function depsWith(github: GitHubApp): ProjectsDeps {
  return { repo, githubFor: () => github };
}

describe('POST /projects/:id/refresh (U7)', () => {
  it('re-reads GitHub framing, stores it, and returns the updated project', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app, calls } = stubGithub();
    const res = await refreshProject(
      httpEvent({ method: 'POST', userId: MATT, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ project: Project & { supportingOutcomeIds?: string[] }; stale: boolean }>(
      res as { body: string },
    );
    expect(calls.framing).toBe(1);
    expect(body.stale).toBe(false);
    expect(body.project.progressPct).toBe(58);
    expect(body.project.prdGoal).toBe('Ship the thing');
    expect(body.project.supportingOutcomeIds).toEqual(['SO-A', 'SO-B']);
    // And it is persisted: a subsequent GET serves the stored fields.
    const stored = await repo.getProject('weekly-compass');
    expect(stored?.progressPct).toBe(58);
    expect((stored as { supportingOutcomeIds?: string[] }).supportingOutcomeIds).toEqual([
      'SO-A',
      'SO-B',
    ]);
  });

  it('returns last-known data + stale flag (never 500) when GitHub is unreachable', async () => {
    await repo.putProject({ ...project('weekly-compass', MATT), progressPct: 40 });
    const { app } = stubGithub({ failing: true });
    const res = await refreshProject(
      httpEvent({ method: 'POST', userId: MATT, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ project: Project & { framingStale?: boolean }; stale: boolean }>(
      res as { body: string },
    );
    expect(body.stale).toBe(true);
    expect(body.project.framingStale).toBe(true);
    expect(body.project.progressPct).toBe(40); // last-known preserved, not zeroed
  });

  it('404s a project the caller does not own', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub();
    const res = await refreshProject(
      httpEvent({ method: 'POST', userId: ALICE, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('serves last-known + stale when the project is not GitHub-connected', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await refreshProject(
      httpEvent({ method: 'POST', userId: MATT, path: { id: 'weekly-compass' } }),
      { repo, githubFor: () => undefined },
    );
    expect(bodyOf<{ stale: boolean }>(res as { body: string }).stale).toBe(true);
  });
});

describe('GET /projects/:id serves stored framing (U7)', () => {
  it('returns progressPct / prdGoal / supportingOutcomeIds after a refresh', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await repo.putProjectFraming('weekly-compass', {
      progressPct: 73,
      prdGoal: 'A goal',
      supportingOutcomeIds: ['SO-X'],
    });
    const res = await getProject(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      deps,
    );
    const body = bodyOf<{ project: Project & { supportingOutcomeIds?: string[] } }>(
      res as { body: string },
    );
    expect(body.project.progressPct).toBe(73);
    expect(body.project.prdGoal).toBe('A goal');
    expect(body.project.supportingOutcomeIds).toEqual(['SO-X']);
  });
});

describe('GET /projects/:id/docs (U8)', () => {
  it('returns the docs tree with per-doc completion', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub();
    const res = await getProjectDocs(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const { docs } = bodyOf<{ docs: { path: string; title: string; completion?: number }[] }>(
      res as { body: string },
    );
    expect(docs).toEqual([{ path: 'docs/plans/a.md', title: 'Alpha', completion: 58 }]);
  });

  it('returns an empty tree + stale flag when GitHub is unreachable (no 500)', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub({ failing: true });
    const res = await getProjectDocs(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ docs: unknown[]; stale: boolean }>(res as { body: string });
    expect(body.docs).toEqual([]);
    expect(body.stale).toBe(true);
  });

  it("404s another user's project docs", async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub();
    const res = await getProjectDocs(
      httpEvent({ method: 'GET', userId: ALICE, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('GET /projects/:id/docs/content (U8)', () => {
  it('returns raw markdown for one doc by ?path=', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub();
    const res = await getProjectDocContent(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { id: 'weekly-compass' },
        query: { path: 'docs/plans/a.md' },
      }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ path: string; markdown: string }>(res as { body: string });
    expect(body.path).toBe('docs/plans/a.md');
    expect(body.markdown).toContain('# Alpha');
  });

  it('400s when ?path= is missing', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub();
    const res = await getProjectDocContent(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('404s an unknown doc path', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub();
    const res = await getProjectDocContent(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { id: 'weekly-compass' },
        query: { path: 'docs/plans/ghost.md' },
      }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

// --- Project Requirements: read-only from docs/PRD.md --------------------

describe('GET /projects/:id/requirements (docs/PRD.md)', () => {
  it('serves docs/PRD.md read-only from GitHub', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app, calls } = stubGithub();
    const res = await getProjectRequirements(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ markdown: string }>(res as { body: string }).markdown).toContain(
      '# Project Requirements',
    );
    expect(calls.readPrd).toBe(1);
  });

  it("serves an empty markdown + stale when the project isn't GitHub-connected", async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await getProjectRequirements(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      { repo, githubFor: () => undefined },
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ markdown: string; stale: boolean }>(res as { body: string });
    expect(body.markdown).toBe('');
    expect(body.stale).toBe(true);
  });

  it('serves an empty markdown + stale (never 500) when GitHub is unreachable', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub({ failing: true });
    const res = await getProjectRequirements(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ markdown: string; stale: boolean }>(res as { body: string });
    expect(body.markdown).toBe('');
    expect(body.stale).toBe(true);
  });

  it("404s another user's project requirements (no enumeration)", async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub();
    const res = await getProjectRequirements(
      httpEvent({ method: 'GET', userId: ALICE, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

// --- Project wireframe: read-only from docs/wireframe.html ----------------

describe('GET /projects/:id/wireframe (docs/wireframe.html)', () => {
  it('serves docs/wireframe.html read-only from GitHub', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app, calls } = stubGithub();
    const res = await getProjectWireframe(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ html: string }>(res as { body: string }).html).toContain('wireframe body');
    expect(calls.readWireframe).toBe(1);
  });

  it("serves empty html + stale when the project isn't GitHub-connected", async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await getProjectWireframe(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      { repo, githubFor: () => undefined },
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ html: string; stale: boolean }>(res as { body: string });
    expect(body.html).toBe('');
    expect(body.stale).toBe(true);
  });

  it('serves empty html + stale (never 500) when GitHub is unreachable', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub({ failing: true });
    const res = await getProjectWireframe(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ html: string; stale: boolean }>(res as { body: string });
    expect(body.html).toBe('');
    expect(body.stale).toBe(true);
  });

  it("404s another user's project wireframe (no enumeration)", async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const { app } = stubGithub();
    const res = await getProjectWireframe(
      httpEvent({ method: 'GET', userId: ALICE, path: { id: 'weekly-compass' } }),
      depsWith(app),
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('routes GET /projects/:id/wireframe through the handler', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await handler(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { id: 'weekly-compass' },
        rawPath: '/projects/weekly-compass/wireframe',
      }),
    );
    expect(res).toMatchObject({ statusCode: 200 });
  });
});

describe('handler: PUT /projects/:id/requirements no longer routes to a write', () => {
  it('does not invoke a requirements write handler (falls through, no 200 echo)', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    // PUT /requirements has no route anymore: it falls through to listProjects,
    // which never echoes back a `{ markdown }` body (proving no write path).
    const res = await handler(
      httpEvent({
        method: 'PUT',
        userId: MATT,
        rawPath: '/projects/weekly-compass/requirements',
        path: { id: 'weekly-compass' },
        body: { markdown: '# should not persist' },
      }),
    );
    const body = bodyOf<{ markdown?: string; projects?: unknown[] }>(res as { body: string });
    expect(body.markdown).toBeUndefined();
  });
});

// --- Learnings: GET /projects/:id/learnings ------------------------------

function learning(over: Partial<LearningRecord> = {}): LearningRecord {
  return {
    projectId: 'weekly-compass',
    sessionId: 's-1',
    segmentId: 'seg-1',
    topicLabel: 'reconciliation',
    stream: 'impl',
    text: 'prefer decimal money',
    turnId: 't-1',
    ts: 1_700_000_000_000,
    seq: 5,
    ...over,
  };
}

describe('GET /projects/:id/learnings', () => {
  it('returns all learnings for a project (both streams, default)', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await repo.putLearning(learning({ turnId: 't-1', stream: 'impl' }));
    await repo.putLearning(learning({ turnId: 't-2', stream: 'doc', docRef: 'docs/plans/a.md' }));

    const res = await getProjectLearnings(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const { learnings } = bodyOf<{ learnings: LearningRecord[] }>(res as { body: string });
    expect(learnings).toHaveLength(2);
    expect(learnings.map((l) => l.stream).sort()).toEqual(['doc', 'impl']);
  });

  it('?stream=doc filters to the doc stream only', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await repo.putLearning(learning({ turnId: 't-1', stream: 'impl' }));
    await repo.putLearning(learning({ turnId: 't-2', stream: 'doc', docRef: 'docs/plans/a.md' }));

    const res = await getProjectLearnings(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { id: 'weekly-compass' },
        query: { stream: 'doc' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const { learnings } = bodyOf<{ learnings: LearningRecord[] }>(res as { body: string });
    expect(learnings).toHaveLength(1);
    expect(learnings[0]!.stream).toBe('doc');
    expect(learnings[0]!.docRef).toBe('docs/plans/a.md');
  });

  it('returns [] for an empty project (no learnings yet)', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await getProjectLearnings(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 'weekly-compass' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ learnings: LearningRecord[] }>(res as { body: string }).learnings).toEqual([]);
  });

  it("404s another user's project learnings (no enumeration, same authz as sibling reads)", async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await repo.putLearning(learning());
    const res = await getProjectLearnings(
      httpEvent({ method: 'GET', userId: ALICE, path: { id: 'weekly-compass' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('routes GET /projects/:id/learnings through the handler', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await repo.putLearning(learning());
    const res = await handler(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { id: 'weekly-compass' },
        rawPath: '/projects/weekly-compass/learnings',
      }),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ learnings: LearningRecord[] }>(res as { body: string }).learnings).toHaveLength(
      1,
    );
  });
});

describe('POST /projects', () => {
  it('creates a project owned by the caller, ignoring a forged owner', async () => {
    const res = await createProject(
      httpEvent({
        method: 'POST',
        userId: MATT,
        body: { id: 'new-proj', name: 'New', repo: 'gh/acme/new', ownerUserId: ALICE },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const stored = await repo.getProject('new-proj');
    expect(stored?.ownerUserId).toBe(MATT); // forged owner ignored
  });

  it('400s an invalid body', async () => {
    const res = await createProject(
      httpEvent({ method: 'POST', userId: MATT, body: { name: 'no id' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('auto-enables the command-hq-starter bundle on a new project', async () => {
    // Seed the org catalog with the starter bundle + two members.
    const orgRef = orgScope('acme');
    await repo.putSkill(skillSchema.parse({ name: 'hq-sync', scope: orgRef, kind: 'skill' }));
    await repo.putSkill(skillSchema.parse({ name: 'hq-add-skill', scope: orgRef, kind: 'skill' }));
    await repo.putSkill(
      skillSchema.parse({
        name: 'command-hq-starter',
        scope: orgRef,
        kind: 'bundle',
        members: ['hq-sync', 'hq-add-skill'],
      }),
    );

    const res = await createProject(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: 'acme',
        body: { id: 'fresh', name: 'Fresh', repo: 'gh/acme/fresh' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });

    const stored = await repo.getProject('fresh');
    expect(stored?.enabledBundles).toContain('command-hq-starter');
    expect(stored?.enabledSkills).toEqual(expect.arrayContaining(['hq-sync', 'hq-add-skill']));
  });

  it('creates an org-less project without starter when the org has no catalog', async () => {
    // No bundle seeded → project is created but nothing is auto-enabled.
    const res = await createProject(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: 'acme',
        body: { id: 'bare', name: 'Bare', repo: 'gh/acme/bare' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const stored = await repo.getProject('bare');
    expect(stored?.enabledBundles ?? []).not.toContain('command-hq-starter');
    expect(stored?.enabledSkills ?? []).toHaveLength(0);
  });
});

describe('DELETE /projects/:id', () => {
  it('deletes a project and its sessions for the owner', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await repo.putSessionProjection(session('s1', 'weekly-compass', MATT, 'active'));
    await repo.linkRepoToProject('acme/weekly-compass', 'weekly-compass');

    const res = await deleteProjectHandler(
      httpEvent({ method: 'DELETE', userId: MATT, path: { id: 'weekly-compass' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ deleted: boolean }>(res as { body: string }).deleted).toBe(true);

    expect(await repo.getProject('weekly-compass')).toBeUndefined();
    expect(await repo.listSessionsForProject('weekly-compass')).toEqual([]);
    expect(await repo.getProjectIdForRepo('acme/weekly-compass')).toBeUndefined();
  });

  it('404s a non-owner (no enumeration) and leaves the project intact', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await deleteProjectHandler(
      httpEvent({ method: 'DELETE', userId: ALICE, path: { id: 'weekly-compass' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
    expect(await repo.getProject('weekly-compass')).toBeDefined();
  });

  it('404s a missing project', async () => {
    const res = await deleteProjectHandler(
      httpEvent({ method: 'DELETE', userId: MATT, path: { id: 'ghost' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('401s without an authenticated principal', async () => {
    const res = await deleteProjectHandler(
      httpEvent({ method: 'DELETE', userId: null, path: { id: 'weekly-compass' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 401 });
  });

  it('routes DELETE through the lambda handler', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await handler(
      httpEvent({ method: 'DELETE', userId: MATT, path: { id: 'weekly-compass' } }),
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getProject('weekly-compass')).toBeUndefined();
  });
});

describe('project agent + mcp-server opt-in (catalog scoped to the project org)', () => {
  async function seedAgent(name: string, org = 'acme'): Promise<void> {
    await repo.putAgent({
      name,
      scope: orgScope(org),
      model: 'claude-sonnet-4',
      prompt: 'p',
      description: '',
      skills: [],
      tools: [],
      mcpServers: [],
    });
  }
  async function seedMcp(name: string, org = 'acme'): Promise<void> {
    await repo.putMcpServer({
      name,
      scope: orgScope(org),
      transport: 'stdio',
      command: 'cmd',
      args: [],
      env: {},
    });
  }

  it('enables an org-catalog agent for the project', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await seedAgent('codebase-analyzer');
    const res = await enableProjectAgent(
      httpEvent({
        method: 'POST',
        userId: MATT,
        path: { projectId: 'weekly-compass', agentName: 'codebase-analyzer' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getProject('weekly-compass'))?.enabledAgents).toEqual(['codebase-analyzer']);
  });

  it('enables an org-catalog mcp server for the project', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await seedMcp('humanlayer-approvals');
    const res = await enableProjectMcpServer(
      httpEvent({
        method: 'POST',
        userId: MATT,
        path: { projectId: 'weekly-compass', name: 'humanlayer-approvals' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getProject('weekly-compass'))?.enabledMcpServers).toEqual([
      'humanlayer-approvals',
    ]);
  });

  // Regression: catalog items must be resolved against the PROJECT's org, not the
  // caller's raw token `custom:org` claim — which can drift from the user's
  // effective (DB profile) org. A user whose token org points elsewhere must still
  // enable items that live in the project's org. This previously 404'd because the
  // handlers looked the item up under `principal.org` (the token claim).
  it('enables agent + mcp when the token org differs from the project org', async () => {
    await repo.putProject(project('weekly-compass', MATT, 'test org'));
    await seedAgent('codebase-analyzer', 'test org');
    await seedMcp('humanlayer-approvals', 'test org');

    const agentRes = await enableProjectAgent(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: 'stale-token-org', // token claim points at the WRONG org
        path: { projectId: 'weekly-compass', agentName: 'codebase-analyzer' },
      }),
      deps,
    );
    expect(agentRes).toMatchObject({ statusCode: 200 });

    const mcpRes = await enableProjectMcpServer(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: 'stale-token-org',
        path: { projectId: 'weekly-compass', name: 'humanlayer-approvals' },
      }),
      deps,
    );
    expect(mcpRes).toMatchObject({ statusCode: 200 });

    const p = await repo.getProject('weekly-compass');
    expect(p?.enabledAgents).toEqual(['codebase-analyzer']);
    expect(p?.enabledMcpServers).toEqual(['humanlayer-approvals']);
  });

  it('404s an agent not in the project org catalog', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await enableProjectAgent(
      httpEvent({
        method: 'POST',
        userId: MATT,
        path: { projectId: 'weekly-compass', agentName: 'ghost' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('project bundle opt-in', () => {
  // Seed a catalog skill/bundle into the org scope so the REST layer can resolve
  // and flatten it. Members default to [] for leaf skills.
  async function seedSkill(
    name: string,
    kind: Skill['kind'],
    members: string[] = [],
  ): Promise<void> {
    await repo.putSkill({
      name,
      scope: orgScope('acme'),
      kind,
      description: '',
      source: 'local',
      members,
      body: '',
    });
  }

  function bundleEvent(method: string, projectId: string, bundleName: string, who = MATT) {
    return httpEvent({
      method,
      userId: who,
      path: { projectId, bundleName },
    });
  }

  it('enabling a bundle records it AND unions its leaf members into enabledSkills', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await seedSkill('alpha', 'skill');
    await seedSkill('beta', 'skill');
    await seedSkill('pack', 'bundle', ['alpha', 'beta']);

    const res = await enableProjectBundle(bundleEvent('POST', 'weekly-compass', 'pack'), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    const { project: p } = bodyOf<{ project: Project }>(res as { body: string });
    expect(p.enabledBundles).toEqual(['pack']);
    expect([...(p.enabledSkills ?? [])].sort()).toEqual(['alpha', 'beta']);
  });

  it('flattens a nested bundle transitively into enabledSkills', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await seedSkill('leaf1', 'skill');
    await seedSkill('leaf2', 'skill');
    await seedSkill('inner', 'bundle', ['leaf2']);
    await seedSkill('outer', 'bundle', ['leaf1', 'inner']);

    const res = await enableProjectBundle(bundleEvent('POST', 'weekly-compass', 'outer'), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    const { project: p } = bodyOf<{ project: Project }>(res as { body: string });
    expect(p.enabledBundles).toEqual(['outer']);
    expect([...(p.enabledSkills ?? [])].sort()).toEqual(['leaf1', 'leaf2']);
  });

  it('404s POST on a non-bundle skill name', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await seedSkill('alpha', 'skill');
    const res = await enableProjectBundle(bundleEvent('POST', 'weekly-compass', 'alpha'), deps);
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('404s POST on an unknown name', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    const res = await enableProjectBundle(bundleEvent('POST', 'weekly-compass', 'ghost'), deps);
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('disabling removes the bundle and its leaves, but keeps leaves another bundle still covers', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    // Two overlapping bundles: `shared` is a member of both.
    await seedSkill('only-a', 'skill');
    await seedSkill('shared', 'skill');
    await seedSkill('only-b', 'skill');
    await seedSkill('packA', 'bundle', ['only-a', 'shared']);
    await seedSkill('packB', 'bundle', ['shared', 'only-b']);

    await enableProjectBundle(bundleEvent('POST', 'weekly-compass', 'packA'), deps);
    await enableProjectBundle(bundleEvent('POST', 'weekly-compass', 'packB'), deps);

    const res = await disableProjectBundle(bundleEvent('DELETE', 'weekly-compass', 'packA'), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    const { project: p } = bodyOf<{ project: Project }>(res as { body: string });
    expect(p.enabledBundles).toEqual(['packB']);
    // `only-a` is dropped; `shared` survives (still covered by packB); `only-b` stays.
    expect([...(p.enabledSkills ?? [])].sort()).toEqual(['only-b', 'shared']);
  });

  it('disabling a bundle that vanished from the catalog still clears the intent', async () => {
    // Seed the project with a stale bundle intent and a leaf the bundle once brought.
    const p = project('weekly-compass', MATT);
    p.enabledBundles = ['gone'];
    p.enabledSkills = ['orphan'];
    await repo.putProject(p);

    const res = await disableProjectBundle(bundleEvent('DELETE', 'weekly-compass', 'gone'), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    const { project: out } = bodyOf<{ project: Project }>(res as { body: string });
    expect(out.enabledBundles).toEqual([]);
    // No catalog entry => no leaves to strip; the orphan skill is left intact.
    expect(out.enabledSkills).toEqual(['orphan']);
  });

  it('403s a non-owner non-admin', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await seedSkill('alpha', 'skill');
    await seedSkill('pack', 'bundle', ['alpha']);
    const res = await enableProjectBundle(
      bundleEvent('POST', 'weekly-compass', 'pack', ALICE),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('routes bundle POST/DELETE through the lambda handler', async () => {
    await repo.putProject(project('weekly-compass', MATT));
    await seedSkill('alpha', 'skill');
    await seedSkill('pack', 'bundle', ['alpha']);

    const enableRes = await handler(
      httpEvent({
        method: 'POST',
        userId: MATT,
        path: { projectId: 'weekly-compass', bundleName: 'pack' },
        rawPath: '/projects/weekly-compass/bundles/pack',
      }),
    );
    expect(enableRes).toMatchObject({ statusCode: 200 });
    expect((await repo.getProject('weekly-compass'))?.enabledBundles).toEqual(['pack']);

    const disableRes = await handler(
      httpEvent({
        method: 'DELETE',
        userId: MATT,
        path: { projectId: 'weekly-compass', bundleName: 'pack' },
        rawPath: '/projects/weekly-compass/bundles/pack',
      }),
    );
    expect(disableRes).toMatchObject({ statusCode: 200 });
    const after = await repo.getProject('weekly-compass');
    expect(after?.enabledBundles).toEqual([]);
    expect(after?.enabledSkills).toEqual([]);
  });
});
