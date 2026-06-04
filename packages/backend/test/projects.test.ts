import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Project, SessionProjection } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import {
  createProject,
  deleteProjectHandler,
  getProject,
  getProjectDocContent,
  getProjectDocs,
  getProjectRequirements,
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

function project(id: string, owner: string): Project {
  return { id, name: id, repo: `gh/acme/${id}`, ownerUserId: owner, liveSessionCount: 0 };
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
    costUsd: 0,
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
  const calls = { listDocs: 0, readDoc: 0, framing: 0, readPrd: 0 };
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

  it("404s a non-owner (no enumeration) and leaves the project intact", async () => {
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
