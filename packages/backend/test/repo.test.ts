import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import {
  DynamoDBDocumentClient,
  GetCommand,
  PutCommand,
  QueryCommand,
} from '@aws-sdk/lib-dynamodb';
import type { Envelope, SessionProjection } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import * as k from '../src/db/keys.js';

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => ddbMock.reset());

function envelope(seq: number): Envelope {
  return {
    v: 1,
    instanceId: 'inst-0',
    host: 'matt@mbp',
    ts: 1717200000000 + seq,
    seq,
    event: { kind: 'user.msg', sessionId: 'a91f', tokens: 1 },
  };
}

describe('key builders', () => {
  it('builds event keys ordered by zero-padded seq', () => {
    expect(k.eventKey('a91f', 7).SK < k.eventKey('a91f', 42).SK).toBe(true);
    expect(k.eventKey('a91f', 7).SK < k.eventKey('a91f', 100).SK).toBe(true); // padding beats lexical
  });

  it('maps scope tiers to partition ids', () => {
    expect(k.scopeId({ tier: 'project', id: 'weekly-compass' })).toBe('proj#weekly-compass');
    expect(k.scopeId({ tier: 'user', id: 'matt' })).toBe('user#matt');
    expect(k.scopeId({ tier: 'org', id: 'acme' })).toBe('org#acme');
  });

  it('only indexes live sessions on GSI1', () => {
    expect(k.liveSessionIndex('active', 1, 's')).toBeDefined();
    expect(k.liveSessionIndex('needs_input', 1, 's')).toBeDefined();
    expect(k.liveSessionIndex('idle', 1, 's')).toBeUndefined();
    expect(k.liveSessionIndex('done', 1, 's')).toBeUndefined();
  });
});

describe('appendEvent', () => {
  it('stores an event with a conditional write', async () => {
    ddbMock.on(PutCommand).resolves({});
    const res = await repo.appendEvent(envelope(0));
    expect(res.stored).toBe(true);
    const call = ddbMock.commandCalls(PutCommand)[0]!.args[0].input;
    expect(call.Item).toMatchObject(k.eventKey('a91f', 0));
    expect(call.ConditionExpression).toBe('attribute_not_exists(PK)');
  });

  it('is idempotent on a duplicate seq (swallows ConditionalCheckFailed)', async () => {
    ddbMock
      .on(PutCommand)
      .rejects(Object.assign(new Error('exists'), { name: 'ConditionalCheckFailedException' }));
    const res = await repo.appendEvent(envelope(0));
    expect(res.stored).toBe(false);
  });

  it('rethrows non-conditional errors', async () => {
    ddbMock
      .on(PutCommand)
      .rejects(Object.assign(new Error('boom'), { name: 'ThrottlingException' }));
    await expect(repo.appendEvent(envelope(0))).rejects.toThrow('boom');
  });
});

describe('session projections', () => {
  const base: SessionProjection = {
    sessionId: 'a91f',
    projectId: 'weekly-compass',
    name: 'reconcile-variance',
    host: 'matt@mbp',
    status: 'active',
    tokens: 48000,
    costUsd: 0.62,
    startedAt: 1717200000000,
    lastEventAt: 1717200900000,
  };

  it('writes GSI1 live keys for a live session', async () => {
    ddbMock.on(PutCommand).resolves({});
    await repo.putSessionProjection(base);
    const item = ddbMock.commandCalls(PutCommand)[0]!.args[0].input.Item as Record<string, unknown>;
    expect(item.GSI1PK).toBe(k.LIVE_PARTITION);
  });

  it('omits GSI1 keys for an idle session so it drops out of the live index', async () => {
    ddbMock.on(PutCommand).resolves({});
    await repo.putSessionProjection({ ...base, status: 'idle' });
    const item = ddbMock.commandCalls(PutCommand)[0]!.args[0].input.Item as Record<string, unknown>;
    expect(item.GSI1PK).toBeUndefined();
  });

  it('lists live sessions via GSI1, newest first', async () => {
    ddbMock.on(QueryCommand).resolves({ Items: [base] });
    const out = await repo.listLiveSessions();
    expect(out).toHaveLength(1);
    const input = ddbMock.commandCalls(QueryCommand)[0]!.args[0].input;
    expect(input.IndexName).toBe(k.GSI1);
    expect(input.ExpressionAttributeValues![':pk']).toBe(k.LIVE_PARTITION);
    expect(input.ScanIndexForward).toBe(false);
  });
});

describe('org-catalog queries', () => {
  it('queries the single org partition for the catalog', async () => {
    ddbMock.on(QueryCommand).resolves({ Items: [{ name: 'x' }, { name: 'y' }] });
    const out = await repo.listAgents('acme');
    expect(out).toHaveLength(2);
    expect(ddbMock.commandCalls(QueryCommand)).toHaveLength(1); // one org partition
    const input = ddbMock.commandCalls(QueryCommand)[0]!.args[0].input;
    expect(input.ExpressionAttributeValues![':pk']).toBe('SCOPE#org#acme');
    expect(input.ExpressionAttributeValues![':sk']).toBe('AGENT#');
  });
});

describe('listProjectsForUser', () => {
  it('queries GSI1 by user partition', async () => {
    ddbMock.on(QueryCommand).resolves({ Items: [] });
    await repo.listProjectsForUser('matt');
    const input = ddbMock.commandCalls(QueryCommand)[0]!.args[0].input;
    expect(input.IndexName).toBe(k.GSI1);
    expect(input.ExpressionAttributeValues![':pk']).toBe('USER#matt');
  });
});

describe('putDeviceAuth TTL', () => {
  it('writes a numeric epoch-seconds ttl on both the record and the userCode pointer', async () => {
    ddbMock.on(PutCommand).resolves({});
    const expiresAt = 1_717_200_000_000; // epoch ms
    await repo.putDeviceAuth({
      deviceCode: 'dc',
      userCode: 'WDJB-MJXT',
      status: 'pending',
      createdAt: expiresAt - 600_000,
      expiresAt,
    });
    const items = ddbMock
      .commandCalls(PutCommand)
      .map((c) => c.args[0].input.Item as Record<string, unknown>);
    expect(items).toHaveLength(2);
    const expectedTtl = Math.floor(expiresAt / 1000); // epoch SECONDS
    for (const item of items) {
      expect(item.ttl).toBe(expectedTtl);
      expect(typeof item.ttl).toBe('number');
    }
  });
});

describe('getProject', () => {
  it('reads by primary key', async () => {
    ddbMock.on(GetCommand).resolves({ Item: undefined });
    const out = await repo.getProject('weekly-compass');
    expect(out).toBeUndefined();
    expect(ddbMock.commandCalls(GetCommand)[0]!.args[0].input.Key).toEqual(
      k.projectKey('weekly-compass'),
    );
  });
});
