import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import type { z } from 'zod';
import {
  createOrgRequestSchema,
  joinOrgRequestSchema,
  setManagerRequestSchema,
  switchOrgRequestSchema,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { hashOrgPassword, verifyOrgPassword } from '../auth/orgPassword.js';
import { seedStarterForOrg } from '../seed/starter.js';
import {
  badRequest,
  created,
  defaultRepo,
  forbidden,
  json,
  ok,
  parseBodySafe,
  INVALID_JSON,
  principalOf,
  unauthorized,
} from './runtime.js';
import { isAdmin } from './scopeauth.js';

/**
 * REST: membership / organizations (org onboarding).
 *
 *   GET  /me          — the caller's identity + REAL org membership (org from the
 *                       PROFILE only; null ⇒ no org ⇒ the web's OrgGate forces
 *                       create/join). NEVER falls back to the token claim here —
 *                       gating must reflect true membership, not the auto-claim.
 *   POST /orgs        — create a new org (name + password); creator becomes admin
 *                       and their profile.org is set to the new name.
 *   POST /orgs/join   — join an existing org by typing its name EXACTLY + password.
 *
 * The org password is a shared secret stored salted+hashed (auth/orgPassword); a
 * join verifies it in constant time. Both create-collision and join-failure use a
 * single generic message so org names can't be enumerated.
 */

export interface OrgsDeps {
  repo: Repo;
}

/**
 * Turn a Zod parse failure into ONE short, human-readable sentence for the UI —
 * never the raw issue JSON. The form only has two fields, so we name them
 * directly and fall back to the first issue's own message for anything else.
 */
function validationMessage(error: z.ZodError): string {
  const issue = error.issues[0];
  if (!issue) return 'Please check your input and try again.';
  if (issue.path[0] === 'name') return 'Please enter an organization name.';
  if (issue.path[0] === 'password') return 'Please enter a password.';
  if (issue.path[0] === 'org') return 'Please choose an organization.';
  return issue.message;
}

/**
 * GET /me — the caller's identity and effective membership for the OrgGate.
 * `org` is read from the PROFILE only (no token fallback): a user whose profile
 * has no org genuinely has none and must onboard, even though their Cognito token
 * still carries the auto-assigned `custom:org`. `admin` is the union of the token
 * admin claim and the profile flag (the org creator is stamped admin on create).
 */
export async function getMe(
  event: APIGatewayProxyEventV2,
  deps: OrgsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const profile = await deps.repo.getUser(principal.userId);
  return ok({
    userId: principal.userId,
    name: principal.name ?? profile?.name,
    org: profile?.org ?? null,
    admin: isAdmin(event) || profile?.admin === true,
    // Every org the user can switch between (legacy single-org profiles read as a
    // one-element set so the switcher still works for them).
    orgs: profile?.orgs ?? (profile?.org ? [profile.org] : []),
    // The caller's manager edge (KTD6), so the UI can offer the manager view to a
    // user who has reports; omitted when unset.
    managerUserId: profile?.managerUserId,
  });
}

/**
 * POST /me/manager — set (or clear, with `managerUserId: null`) a manager edge
 * (KTD6). A plain user may only set their OWN manager (the default target is the
 * caller); an admin may target another `userId` to wire up a report on their
 * behalf. A non-admin targeting someone else is a 403 — a user must not be able
 * to make themselves another user's manager (or reassign a colleague's edge).
 */
export async function setManager(
  event: APIGatewayProxyEventV2,
  deps: OrgsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const parsed = setManagerRequestSchema.safeParse(body ?? {});
  if (!parsed.success) return badRequest(validationMessage(parsed.error));
  const { userId, managerUserId } = parsed.data;

  // Default the target to the caller; only an admin may set another user's edge.
  const target = userId ?? principal.userId;
  if (target !== principal.userId && !isAdmin(event)) return forbidden();

  await deps.repo.setManager(target, managerUserId);
  return ok({ userId: target, managerUserId: managerUserId ?? null });
}

/**
 * POST /orgs — create an org. The name must be unique (it is the join key), so
 * the create is a conditional write; a taken name is a 409. On success the
 * creator becomes the org's first admin and their profile.org is set, ending
 * onboarding. The password is hashed before storage — the plaintext is dropped.
 */
export async function createOrg(
  event: APIGatewayProxyEventV2,
  deps: OrgsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const parsed = createOrgRequestSchema.safeParse(body ?? {});
  if (!parsed.success) return badRequest(validationMessage(parsed.error));
  const { name, password } = parsed.data;

  const { salt, hash } = hashOrgPassword(password);
  const { created: didCreate } = await deps.repo.createOrg({
    name,
    createdBy: principal.userId,
    createdAt: Date.now(),
    passwordSalt: salt,
    passwordHash: hash,
  });
  // Name taken — a generic 409 (the name being unavailable is not a secret, but
  // the message is fixed so it can't be probed for more than existence).
  if (!didCreate) return json(409, { error: 'organization already exists' });

  // The creator is the first admin and is now a member of the org.
  await deps.repo.setUserOrg(principal.userId, name, { name: principal.name, admin: true });

  // Pre-load the product starter bundle so the new org's Skills tab isn't empty.
  // Best-effort: a seeding hiccup must never fail org creation (the catalog can be
  // re-seeded later), so we swallow + log any error.
  try {
    await seedStarterForOrg(deps.repo, name);
  } catch (err) {
    console.warn(`[orgs] starter-skill seed failed for org '${name}':`, err);
  }

  return created({ org: name, admin: true });
}

/**
 * POST /orgs/join — join an existing org. The name must match EXACTLY (after the
 * schema's trim) and the password must verify. A missing org OR a wrong password
 * both return ONE generic 403 message so a caller can't tell which was wrong (no
 * org enumeration). On success the caller's profile.org is set; their admin flag
 * is whatever it already was (a joiner is not auto-admin).
 */
export async function joinOrg(
  event: APIGatewayProxyEventV2,
  deps: OrgsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const parsed = joinOrgRequestSchema.safeParse(body ?? {});
  if (!parsed.success) return badRequest(validationMessage(parsed.error));
  const { name, password } = parsed.data;

  const org = await deps.repo.getOrg(name);
  // Missing org OR wrong password → the SAME generic message (avoid enumeration).
  if (!org || !verifyOrgPassword(password, org.passwordSalt, org.passwordHash)) {
    return json(403, { error: 'invalid organization name or password' });
  }

  // Preserve any pre-existing admin flag (e.g. a user who created another org
  // earlier); joining does not grant admin on the joined org.
  const existing = await deps.repo.getUser(principal.userId);
  await deps.repo.setUserOrg(principal.userId, name, { name: principal.name });
  return ok({ org: name, admin: existing?.admin === true });
}

/**
 * POST /me/org — switch the ACTIVE org to another one the caller has already
 * joined. No password (this is not a join, just changing which membership the
 * app is scoped to); the repo verifies membership and 403s otherwise. The web
 * resets its whole cache on success so every dataset re-reads under the new org.
 */
export async function switchOrg(
  event: APIGatewayProxyEventV2,
  deps: OrgsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const parsed = switchOrgRequestSchema.safeParse(body ?? {});
  if (!parsed.success) return badRequest(validationMessage(parsed.error));
  const { org } = parsed.data;

  const { switched } = await deps.repo.switchActiveOrg(principal.userId, org);
  if (!switched) return json(403, { error: 'you are not a member of that organization' });

  const profile = await deps.repo.getUser(principal.userId);
  return ok({ org, admin: profile?.admin === true });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: OrgsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path ?? event.rawPath ?? '';

  // Order matters: the more specific suffixes are checked before bare /orgs.
  if (method === 'POST' && path.endsWith('/me/manager')) return setManager(event, deps);
  if (method === 'POST' && path.endsWith('/me/org')) return switchOrg(event, deps);
  if (method === 'POST' && path.endsWith('/join')) return joinOrg(event, deps);
  if (method === 'POST') return createOrg(event, deps);
  return getMe(event, deps); // GET /me
}
