import { randomBytes } from 'node:crypto';
import type { DeviceAuth } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { signDeviceToken } from './verify.js';

/**
 * The device-code login flow used by the claude+ wrapper (`claude+ login`).
 *
 *   start   -> { deviceCode, userCode }   (wrapper shows userCode to the human)
 *   approve -> binds the record to a signed-in HQ user (called from HQ web)
 *   poll    -> 'pending' until approved, then mints the signed wrapper token
 *              exactly once.
 *
 * Records persist through the Repo so the three steps can hit different Lambda
 * invocations. Token signing/verification lives in ./verify.ts.
 */

/** Default lifetime of a pending device-auth record (10 minutes). */
export const DEVICE_AUTH_TTL_MS = 10 * 60 * 1000;

const USER_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; // no ambiguous 0/O/1/I

/** Random URL-safe device code (opaque, looked up directly on poll). */
function generateDeviceCode(): string {
  return randomBytes(32).toString('base64url');
}

/** Short, human-typable code shown by the wrapper, e.g. `WDJB-MJXT`. */
function generateUserCode(): string {
  const pick = (): string => {
    const idx = randomBytes(1)[0]! % USER_CODE_ALPHABET.length;
    return USER_CODE_ALPHABET[idx]!;
  };
  const block = (): string => Array.from({ length: 4 }, pick).join('');
  return `${block()}-${block()}`;
}

export interface DeviceFlowOptions {
  /** Injectable clock for deterministic tests. */
  now?: () => number;
  /** Injectable signing secret (defaults to the env-backed secret). */
  secret?: Uint8Array;
  /** Lifetime override for the pending record. */
  ttlMs?: number;
}

export interface StartResult {
  deviceCode: string;
  userCode: string;
  expiresAt: number;
}

export type PollResult =
  | { status: 'pending' }
  | { status: 'expired' }
  | { status: 'token'; token: string }
  | { status: 'unknown' };

/**
 * Begin a device-code login: persist a pending record and return the codes the
 * wrapper polls / the human approves against.
 */
export async function startDeviceAuth(
  repo: Repo,
  opts: DeviceFlowOptions = {},
): Promise<StartResult> {
  const now = opts.now ?? Date.now;
  const createdAt = now();
  const expiresAt = createdAt + (opts.ttlMs ?? DEVICE_AUTH_TTL_MS);
  const record: DeviceAuth = {
    deviceCode: generateDeviceCode(),
    userCode: generateUserCode(),
    status: 'pending',
    createdAt,
    expiresAt,
  };
  await repo.putDeviceAuth(record);
  return { deviceCode: record.deviceCode, userCode: record.userCode, expiresAt };
}

/**
 * Approve a pending device-auth (called from HQ once the signed-in user
 * confirms the userCode). Binds the record to the approving identity. Returns
 * whether the approval took effect (false if not found or already approved).
 */
export async function approveDeviceAuth(
  repo: Repo,
  args: { deviceCode: string; userId: string; org: string; name?: string },
): Promise<{ approved: boolean }> {
  return repo.approveDeviceAuth(args.deviceCode, args.userId, args.org, { name: args.name });
}

/**
 * Poll a device-auth. Returns `pending` until approved; on the first poll after
 * approval, atomically consumes the record and mints the signed wrapper token.
 * Subsequent polls see a consumed record and report `pending` is over — they
 * never re-issue a token.
 */
/**
 * Normalize a human-entered user_code to the stored canonical form: strip
 * spaces/dashes, uppercase, and re-insert the dash at the 4-char boundary, so
 * `wdjbmjxt`, `wdjb-mjxt`, and `WDJB MJXT` all resolve to `WDJB-MJXT`.
 */
export function normalizeUserCode(input: string): string {
  const compact = input.replace(/[^a-zA-Z0-9]/g, '').toUpperCase();
  if (compact.length === 8) return `${compact.slice(0, 4)}-${compact.slice(4)}`;
  return compact;
}

/**
 * Approve a device-auth by its human-typed user_code (the form HQ's "link a
 * device" screen submits). Resolves the user_code to its device_code via the
 * pointer item, then binds the record to the approving identity. Returns
 * `approved: false` for an unknown/expired code or an already-approved record.
 */
export async function approveDeviceAuthByUserCode(
  repo: Repo,
  args: { userCode: string; userId: string; org: string; name?: string },
  opts: DeviceFlowOptions = {},
): Promise<{ approved: boolean }> {
  const now = opts.now ?? Date.now;
  const deviceCode = await repo.getDeviceCodeByUserCode(normalizeUserCode(args.userCode));
  if (!deviceCode) return { approved: false };
  // Reject an expired record up front: otherwise an approval would flip a
  // timed-out record to `approved`, and a subsequent poll (which only treated
  // `pending` records as expired) would mint a token for it. The storage-layer
  // condition below is the defense-in-depth backstop.
  const record = await repo.getDeviceAuth(deviceCode);
  if (!record || now() >= record.expiresAt) return { approved: false };
  return repo.approveDeviceAuth(deviceCode, args.userId, args.org, { now: now(), name: args.name });
}

export async function pollDeviceAuth(
  repo: Repo,
  deviceCode: string,
  opts: DeviceFlowOptions = {},
): Promise<PollResult> {
  const now = opts.now ?? Date.now;
  const record = await repo.getDeviceAuth(deviceCode);
  if (!record) return { status: 'unknown' };
  // Expired any time before the token is consumed — including an `approved`
  // record whose window elapsed before the first poll — so expiry can't be
  // bypassed by approving late.
  if (now() >= record.expiresAt && record.status !== 'consumed') {
    return { status: 'expired' };
  }
  if (record.status === 'pending') return { status: 'pending' };
  if (record.status === 'consumed') return { status: 'unknown' };

  // status === 'approved': claim it exactly once.
  const { consumed } = await repo.consumeDeviceAuth(deviceCode);
  if (!consumed) {
    // Lost the race — another poll already minted the token.
    return { status: 'unknown' };
  }
  if (!record.userId || !record.org) {
    throw new Error('approved device-auth record missing identity');
  }
  const token = await signDeviceToken(
    { userId: record.userId, org: record.org, ...(record.name ? { name: record.name } : {}) },
    { secret: opts.secret },
  );
  return { status: 'token', token };
}
