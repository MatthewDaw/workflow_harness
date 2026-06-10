import { describe, expect, it } from 'vitest';
import {
  approveDeviceAuth,
  pollDeviceAuth,
  startDeviceAuth,
  DEVICE_AUTH_TTL_MS,
} from '../src/auth/device.js';
import { signDeviceToken, verifyDeviceToken } from '../src/auth/verify.js';
import { memRepoHarness } from './helpers/memtable.js';

/**
 * Offline auth tests. The Repo's DynamoDB calls are served by the shared
 * in-memory table (via aws-sdk-client-mock), which honours the conditional
 * writes the device flow relies on, so the happy path and the "issued once"
 * guarantee are exercised end-to-end without AWS.
 */

const { repo } = memRepoHarness();

const SECRET = new TextEncoder().encode('test-device-secret-please-change');

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

  it('carries the display name in the payload (for the wrapper status line)', async () => {
    const token = await signDeviceToken(
      { userId: 'matt', org: 'acme', name: 'Matthew' },
      { secret: SECRET },
    );
    // The wrapper reads the name claim straight from the (base64url) payload —
    // mirror that here rather than going through verifyDeviceToken (which only
    // returns userId + org).
    const payload = JSON.parse(Buffer.from(token.split('.')[1]!, 'base64url').toString('utf8'));
    expect(payload.name).toBe('Matthew');
    expect(payload.org).toBe('acme');
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
