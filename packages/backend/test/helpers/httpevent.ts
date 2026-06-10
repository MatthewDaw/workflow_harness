import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { signDeviceToken } from '../../src/auth/verify.js';

/**
 * Build an HTTP API (v2) event with a Cognito JWT authorizer context, as the
 * REST handlers see it after the authorizer runs. `userId`/`org` populate the
 * `sub` and `custom:org` claims `principalOf` reads. Pass `userId: null` to
 * simulate an unauthenticated request (no authorizer context).
 */
export function httpEvent(opts: {
  method: string;
  userId?: string | null;
  org?: string;
  admin?: boolean;
  rawPath?: string;
  path?: Record<string, string>;
  query?: Record<string, string>;
  headers?: Record<string, string>;
  body?: unknown;
}): APIGatewayProxyEventV2 {
  const claims =
    opts.userId === null || opts.userId === undefined
      ? undefined
      : {
          sub: opts.userId,
          'custom:org': opts.org ?? 'acme',
          ...(opts.admin ? { 'custom:admin': 'true' } : {}),
        };

  return {
    version: '2.0',
    routeKey: '$default',
    rawPath: opts.rawPath ?? '/',
    rawQueryString: '',
    headers: opts.headers ?? {},
    requestContext: {
      http: {
        method: opts.method,
        path: opts.rawPath ?? '/',
        protocol: 'HTTP/1.1',
        sourceIp: '',
        userAgent: '',
      },
      ...(claims ? { authorizer: { jwt: { claims } } } : {}),
    },
    pathParameters: opts.path,
    queryStringParameters: opts.query,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    isBase64Encoded: false,
  } as unknown as APIGatewayProxyEventV2;
}

/** An admin event for the caller's own org (org-catalog writes require admin). */
export function adminEvent(opts: Parameters<typeof httpEvent>[0], org = 'acme') {
  return httpEvent({ org, admin: true, ...opts });
}

/**
 * Build an event authenticated ONLY by an HS256 device token in the bearer
 * header (no Cognito jwt claims) — how the claude+ wrapper calls the
 * HttpNoneAuthorizer routes. Sets DEVICE_TOKEN_SECRET so the handler's
 * resolvePrincipal can verify the token offline.
 */
export async function deviceTokenEvent(opts: {
  method: string;
  userId: string;
  org: string;
  secret?: string;
  path?: Record<string, string>;
  rawPath?: string;
  body?: unknown;
}): Promise<APIGatewayProxyEventV2> {
  const secret = opts.secret ?? 'test-device-secret';
  process.env.DEVICE_TOKEN_SECRET = secret;
  const token = await signDeviceToken(
    { userId: opts.userId, org: opts.org },
    { secret: new TextEncoder().encode(secret) },
  );
  return httpEvent({
    method: opts.method,
    userId: null,
    headers: { authorization: `Bearer ${token}` },
    path: opts.path,
    rawPath: opts.rawPath,
    body: opts.body,
  });
}

/** Parse a handler's JSON result body. */
export function bodyOf<T = unknown>(res: APIGatewayProxyResultV2 | { body?: string }): T {
  const raw = typeof res === 'string' ? res : (res.body ?? '');
  return JSON.parse(raw) as T;
}
