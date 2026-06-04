import type { APIGatewayProxyEventV2 } from 'aws-lambda';
import {
  AwsCognitoVerifier,
  verifyDeviceToken,
  type CognitoVerifier,
  type Principal,
} from '../auth/verify.js';
import { principalOf } from './runtime.js';

/**
 * Principal resolution for routes that may be reached EITHER behind the gateway
 * Cognito JWT authorizer (HQ web) OR with a raw `Authorization: Bearer` token —
 * the claude+ wrapper's HS256 device token — when the route is configured with
 * `HttpNoneAuthorizer` (so the gateway does not pre-reject the non-Cognito token).
 *
 * This mirrors the WebSocket `$connect` authorizer (`ws/authorizer.ts`), which
 * already accepts a device token OR a Cognito ID token. Both verifiers yield the
 * same `Principal` shape, so callers treat the result uniformly.
 */

// Lazily-built Cognito verifier, reused across warm invocations (it caches the
// JWKS internally). Undefined when the pool env vars are absent (e.g. a test).
let cognito: CognitoVerifier | undefined;
function defaultCognito(): CognitoVerifier | undefined {
  if (cognito) return cognito;
  const userPoolId = process.env.USER_POOL_ID;
  const clientId = process.env.USER_POOL_CLIENT_ID;
  if (!userPoolId || !clientId) return undefined;
  cognito = new AwsCognitoVerifier({ userPoolId, clientId, tokenUse: 'id' });
  return cognito;
}

/** Extract the bearer token from the `Authorization` header (case-insensitive). */
function bearerToken(event: APIGatewayProxyEventV2): string | undefined {
  const headers = event.headers ?? {};
  const raw = headers.authorization ?? headers.Authorization;
  if (!raw) return undefined;
  const match = /^Bearer\s+(.+)$/i.exec(raw.trim());
  return match ? match[1]?.trim() : undefined;
}

/** Injectable verifiers so tests can run offline without env/network. */
export interface PrincipalVerifiers {
  verifyDevice?: (token: string) => Promise<Principal>;
  cognito?: CognitoVerifier;
}

/**
 * Resolve the authenticated principal. Order:
 *   1. gateway JWT authorizer claims (`requestContext.authorizer.jwt.claims`) —
 *      the path for routes still behind the Cognito JWT authorizer;
 *   2. `Authorization: Bearer <token>` verified as a device token (HS256);
 *   3. the same bearer token verified as a Cognito ID token.
 * Returns `undefined` when none verify (the caller responds 401).
 */
export async function resolvePrincipal(
  event: APIGatewayProxyEventV2,
  verifiers: PrincipalVerifiers = {},
): Promise<Principal | undefined> {
  const fromClaims = principalOf(event);
  if (fromClaims) return fromClaims;

  const token = bearerToken(event);
  if (!token) return undefined;

  const verifyDevice = verifiers.verifyDevice ?? verifyDeviceToken;
  try {
    return await verifyDevice(token);
  } catch {
    // Not a valid device token — fall through to Cognito.
  }

  const cog = verifiers.cognito ?? defaultCognito();
  if (cog) {
    try {
      return await cog.verify(token);
    } catch {
      // Neither verifier accepted the token.
    }
  }
  return undefined;
}
