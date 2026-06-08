import type { APIGatewayRequestAuthorizerEvent, APIGatewayAuthorizerResult } from 'aws-lambda';
import { verifyDeviceToken, AwsCognitoVerifier, type CognitoVerifier } from '../auth/verify.js';

/**
 * WebSocket `$connect` Lambda authorizer (U5/U7). The token arrives on the
 * handshake query string (`?token=...`) because browsers and the daemon cannot
 * set headers on the WS upgrade. TWO kinds of client connect over this socket:
 *
 *  - the wrapper **daemon**, which presents an HS256 wrapper **device token**, and
 *  - the **web** app, which presents the user's RS256 **Cognito ID token**.
 *
 * The authorizer accepts either: it tries the device token first, then falls
 * back to verifying the Cognito JWT. Previously it only verified the device
 * token, so a logged-in web user's live socket was always rejected (403) and the
 * live feed could never stream. The resolved identity (+ `role`) is passed to the
 * connect handler via the authorizer context.
 */

// Lazily-built Cognito verifier, reused across warm invocations (it caches the
// JWKS internally). Undefined when the pool env vars are absent (e.g. a test).
let cognito: CognitoVerifier | undefined;
function cognitoVerifier(): CognitoVerifier | undefined {
  if (cognito) return cognito;
  const userPoolId = process.env.USER_POOL_ID;
  const clientId = process.env.USER_POOL_CLIENT_ID;
  if (!userPoolId || !clientId) return undefined;
  cognito = new AwsCognitoVerifier({ userPoolId, clientId, tokenUse: 'id' });
  return cognito;
}

function policy(
  principalId: string,
  effect: 'Allow' | 'Deny',
  resource: string,
  context: Record<string, string>,
): APIGatewayAuthorizerResult {
  return {
    principalId,
    policyDocument: {
      Version: '2012-10-17',
      Statement: [{ Action: 'execute-api:Invoke', Effect: effect, Resource: resource }],
    },
    context,
  };
}

export const handler = async (
  event: APIGatewayRequestAuthorizerEvent,
): Promise<APIGatewayAuthorizerResult> => {
  const resource = event.methodArn;
  const token = event.queryStringParameters?.token;
  if (!token) {
    console.warn('ws authorizer: denying connection — no token on handshake query string');
    return policy('anonymous', 'Deny', resource, {});
  }
  // 1) Daemon: HS256 wrapper device token.
  let deviceErr: unknown;
  try {
    const principal = await verifyDeviceToken(token);
    return policy(principal.userId, 'Allow', resource, {
      userId: principal.userId,
      org: principal.org,
      role: 'daemon',
    });
  } catch (err) {
    deviceErr = err;
  }

  // 2) Web: RS256 Cognito ID token. The web sends the same token it uses for
  // REST (`?token=<idToken>`); without this, a logged-in web user can never open
  // the live socket and the live feed never streams.
  const verifier = cognitoVerifier();
  if (verifier) {
    try {
      const principal = await verifier.verify(token);
      return policy(principal.userId, 'Allow', resource, {
        userId: principal.userId,
        org: principal.org,
        role: 'web',
      });
    } catch (cognitoErr) {
      console.warn(
        'ws authorizer: denying connection — token is neither a valid device token nor a valid Cognito ID token.',
        'device:',
        deviceErr instanceof Error ? `${deviceErr.name}: ${deviceErr.message}` : String(deviceErr),
        '| cognito:',
        cognitoErr instanceof Error
          ? `${cognitoErr.name}: ${cognitoErr.message}`
          : String(cognitoErr),
      );
      return policy('anonymous', 'Deny', resource, {});
    }
  }

  // No Cognito verifier configured — fall back to reporting the device-token
  // failure (never log the token itself).
  console.warn(
    'ws authorizer: denying connection — device token rejected (no Cognito verifier configured):',
    deviceErr instanceof Error ? `${deviceErr.name}: ${deviceErr.message}` : String(deviceErr),
  );
  return policy('anonymous', 'Deny', resource, {});
};
