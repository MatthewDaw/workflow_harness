import { beforeEach, describe, expect, it } from 'vitest';
import { deviceApprove, devicePoll, deviceStart } from '../src/rest/device.js';
import {
  approveDeviceAuthByUserCode,
  pollDeviceAuth,
  startDeviceAuth,
} from '../src/auth/device.js';
import { memRepoHarness } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * Device-code login (`claude+ login`): start/poll are public; approve is gated by
 * the JWT authorizer. A token is minted exactly once, after an authenticated
 * approval of the human user_code.
 */

const { repo } = memRepoHarness();
const deps = { repo };

beforeEach(() => {
  process.env.DEVICE_TOKEN_SECRET = 'test-secret';
});

describe('device-code login', () => {
  it('start returns a deviceCode + userCode + poll interval', async () => {
    const res = await deviceStart(httpEvent({ method: 'POST' }), deps);
    const body = bodyOf<{
      deviceCode: string;
      userCode: string;
      expiresAt: number;
      interval: number;
    }>(res);
    expect(body.deviceCode).toBeTruthy();
    expect(body.userCode).toMatch(/^[A-Z0-9]{4}-[A-Z0-9]{4}$/);
    expect(body.expiresAt).toBeGreaterThan(0);
    expect(body.interval).toBe(5);
  });

  it('poll returns 400 (not 500) on a malformed JSON body', async () => {
    const malformed = httpEvent({ method: 'POST' });
    (malformed as { body?: string }).body = '{ not json';
    const res = await devicePoll(malformed, deps);
    expect(res.statusCode).toBe(400);
  });

  it('approve returns 400 (not 500) on a malformed JSON body', async () => {
    const malformed = httpEvent({ method: 'POST', userId: 'matt', org: 'acme' });
    (malformed as { body?: string }).body = '{ not json';
    const res = await deviceApprove(malformed, deps);
    expect(res.statusCode).toBe(400);
  });

  it('poll requires a deviceCode (400) and reports unknown codes', async () => {
    const bad = await devicePoll(httpEvent({ method: 'POST', body: {} }), deps);
    expect(bad.statusCode).toBe(400);

    const unknown = await devicePoll(
      httpEvent({ method: 'POST', body: { deviceCode: 'nope' } }),
      deps,
    );
    expect(bodyOf<{ status: string }>(unknown).status).toBe('unknown');
  });

  it('approve is unauthenticated-rejected (401) and no-ops for an unknown userCode', async () => {
    const noAuth = await deviceApprove(
      httpEvent({ method: 'POST', userId: null, body: { userCode: 'WDJB-MJXT' } }),
      deps,
    );
    expect(noAuth.statusCode).toBe(401);

    const unknown = await deviceApprove(
      httpEvent({ method: 'POST', userId: 'matt', org: 'acme', body: { userCode: 'ZZZZ-ZZZZ' } }),
      deps,
    );
    expect(bodyOf<{ approved: boolean }>(unknown).approved).toBe(false);
  });

  it('mints a token exactly once after an authenticated approval', async () => {
    // start
    const start = bodyOf<{ deviceCode: string; userCode: string }>(
      await deviceStart(httpEvent({ method: 'POST' }), deps),
    );

    // poll before approval -> pending
    const pending = await devicePoll(
      httpEvent({ method: 'POST', body: { deviceCode: start.deviceCode } }),
      deps,
    );
    expect(bodyOf<{ status: string }>(pending).status).toBe('pending');

    // approve by userCode (lowercased + no dash still resolves) as a signed-in user
    const approve = await deviceApprove(
      httpEvent({
        method: 'POST',
        userId: 'matt',
        org: 'acme',
        body: { userCode: start.userCode.replace('-', '').toLowerCase() },
      }),
      deps,
    );
    expect(bodyOf<{ approved: boolean }>(approve).approved).toBe(true);

    // first poll after approval -> token
    const minted = bodyOf<{ status: string; token?: string }>(
      await devicePoll(httpEvent({ method: 'POST', body: { deviceCode: start.deviceCode } }), deps),
    );
    expect(minted.status).toBe('token');
    expect(minted.token).toBeTruthy();

    // second poll -> token is not re-issued
    const again = bodyOf<{ status: string }>(
      await devicePoll(httpEvent({ method: 'POST', body: { deviceCode: start.deviceCode } }), deps),
    );
    expect(again.status).not.toBe('token');
  });

  it('refuses to approve an expired record, so no token is minted (security)', async () => {
    // Create a record that is already past its window, then approve + poll well
    // after expiry. Approval must be refused and poll must report expired —
    // never mint a token (regression for the expired-credential-acceptance bug).
    const created = await startDeviceAuth(repo, { now: () => 1_000, ttlMs: 10 }); // expires at 1010
    const late = () => 5_000;

    const approved = await approveDeviceAuthByUserCode(
      repo,
      { userCode: created.userCode, userId: 'matt', org: 'acme' },
      { now: late },
    );
    expect(approved.approved).toBe(false);

    const poll = await pollDeviceAuth(repo, created.deviceCode, { now: late });
    expect(poll.status).toBe('expired');
  });
});
