import type {
  APIGatewayRequestAuthorizerEvent,
  APIGatewayAuthorizerResult,
} from 'aws-lambda';
import { verifyDeviceToken } from '../auth/verify.js';

/**
 * WebSocket `$connect` Lambda authorizer (U5/U7). The token arrives on the
 * handshake query string (`?token=...`) because browsers and the daemon cannot
 * set headers on the WS upgrade. A valid wrapper device token authorizes the
 * connection; anything else is denied. The resolved identity is passed to the
 * connect handler via the authorizer context.
 */
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
  if (!token) return policy('anonymous', 'Deny', resource, {});
  try {
    const principal = await verifyDeviceToken(token);
    return policy(principal.userId, 'Allow', resource, {
      userId: principal.userId,
      org: principal.org,
      role: 'daemon',
    });
  } catch {
    return policy('anonymous', 'Deny', resource, {});
  }
};
