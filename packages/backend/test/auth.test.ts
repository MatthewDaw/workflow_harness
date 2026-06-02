import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import {
  DynamoDBDocumentClient,
  GetCommand,
  PutCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import { Repo } from '../src/db/repo.js';
import {
  approveDeviceAuth,
  pollDeviceAuth,
  startDeviceAuth,
  DEVICE_AUTH_TTL_MS,
} from '../src/auth/device.js';
import { signDeviceToken, verifyDeviceToken } from '../src/auth/verify.js';

/**
 * Offline auth tests. The Repo's DynamoDB calls are served by a small in-memory
 * table (via aws-sdk-client-mock) that honours the conditional writes the
 * device flow relies on, so the happy path and the "issued once" guarantee are
 * exercised end-to-end without AWS.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

const SECRET = new TextEncoder().encode('test-device-secret-please-change');

interface KeyShape {
  PK: string;
  SK: string;
}
const keyOf = (item: KeyShape): string => `${item.PK}::${item.SK}`;

class ConditionalCheckFailed extends Error {
  override name = 'ConditionalCheckFailedException';
}

/** Wire the mock to a stateful in-memory store with conditional-write support. */
function installInMemoryTable(): Map<string, Record<string, unknown>> {
  const store = new Map<string, Record<string, unknown>>();

  ddbMock.on(PutCommand).callsFake((input) => {
    const item = input.Item as Record<string, unknown> & KeyShape;
    if (input.ConditionExpression?.includes('attribute_not_exists(PK)')) {
      if (store.has(keyOf(item))) throw new ConditionalCheckFailed();
    }
    store.set(keyOf(item), { ...item });
    return {};
  });

  ddbMock.on(GetCommand).callsFake((input) => {
    const key = input.Key as KeyShape;
    return { Item: store.get(keyOf(key)) };
  });

  ddbMock.on(UpdateCommand).callsFake((input) => {
    const key = input.Key as KeyShape;
    const existing = store.get(keyOf(key));
    const values = (input.ExpressionAttributeValues ?? {}) as Record<string, unknown>;
    const cond = input.ConditionExpression ?? '';
    if (cond.includes('attribute_exists(PK)') && !existing) {
      throw new ConditionalCheckFailed();
    }
    // We only ever guard on the status attribute: `#s = :pending|:approved`.
    if (existing && cond.includes('#s =')) {
      const guard = cond.includes(':pending') ? values[':pending'] : values[':approved'];
      if (existing.status !== guard) throw new ConditionalCheckFailed();
    }
    const next = { ...(existing ?? key) };
    if (values[':approved'] !== undefined && cond.includes(':pending')) {
      next.status = 'approved';
      next.userId = values[':u'];
      next.org = values[':o'];
    } else if (values[':consumed'] !== undefined) {
      next.status = 'consumed';
    }
    store.set(keyOf(key), next);
    return {};
  });

  return store;
}

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable();
});

describe('device-code flow', () => {
  it('happy path: start -> poll pending -> approve -> poll returns token', async () => {
    const { deviceCode, userCode } = await startDeviceAuth(repo, { secret: SECRET });
    expect(deviceCode).toBeTruthy();
    expect(userCode).toMatch(/^[A-Z2-9]{4}-[A-Z2-9]{4}$/);

    const before = await pollDeviceAuth(repo, deviceCode, { secret: SECRET });
    expect(before).toEqual({ status: 'pending' });

    const approval = await approveDeviceAuth(repo, {
      deviceCode,
      userId: 'matt',
      org: 'acme',
    });
    expect(approval.approved).toBe(true);

    const after = await pollDeviceAuth(repo, deviceCode, { secret: SECRET });
    expect(after.status).toBe('token');
    if (after.status !== 'token') throw new Error('expected token');

    const principal = await verifyDeviceToken(after.token, { secret: SECRET });
    expect(principal).toEqual({ userId: 'matt', org: 'acme' });
  });

  it('poll before approval is pending', async () => {
    const { deviceCode } = await startDeviceAuth(repo, { secret: SECRET });
    expect(await pollDeviceAuth(repo, deviceCode, { secret: SECRET })).toEqual({
      status: 'pending',
    });
    expect(await pollDeviceAuth(repo, deviceCode, { secret: SECRET })).toEqual({
      status: 'pending',
    });
  });

  it('approved token is only issued once', async () => {
    const { deviceCode } = await startDeviceAuth(repo, { secret: SECRET });
    await approveDeviceAuth(repo, { deviceCode, userId: 'matt', org: 'acme' });

    const first = await pollDeviceAuth(repo, deviceCode, { secret: SECRET });
    expect(first.status).toBe('token');

    const second = await pollDeviceAuth(repo, deviceCode, { secret: SECRET });
    expect(second.status).not.toBe('token');
  });

  it('a second approval is a no-op (idempotent approve)', async () => {
    const { deviceCode } = await startDeviceAuth(repo, { secret: SECRET });
    expect(
      (await approveDeviceAuth(repo, { deviceCode, userId: 'matt', org: 'acme' })).approved,
    ).toBe(true);
    expect(
      (await approveDeviceAuth(repo, { deviceCode, userId: 'eve', org: 'evil' })).approved,
    ).toBe(false);
    const res = await pollDeviceAuth(repo, deviceCode, { secret: SECRET });
    if (res.status !== 'token') throw new Error('expected token');
    // The first approver wins.
    expect(await verifyDeviceToken(res.token, { secret: SECRET })).toEqual({
      userId: 'matt',
      org: 'acme',
    });
  });

  it('polling an unknown device code returns unknown', async () => {
    expect(await pollDeviceAuth(repo, 'nope', { secret: SECRET })).toEqual({ status: 'unknown' });
  });

  it('an expired pending record reports expired', async () => {
    let clock = 1_000_000;
    const now = () => clock;
    const { deviceCode } = await startDeviceAuth(repo, { secret: SECRET, now });
    clock += DEVICE_AUTH_TTL_MS + 1;
    expect(await pollDeviceAuth(repo, deviceCode, { secret: SECRET, now })).toEqual({
      status: 'expired',
    });
  });
});

describe('device token verification', () => {
  it('verifyDeviceToken round-trips a signed token', async () => {
    const token = await signDeviceToken({ userId: 'matt', org: 'acme' }, { secret: SECRET });
    expect(await verifyDeviceToken(token, { secret: SECRET })).toEqual({
      userId: 'matt',
      org: 'acme',
    });
  });

  it('rejects a forged token (wrong secret)', async () => {
    const token = await signDeviceToken({ userId: 'matt', org: 'acme' }, { secret: SECRET });
    const wrong = new TextEncoder().encode('a-different-secret-entirely-xxxx');
    await expect(verifyDeviceToken(token, { secret: wrong })).rejects.toThrow();
  });

  it('rejects an expired token', async () => {
    const token = await signDeviceToken(
      { userId: 'matt', org: 'acme' },
      { secret: SECRET, expiresIn: '-1s' },
    );
    await expect(verifyDeviceToken(token, { secret: SECRET })).rejects.toThrow();
  });

  it('rejects a structurally invalid token', async () => {
    await expect(verifyDeviceToken('not-a-jwt', { secret: SECRET })).rejects.toThrow();
  });
});
