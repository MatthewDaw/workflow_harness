import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { startDeviceAuth, pollDeviceAuth, approveDeviceAuthByUserCode } from '../auth/device.js';
import type { Repo } from '../db/repo.js';
import { badRequest, defaultRepo, ok, parseBody, principalOf, unauthorized } from './runtime.js';

/**
 * REST: device-code login for the claude+ wrapper (`claude+ login`).
 *
 *   POST /device/start    (public) -> { deviceCode, userCode, expiresAt, interval }
 *   POST /device/poll     (public) -> { status, token? }   body: { deviceCode }
 *   POST /device/approve  (JWT)    -> { approved }          body: { userCode }
 *
 * start/poll are intentionally unauthenticated — the device has no token yet.
 * Security rests on the opaque 32-byte deviceCode and the authenticated approve
 * step (an HQ-signed-in user must confirm the short userCode before any token is
 * minted). approve is gated by the Cognito JWT authorizer.
 */

export interface DeviceDeps {
  repo: Repo;
}

/** Recommended client poll interval, in seconds. */
const POLL_INTERVAL_SECONDS = 5;

export async function deviceStart(
  _event: APIGatewayProxyEventV2,
  deps: DeviceDeps,
): Promise<APIGatewayProxyResultV2> {
  const { deviceCode, userCode, expiresAt } = await startDeviceAuth(deps.repo);
  return ok({ deviceCode, userCode, expiresAt, interval: POLL_INTERVAL_SECONDS });
}

export async function devicePoll(
  event: APIGatewayProxyEventV2,
  deps: DeviceDeps,
): Promise<APIGatewayProxyResultV2> {
  const body = parseBody(event) as { deviceCode?: string } | undefined;
  const deviceCode = body?.deviceCode;
  if (!deviceCode) return badRequest('deviceCode required');
  const result = await pollDeviceAuth(deps.repo, deviceCode);
  return ok(result);
}

export async function deviceApprove(
  event: APIGatewayProxyEventV2,
  deps: DeviceDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const body = parseBody(event) as { userCode?: string } | undefined;
  const userCode = body?.userCode;
  if (!userCode) return badRequest('userCode required');
  const result = await approveDeviceAuthByUserCode(deps.repo, {
    userCode,
    userId: principal.userId,
    org: principal.org,
  });
  return ok(result);
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: DeviceDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const rawPath = event.requestContext.http.path ?? event.rawPath ?? '';
  if (method === 'POST' && /\/start$/.test(rawPath)) return deviceStart(event, deps);
  if (method === 'POST' && /\/poll$/.test(rawPath)) return devicePoll(event, deps);
  if (method === 'POST' && /\/approve$/.test(rawPath)) return deviceApprove(event, deps);
  return badRequest('unknown device route');
}
