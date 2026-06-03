import type { ScopeRef } from '@harness/shared';

/**
 * Single-table key design for the `harness` table. Every entity is addressed by
 * an overloaded PK/SK; GSI1 carries two cross-cutting queries (live sessions,
 * and a user's projects). Key builders are pure so they can be unit-tested
 * without touching DynamoDB.
 */

export const DEFAULT_TABLE_NAME = 'harness';
export const tableName = (): string => process.env.HARNESS_TABLE ?? DEFAULT_TABLE_NAME;

export const GSI1 = 'GSI1';
/** Partition value on GSI1 that gathers all currently-live sessions. */
export const LIVE_PARTITION = 'LIVE';

const pad = (n: number, width: number): string => String(Math.trunc(n)).padStart(width, '0');
const TS_WIDTH = 15; // epoch ms, comfortably future-proof
const SEQ_WIDTH = 12;

export interface PrimaryKey {
  PK: string;
  SK: string;
}

/** `org#acme` | `user#matt` | `proj#weekly-compass` */
export function scopeId(scope: ScopeRef): string {
  const prefix = scope.tier === 'project' ? 'proj' : scope.tier;
  return `${prefix}#${scope.id}`;
}

export const userKey = (userId: string): PrimaryKey => ({ PK: `USER#${userId}`, SK: 'PROFILE' });

export const projectKey = (projectId: string): PrimaryKey => ({
  PK: `PROJ#${projectId}`,
  SK: 'META',
});

export const instanceKey = (projectId: string, host: string): PrimaryKey => ({
  PK: `PROJ#${projectId}`,
  SK: `INST#${host}`,
});

/**
 * HQ-owned high-level requirements markdown for a project (U10). HQ is the
 * source of truth for this doc (unlike the GitHub-sourced detailed-requirements
 * tree), so it lives in the project partition under a fixed meta SK.
 */
export const projectRequirementsKey = (projectId: string): PrimaryKey => ({
  PK: `PROJ#${projectId}`,
  SK: 'REQUIREMENTS',
});

export const sessionKey = (projectId: string, sessionId: string): PrimaryKey => ({
  PK: `PROJ#${projectId}`,
  SK: `SESS#${sessionId}`,
});

/**
 * A direct sessionId -> projectId pointer. Session projections live under their
 * project partition, but event ingestion and the control gateway often hold only
 * a sessionId (events after `session.start` carry no projectId). This tiny
 * record lets them resolve the projection in one extra get.
 */
export const sessionPointerKey = (sessionId: string): PrimaryKey => ({
  PK: `SESS#${sessionId}`,
  SK: 'PTR',
});

/** Events sort by seq (monotonic per session) so the SK is the dedupe key on (sessionId, seq). */
export const eventKey = (sessionId: string, seq: number): PrimaryKey => ({
  PK: `SESS#${sessionId}`,
  SK: `EVT#${pad(seq, SEQ_WIDTH)}`,
});

export const eventPrefix = (sessionId: string): { PK: string; skPrefix: string } => ({
  PK: `SESS#${sessionId}`,
  skPrefix: 'EVT#',
});

export const agentKey = (scope: ScopeRef, name: string): PrimaryKey => ({
  PK: `SCOPE#${scopeId(scope)}`,
  SK: `AGENT#${name}`,
});

export const skillKey = (scope: ScopeRef, name: string): PrimaryKey => ({
  PK: `SCOPE#${scopeId(scope)}`,
  SK: `SKILL#${name}`,
});

export const scopePartition = (scope: ScopeRef): string => `SCOPE#${scopeId(scope)}`;

export const objectiveKey = (org: string, path: string): PrimaryKey => ({
  PK: `ORG#${org}`,
  SK: `RCDO#${path}`,
});

export const weeklyKey = (projectId: string, isoWeek: string): PrimaryKey => ({
  PK: `PROJ#${projectId}`,
  SK: `WEEK#${isoWeek}`,
});

/**
 * Pending device-authorization record for the wrapper's device-code login flow.
 * Keyed by the opaque device code so a `poll` can look it up directly; the
 * short user_code is carried as an attribute (the human approves against it).
 */
export const deviceAuthKey = (deviceCode: string): PrimaryKey => ({
  PK: `DEVAUTH#${deviceCode}`,
  SK: 'PENDING',
});

/**
 * WebSocket connection registry (U6/U7). Two record families share the table:
 *
 *  - A daemon (or web) connection record, keyed by `connectionId`, carries the
 *    authenticated `{uid, org}` and (for daemons) the `instanceId` they host.
 *    `$disconnect` deletes it.
 *  - A reverse index from `instanceId` to its current daemon `connectionId`, so
 *    the control gateway can route a steer frame to the owning daemon without a
 *    scan. Overwritten on reconnect; removed on disconnect.
 */
export const connectionKey = (connectionId: string): PrimaryKey => ({
  PK: `CONN#${connectionId}`,
  SK: 'META',
});

export const instanceConnKey = (instanceId: string): PrimaryKey => ({
  PK: `INSTCONN#${instanceId}`,
  SK: 'CURRENT',
});

/**
 * A web client's subscription to a session's live feed. Keyed under the session
 * so ingestion can list listeners for fan-out; the SK carries the listener's
 * `connectionId` so a subscriber disconnect can delete its own record directly.
 */
export const listenerKey = (sessionId: string, connectionId: string): PrimaryKey => ({
  PK: `SESSLISTEN#${sessionId}`,
  SK: `CONN#${connectionId}`,
});

export const listenerPrefix = (sessionId: string): { PK: string; skPrefix: string } => ({
  PK: `SESSLISTEN#${sessionId}`,
  skPrefix: 'CONN#',
});

/**
 * A summarized + embedded session vector (U27 Forge). Stored under the owning
 * user so the brute-force cosine fallback can query a user's whole corpus with a
 * single partition read (`begins_with(SK, 'VEC#')`), keeping k-NN scoped to the
 * user (KTD7).
 */
export const sessionVectorKey = (userId: string, sessionId: string): PrimaryKey => ({
  PK: `USERVEC#${userId}`,
  SK: `VEC#${sessionId}`,
});

export const sessionVectorPrefix = (userId: string): { PK: string; skPrefix: string } => ({
  PK: `USERVEC#${userId}`,
  skPrefix: 'VEC#',
});

/**
 * A `owner/repo` -> projectId pointer (U26). GitHub webhooks identify a repo by
 * its full name, not the harness project id; this record lets a GitHub-sourced
 * read resolve the project without a scan. Written when a project connects a
 * repo.
 */
export const repoProjectKey = (repoFullName: string): PrimaryKey => ({
  PK: `REPO#${repoFullName}`,
  SK: 'PROJ',
});

/** GSI1 attributes for a project, so it is queryable by its owner. */
export const projectOwnerIndex = (
  ownerUserId: string,
  projectId: string,
): { GSI1PK: string; GSI1SK: string } => ({
  GSI1PK: `USER#${ownerUserId}`,
  GSI1SK: `PROJ#${projectId}`,
});

/**
 * GSI1 attributes for a live session. Returns undefined when the session is not
 * live, signalling that the GSI1 keys should be stripped so it drops out of the
 * live index.
 */
export function liveSessionIndex(
  status: string,
  lastEventAt: number,
  sessionId: string,
): { GSI1PK: string; GSI1SK: string } | undefined {
  if (status !== 'active' && status !== 'needs_input') return undefined;
  return { GSI1PK: LIVE_PARTITION, GSI1SK: `${pad(lastEventAt, TS_WIDTH)}#${sessionId}` };
}
