import { randomBytes, scryptSync, timingSafeEqual } from 'node:crypto';

/**
 * Org-password hashing. Orgs are joined by typing the org name + its password,
 * so the password is a shared secret we must store at rest without keeping the
 * plaintext.
 *
 * We use scrypt (a memory-hard KDF in node:crypto, so NO new dependency) with a
 * per-org random salt: it is deliberately slow + memory-hard, which blunts
 * offline brute-force of a leaked hash far better than a plain SHA. Verification
 * uses `timingSafeEqual` so a comparison can't leak how many leading bytes
 * matched via timing — a constant-time compare closes that side channel.
 */

const SALT_BYTES = 16;
const KEY_LEN = 64;

/** Hash a password: fresh 16-byte hex salt + scrypt-derived 64-byte hex key. */
export function hashOrgPassword(password: string): { salt: string; hash: string } {
  const salt = randomBytes(SALT_BYTES).toString('hex');
  const hash = scryptSync(password, salt, KEY_LEN).toString('hex');
  return { salt, hash };
}

/**
 * Verify a candidate password against a stored salt + hash. We recompute scrypt
 * with the stored salt, then compare in constant time. `timingSafeEqual` throws
 * on unequal-length Buffers, so we guard the length first (a length mismatch is
 * simply "not equal").
 */
export function verifyOrgPassword(password: string, salt: string, hash: string): boolean {
  const candidate = scryptSync(password, salt, KEY_LEN);
  const expected = Buffer.from(hash, 'hex');
  if (candidate.length !== expected.length) return false;
  return timingSafeEqual(candidate, expected);
}
