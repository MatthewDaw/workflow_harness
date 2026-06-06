import type { APIGatewayProxyEventV2 } from 'aws-lambda';
import type { Repo } from '../db/repo.js';
import { principalOf } from './runtime.js';

/**
 * The EFFECTIVE org a request's data should be scoped to. Membership now lives
 * on the PROFILE record (the source of truth — `profile.org`), but we fall back
 * to the token's `custom:org` claim when the profile has none.
 *
 * Why the token fallback exists:
 *  - Back-compat: every existing handler test seeds NO profile but does set the
 *    claim org, so without the fallback those tests would suddenly see no org.
 *    Real onboarded users always have `profile.org`, so the fallback never wins
 *    for them — it only covers the legacy/claim-only path.
 *  - Device tokens: the claude+ wrapper's device token carries the member's org
 *    as a claim (not a profile lookup), so the fallback keeps those scoped too.
 *
 * Returns undefined only when there is no principal at all (unauthenticated) —
 * a genuinely org-less but authenticated user surfaces their (possibly absent)
 * profile.org, which `GET /me` reads directly to drive the OrgGate.
 */
export async function effectiveOrg(
  event: APIGatewayProxyEventV2,
  repo: Repo,
): Promise<string | undefined> {
  const p = principalOf(event);
  if (!p) return undefined;
  const profile = await repo.getUser(p.userId);
  return profile?.org ?? p.org;
}
