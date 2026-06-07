import { SignJWT, jwtVerify } from 'jose';
import { CognitoJwtVerifier } from 'aws-jwt-verify';

/**
 * Token verification for the two authentication paths:
 *
 *  - HQ web users authenticate with Cognito-issued JWTs, verified by
 *    `CognitoVerifier` (a thin, injectable wrapper over `aws-jwt-verify`).
 *  - The claude+ wrapper authenticates with a long-lived "device token" — a
 *    JWT we mint ourselves (HS256) in the device-code flow — verified here by
 *    `verifyDeviceToken`. Both yield the same authenticated principal shape so
 *    handlers can treat them uniformly.
 */

/** The authenticated principal a handler receives once a token is verified. */
export interface Principal {
  userId: string;
  org: string;
  /** Display name for authorship stamps (createdBy). Optional; falls back to userId. */
  name?: string;
}

export const DEVICE_TOKEN_AUDIENCE = 'claude-plus-wrapper';
export const DEVICE_TOKEN_ISSUER = 'command-hq';

/** Read the HS256 signing secret for device tokens from the environment. */
export function deviceTokenSecret(): Uint8Array {
  const secret = process.env.DEVICE_TOKEN_SECRET;
  if (!secret) {
    throw new Error('DEVICE_TOKEN_SECRET is not set');
  }
  return new TextEncoder().encode(secret);
}

/**
 * Mint a signed wrapper token scoped to a user/org. Long-lived by design (the
 * wrapper runs unattended on remote hosts); rotation is handled by re-running
 * the device-code flow. Returns the compact JWS string.
 */
export async function signDeviceToken(
  principal: Principal,
  opts: { expiresIn?: string; secret?: Uint8Array } = {},
): Promise<string> {
  const secret = opts.secret ?? deviceTokenSecret();
  // `name` is carried so the wrapper can show the signed-in username in its status
  // line (the org rides alongside); both are display-only — authz uses sub + org.
  return new SignJWT({ org: principal.org, ...(principal.name ? { name: principal.name } : {}) })
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(principal.userId)
    .setIssuer(DEVICE_TOKEN_ISSUER)
    .setAudience(DEVICE_TOKEN_AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(opts.expiresIn ?? '180d')
    .sign(secret);
}

/**
 * Verify a wrapper device token and resolve its principal. Throws on an
 * expired, forged, or malformed token (jose rejects bad signatures/exp). The
 * secret is injectable for tests.
 */
export async function verifyDeviceToken(
  token: string,
  opts: { secret?: Uint8Array } = {},
): Promise<Principal> {
  const secret = opts.secret ?? deviceTokenSecret();
  const { payload } = await jwtVerify(token, secret, {
    issuer: DEVICE_TOKEN_ISSUER,
    audience: DEVICE_TOKEN_AUDIENCE,
  });
  const userId = payload.sub;
  const org = payload.org;
  if (typeof userId !== 'string' || typeof org !== 'string') {
    throw new Error('device token missing userId/org claims');
  }
  return { userId, org };
}

/**
 * Verifies Cognito-issued JWTs for HQ web. Network access (JWKS fetch) is kept
 * behind this interface so handlers depend on the abstraction and tests can
 * supply an offline fake.
 */
export interface CognitoVerifier {
  verify(token: string): Promise<Principal>;
}

export interface CognitoVerifierConfig {
  userPoolId: string;
  clientId: string;
  /** 'id' for an ID token, 'access' for an access token. */
  tokenUse?: 'id' | 'access';
  /** Custom claim carrying the org; defaults to `custom:org`. */
  orgClaim?: string;
}

/**
 * Production implementation backed by `aws-jwt-verify`. The verifier object is
 * created lazily and caches the JWKS internally; the only network call is the
 * one-time JWKS fetch that library performs.
 */
export class AwsCognitoVerifier implements CognitoVerifier {
  private readonly orgClaim: string;
  private readonly verifier: ReturnType<typeof CognitoJwtVerifier.create>;

  constructor(config: CognitoVerifierConfig) {
    this.orgClaim = config.orgClaim ?? 'custom:org';
    this.verifier = CognitoJwtVerifier.create({
      userPoolId: config.userPoolId,
      clientId: config.clientId,
      tokenUse: config.tokenUse ?? 'id',
    });
  }

  async verify(token: string): Promise<Principal> {
    const payload = await this.verifier.verify(token);
    const userId = typeof payload.sub === 'string' ? payload.sub : undefined;
    const orgValue = payload[this.orgClaim];
    const org = typeof orgValue === 'string' ? orgValue : undefined;
    if (!userId || !org) {
      throw new Error('Cognito token missing sub/org claims');
    }
    return { userId, org };
  }
}
