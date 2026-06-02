import type { APIGatewayProxyEventV2 } from 'aws-lambda';

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
  path?: Record<string, string>;
  query?: Record<string, string>;
  body?: unknown;
}): APIGatewayProxyEventV2 {
  const claims =
    opts.userId === null || opts.userId === undefined
      ? undefined
      : { sub: opts.userId, 'custom:org': opts.org ?? 'acme' };

  return {
    version: '2.0',
    routeKey: '$default',
    rawPath: '/',
    rawQueryString: '',
    headers: {},
    requestContext: {
      http: { method: opts.method, path: '/', protocol: 'HTTP/1.1', sourceIp: '', userAgent: '' },
      ...(claims ? { authorizer: { jwt: { claims } } } : {}),
    },
    pathParameters: opts.path,
    queryStringParameters: opts.query,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    isBase64Encoded: false,
  } as unknown as APIGatewayProxyEventV2;
}

/** Parse a handler's JSON result body. */
export function bodyOf<T = unknown>(res: { body?: string } | string): T {
  const raw = typeof res === 'string' ? res : (res.body ?? '');
  return JSON.parse(raw) as T;
}
