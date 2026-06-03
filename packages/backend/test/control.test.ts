import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';
import type { Envelope, Event, SessionProjection } from '@harness/shared';
import { Repo, type ConnectionRecord } from '../src/db/repo.js';
import { subscribe } from '../src/ws/subscribe.js';
import { control } from '../src/ws/control.js';
import { ingest } from '../src/ws/event.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * U7 control gateway: subscribe replays a recent window then registers a live
 * listener; control authorizes session ownership and routes to the owning
 * daemon's connection. End-to-end through the real Repo + in-memory table.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const SESSION = 's-1';
const INSTANCE = 'inst-a';
const OWNER = 'matt';
const OTHER = 'alice';

interface Recorded {
  connectionId: string;
  body: unknown;
}
function recordingPoster() {
  const posts: Recorded[] = [];
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

function wsEvent(body: unknown, connectionId: string): APIGatewayProxyWebsocketEventV2 {
  return {
    requestContext: { connectionId, domainName: 'd', stage: 'prod' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  } as unknown as APIGatewayProxyWebsocketEventV2;
}

async function seedConn(
  connectionId: string,
  userId: string,
  role: 'daemon' | 'web',
  instanceId?: string,
): Promise<void> {
  const rec: ConnectionRecord = {
    connectionId,
    userId,
    org: 'acme',
    role,
    instanceId,
    connectedAt: 1,
  };
  await repo.putConnection(rec);
}

async function seedSession(): Promise<void> {
  const proj: SessionProjection = {
    sessionId: SESSION,
    projectId: 'weekly-compass',
    name: 'reconcile-variance',
    host: 'matt@mbp',
    instanceId: INSTANCE,
    ownerUserId: OWNER,
    status: 'active',
    tokens: 0,
    costUsd: 0,
    startedAt: 1,
    lastEventAt: 1,
    maxSeq: 0,
  };
  await repo.putSessionProjection(proj);
}

function env(seq: number, event: Event): Envelope {
  return { v: 1, instanceId: INSTANCE, host: 'matt@mbp', ts: 1_700_000_000_000 + seq, seq, event };
}

describe('subscribe', () => {
  it('replays a recent window then registers the web connection as a listener', async () => {
    await seedSession();
    // Two prior events to replay.
    await repo.appendEvent(
      env(0, {
        kind: 'session.start',
        sessionId: SESSION,
        projectId: 'weekly-compass',
        host: 'h',
        name: 'n',
      }),
    );
    await repo.appendEvent(
      env(1, { kind: 'tool.call', sessionId: SESSION, tool: 'Bash', argsSummary: 'ls' }),
    );
    await seedConn('web-conn', OWNER, 'web');

    const { posts, poster } = recordingPoster();
    const res = await subscribe(wsEvent({ sessionId: SESSION }, 'web-conn'), { repo, poster });
    expect(res).toMatchObject({ statusCode: 200 });

    // The recent window was posted oldest-first.
    expect(posts).toHaveLength(1);
    const replay = posts[0]!.body as { type: string; events: Envelope[] };
    expect(replay.type).toBe('replay');
    expect(replay.events.map((e) => e.seq)).toEqual([0, 1]);

    // Registered for live fan-out.
    expect(await repo.listListeners(SESSION)).toEqual(['web-conn']);
  });

  it('streams subsequent live events to the subscriber after subscribe', async () => {
    await seedSession();
    await seedConn('web-conn', OWNER, 'web');
    await seedConn('daemon-conn', OWNER, 'daemon', INSTANCE);

    const { posts, poster } = recordingPoster();
    await subscribe(wsEvent({ sessionId: SESSION }, 'web-conn'), { repo, poster });

    // A new event ingested now fans out to the subscriber.
    await ingest(
      wsEvent(
        env(1, { kind: 'tool.call', sessionId: SESSION, tool: 'Read', argsSummary: 'x' }),
        'daemon-conn',
      ),
      { repo, poster },
    );

    const live = posts.filter((p) => (p.body as { type: string }).type === 'event');
    expect(live).toHaveLength(1);
    expect(live[0]!.connectionId).toBe('web-conn');
  });

  it('denies a subscribe for a session the requester does not own', async () => {
    await seedSession();
    await seedConn('web-conn', OTHER, 'web');
    const { posts, poster } = recordingPoster();
    const res = await subscribe(wsEvent({ sessionId: SESSION }, 'web-conn'), { repo, poster });
    expect(res).toMatchObject({ statusCode: 403 });
    expect(posts).toHaveLength(0);
    expect(await repo.listListeners(SESSION)).toEqual([]);
  });
});

describe('control authorization', () => {
  it('denies a control frame from a non-owner and routes nothing', async () => {
    await seedSession();
    await seedConn('daemon-conn', OWNER, 'daemon', INSTANCE);
    await seedConn('attacker-conn', OTHER, 'web');

    const { posts, poster } = recordingPoster();
    const res = await control(
      wsEvent(
        { sessionId: SESSION, action: 'inject', payload: { text: 'rm -rf /' } },
        'attacker-conn',
      ),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 403 });
    expect(posts).toHaveLength(0);
  });

  it('denies control for an unknown session (no enumeration)', async () => {
    await seedConn('web-conn', OWNER, 'web');
    const { posts, poster } = recordingPoster();
    const res = await control(wsEvent({ sessionId: 'ghost', action: 'pause' }, 'web-conn'), {
      repo,
      poster,
    });
    expect(res).toMatchObject({ statusCode: 403 });
    expect(posts).toHaveLength(0);
  });

  it('denies a shutdown frame from a non-owner (terminate inherits the ownership gate)', async () => {
    await seedSession();
    await seedConn('daemon-conn', OWNER, 'daemon', INSTANCE);
    await seedConn('attacker-conn', OTHER, 'web');

    const { posts, poster } = recordingPoster();
    const res = await control(
      wsEvent({ sessionId: SESSION, action: 'shutdown' }, 'attacker-conn'),
      { repo, poster },
    );
    // A non-owner can no more terminate a session than inject into it.
    expect(res).toMatchObject({ statusCode: 403 });
    expect(posts).toHaveLength(0);
  });
});

describe('control routing', () => {
  it('routes an inject to the owning daemon connection', async () => {
    await seedSession();
    await seedConn('daemon-conn', OWNER, 'daemon', INSTANCE);
    await seedConn('web-conn', OWNER, 'web');

    const { posts, poster } = recordingPoster();
    const res = await control(
      wsEvent(
        { sessionId: SESSION, action: 'inject', payload: { text: 'answer: 42' } },
        'web-conn',
      ),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(posts).toHaveLength(1);
    expect(posts[0]!.connectionId).toBe('daemon-conn'); // routed to the daemon, not the web client
    expect(posts[0]!.body).toMatchObject({
      type: 'control',
      sessionId: SESSION,
      action: 'inject',
      payload: { text: 'answer: 42' },
    });
  });

  it('routes pause and interrupt actions to the daemon', async () => {
    await seedSession();
    await seedConn('daemon-conn', OWNER, 'daemon', INSTANCE);
    await seedConn('web-conn', OWNER, 'web');
    const { posts, poster } = recordingPoster();

    await control(wsEvent({ sessionId: SESSION, action: 'pause' }, 'web-conn'), { repo, poster });
    await control(wsEvent({ sessionId: SESSION, action: 'interrupt' }, 'web-conn'), {
      repo,
      poster,
    });
    expect(posts.map((p) => (p.body as { action: string }).action)).toEqual(['pause', 'interrupt']);
    expect(posts.every((p) => p.connectionId === 'daemon-conn')).toBe(true);
  });

  it('routes shutdown and kill terminate frames to the owning daemon', async () => {
    await seedSession();
    await seedConn('daemon-conn', OWNER, 'daemon', INSTANCE);
    await seedConn('web-conn', OWNER, 'web');
    const { posts, poster } = recordingPoster();

    await control(wsEvent({ sessionId: SESSION, action: 'shutdown' }, 'web-conn'), { repo, poster });
    await control(wsEvent({ sessionId: SESSION, action: 'kill' }, 'web-conn'), { repo, poster });

    expect(posts.map((p) => (p.body as { action: string }).action)).toEqual(['shutdown', 'kill']);
    expect(posts.every((p) => p.connectionId === 'daemon-conn')).toBe(true);
  });

  it('surfaces an error when the owning daemon is offline', async () => {
    await seedSession(); // session owned, but no daemon connection for INSTANCE
    await seedConn('web-conn', OWNER, 'web');
    const { posts, poster } = recordingPoster();
    const res = await control(
      wsEvent({ sessionId: SESSION, action: 'inject', payload: { text: 'hi' } }, 'web-conn'),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 502 });
    expect(posts).toHaveLength(0);
  });

  it('rejects an invalid control frame', async () => {
    await seedConn('web-conn', OWNER, 'web');
    const { poster } = recordingPoster();
    const res = await control(wsEvent({ sessionId: SESSION, action: 'nope' }, 'web-conn'), {
      repo,
      poster,
    });
    expect(res).toMatchObject({ statusCode: 400 });
  });
});
