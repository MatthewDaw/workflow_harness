import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Project, SessionProjection } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { createProject, getProject, listProjects } from '../src/rest/projects.js';
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
