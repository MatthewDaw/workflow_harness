import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Envelope, Event, Project, SessionProjection } from '@harness/shared';
import { Repo, type ConnectionRecord } from '../src/db/repo.js';
import { control, getSession, listSessions } from '../src/rest/sessions.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U8 REST: sessions. Live filter, live-first ordering, uid scoping, paged event
 * replay, and 404 for a non-owned session.
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
  lastEventAt = 1,
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
    lastEventAt,
    maxSeq: 0,
  };
}

function env(seq: number, event: Event): Envelope {
  return { v: 1, instanceId: 'i', host: 'h', ts: 1_700_000_000_000 + seq, seq, event };
}

describe('GET /sessions', () => {
  it('lists the caller sessions live-first, scoped to uid', async () => {
    await repo.putProject(project('p1', MATT));
    await repo.putSessionProjection(session('idle-1', 'p1', MATT, 'idle', 5));
    await repo.putSessionProjection(session('live-1', 'p1', MATT, 'active', 2));
    await repo.putSessionProjection(session('alice-live', 'p2', ALICE, 'active', 9));

    const res = await listSessions(httpEvent({ method: 'GET', userId: MATT }), deps);
    const { sessions } = bodyOf<{ sessions: SessionProjection[] }>(res as { body: string });
    expect(sessions.map((s) => s.sessionId)).toEqual(['live-1', 'idle-1']); // live first; alice excluded
  });

  it('?live=true returns only live sessions for the caller', async () => {
    await repo.putProject(project('p1', MATT));
    await repo.putSessionProjection(session('idle-1', 'p1', MATT, 'idle'));
    await repo.putSessionProjection(session('live-1', 'p1', MATT, 'active'));
    await repo.putSessionProjection(session('alice-live', 'p2', ALICE, 'active'));

    const res = await listSessions(
      httpEvent({ method: 'GET', userId: MATT, query: { live: 'true' } }),
      deps,
    );
    const { sessions } = bodyOf<{ sessions: SessionProjection[] }>(res as { body: string });
    expect(sessions.map((s) => s.sessionId)).toEqual(['live-1']);
  });
});

describe('GET /sessions/:id', () => {
  it('returns the projection plus a page of events for the owner', async () => {
    await repo.putSessionProjection(session('s-1', 'p1', MATT, 'active'));
    await repo.appendEvent(
      env(0, { kind: 'session.start', sessionId: 's-1', projectId: 'p1', host: 'h', name: 'n' }),
    );
    await repo.appendEvent(
      env(1, { kind: 'tool.call', sessionId: 's-1', tool: 'Bash', argsSummary: 'ls' }),
    );

    const res = await getSession(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 's-1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const { session: s, events } = bodyOf<{ session: SessionProjection; events: Envelope[] }>(
      res as { body: string },
    );
    expect(s.sessionId).toBe('s-1');
    expect(events.map((e) => e.seq)).toEqual([0, 1]);
  });

  it('404s a session the caller does not own', async () => {
    await repo.putSessionProjection(session('s-1', 'p1', ALICE, 'active'));
    const res = await getSession(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 's-1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('POST /sessions/:id/control', () => {
  const INSTANCE = 'inst-a';

  function recordingPoster() {
    const posts: Array<{ connectionId: string; body: unknown }> = [];
    return {
      posts,
      poster: {
        post: async (connectionId: string, body: unknown) => {
          posts.push({ connectionId, body });
          return true;
        },
      },
    };
  }

  async function seedOwnedSessionWithDaemon(): Promise<void> {
    await repo.putSessionProjection({
      ...session('s-1', 'p1', MATT, 'active'),
      instanceId: INSTANCE,
    });
    const conn: ConnectionRecord = {
      connectionId: 'daemon-conn',
      userId: MATT,
      org: 'acme',
      role: 'daemon',
      instanceId: INSTANCE,
      connectedAt: 1,
    };
    await repo.putConnection(conn);
  }

  it('routes a control frame to the owning daemon and returns 202', async () => {
    await seedOwnedSessionWithDaemon();
    const { posts, poster } = recordingPoster();
    const res = await control(
      httpEvent({
        method: 'POST',
        userId: MATT,
        rawPath: '/sessions/s-1/control',
        path: { id: 's-1' },
        body: { action: 'inject', payload: { text: 'answer: 42' } },
      }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 202 });
    expect(posts).toHaveLength(1);
    expect(posts[0]!.connectionId).toBe('daemon-conn');
    expect(posts[0]!.body).toMatchObject({
      type: 'control',
      sessionId: 's-1',
      action: 'inject',
      payload: { text: 'answer: 42' },
    });
  });

  it('404s a control frame from a non-owner and routes nothing', async () => {
    await seedOwnedSessionWithDaemon();
    const { posts, poster } = recordingPoster();
    const res = await control(
      httpEvent({
        method: 'POST',
        userId: ALICE,
        path: { id: 's-1' },
        body: { action: 'inject', payload: { text: 'rm -rf /' } },
      }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 404 });
    expect(posts).toHaveLength(0);
  });

  it('404s control for an unknown session (no enumeration)', async () => {
    const { posts, poster } = recordingPoster();
    const res = await control(
      httpEvent({ method: 'POST', userId: MATT, path: { id: 'ghost' }, body: { action: 'pause' } }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 404 });
    expect(posts).toHaveLength(0);
  });

  it('rejects an invalid control body (400)', async () => {
    await seedOwnedSessionWithDaemon();
    const { poster } = recordingPoster();
    const res = await control(
      httpEvent({ method: 'POST', userId: MATT, path: { id: 's-1' }, body: { action: 'nope' } }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('502s when the owning daemon is offline', async () => {
    await repo.putSessionProjection({
      ...session('s-1', 'p1', MATT, 'active'),
      instanceId: INSTANCE,
    });
    const { posts, poster } = recordingPoster();
    const res = await control(
      httpEvent({
        method: 'POST',
        userId: MATT,
        path: { id: 's-1' },
        body: { action: 'inject', payload: { text: 'hi' } },
      }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 502 });
    expect(posts).toHaveLength(0);
  });
});
