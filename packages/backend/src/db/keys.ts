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

/**
 * A learning mined from a correction turn (topic-focus logging). Unlike events
 * (keyed under the session), learnings live under their PROJECT partition so a
 * project's whole corpus is one `begins_with(SK, 'LEARN#')` read. The SK carries
 * `sessionId#turnId`, which both groups a session's learnings together and makes
 * `(sessionId, turnId)` the idempotency key — a re-emitted learning overwrites
 * the same item.
 */
export const learningKey = (projectId: string, sessionId: string, turnId: string): PrimaryKey => ({
  PK: `PROJ#${projectId}`,
  SK: `LEARN#${sessionId}#${turnId}`,
});

export const learningPrefix = (projectId: string): { PK: string; skPrefix: string } => ({
  PK: `PROJ#${projectId}`,
  skPrefix: 'LEARN#',
});

/**
 * A project memory synced up from a developer's machine. Like learnings, memories
 * live under their PROJECT partition so the whole project's set is one
 * `begins_with(SK, 'MEM#')` read (the "Memories" tab). The SK carries
 * `userId#name`, which both groups an author's memories together and makes
 * `(userId, name)` the idempotency key — a re-synced memory overwrites in place,
 * and a per-author `begins_with(SK, 'MEM#<userId>#')` scopes the reconcile to one
 * author so users never clobber each other. Cognito subs and kebab slugs contain
 * no `#`, so the composite SK is unambiguous.
 */
export const memoryKey = (projectId: string, userId: string, name: string): PrimaryKey => ({
  PK: `PROJ#${projectId}`,
  SK: `MEM#${userId}#${name}`,
});

/** Every memory in a project, across all authors (the tab's read). */
export const memoryPrefix = (projectId: string): { PK: string; skPrefix: string } => ({
  PK: `PROJ#${projectId}`,
  skPrefix: 'MEM#',
});

/** One author's memories in a project (the per-user reconcile scope). */
export const memoryUserPrefix = (
  projectId: string,
  userId: string,
): { PK: string; skPrefix: string } => ({
  PK: `PROJ#${projectId}`,
  skPrefix: `MEM#${userId}#`,
});

export const agentKey = (scope: ScopeRef, name: string): PrimaryKey => ({
  PK: `SCOPE#${scopeId(scope)}`,
  SK: `AGENT#${name}`,
});

export const skillKey = (scope: ScopeRef, name: string): PrimaryKey => ({
  PK: `SCOPE#${scopeId(scope)}`,
  SK: `SKILL#${name}`,
});

export const mcpServerKey = (scope: ScopeRef, name: string): PrimaryKey => ({
  PK: `SCOPE#${scopeId(scope)}`,
  SK: `MCPSERVER#${name}`,
});

export const workflowKey = (scope: ScopeRef, name: string): PrimaryKey => ({
  PK: `SCOPE#${scopeId(scope)}`,
  SK: `WORKFLOW#${name}`,
});

/**
 * A workflow RUN's live status record. Unlike the workflow itself (a versioned
 * catalog item under its scope partition), a run is transient execution state
 * keyed under the owning PROJECT partition, so a project's runs are one
 * `begins_with(SK, 'WORKFLOWRUN#')` read. The executor creates one per run and
 * updates its per-node state as the DAG progresses.
 */
export const workflowRunKey = (projectId: string, runId: string): PrimaryKey => ({
  PK: `PROJ#${projectId}`,
  SK: `WORKFLOWRUN#${runId}`,
});

/** Every workflow run in a project, across all workflows (the run-status read). */
export const workflowRunPrefix = (projectId: string): { PK: string; skPrefix: string } => ({
  PK: `PROJ#${projectId}`,
  skPrefix: 'WORKFLOWRUN#',
});

export const scopePartition = (scope: ScopeRef): string => `SCOPE#${scopeId(scope)}`;

/**
 * VERSIONING KEYS (KTD6). Catalog items (skill / agent / mcp) snapshot an
 * immutable REVISION on every content change and carry a per-name ORG-WIDE TRUE
 * pointer. All these rows live in the SAME scope partition as the item record
 * (`SCOPE#org#<org>`), so a variant family + its revisions + its TRUE pointer
 * are one partition read.
 *
 * Variant identity is `(baseName, repoId, userId)`. The BASE variant (org-seeded)
 * has empty repo + user and its variantId is just `<baseName>`; a fork's
 * variantId is `<baseName>#R#<repoId>#U#<userId>`. The revision-row SK mirrors
 * that variant id exactly:
 *
 *   base variant rev N:  SKILL#<baseName>#r<N>
 *   fork variant rev N:  SKILL#<baseName>#R#<repoId>#U#<userId>#r<N>
 *
 * The TRUE pointer is one row per baseName:  SKILL#<baseName>#TRUE
 * (same pattern for AGENT# / MCPSERVER#).
 */
export type CatalogKind = 'SKILL' | 'AGENT' | 'MCPSERVER' | 'WORKFLOW';

/** The variant-id infix shared by the revision SK and the DTO `variantId`. */
export function variantInfix(baseName: string, repoId?: string, userId?: string): string {
  if (!repoId && !userId) return baseName;
  return `${baseName}#R#${repoId ?? ''}#U#${userId ?? ''}`;
}

/** Revision row key: an immutable snapshot of one variant at revision `rev`. */
export const revisionKey = (
  scope: ScopeRef,
  kind: CatalogKind,
  baseName: string,
  rev: number,
  opts: { repoId?: string; userId?: string } = {},
): PrimaryKey => ({
  PK: `SCOPE#${scopeId(scope)}`,
  SK: `${kind}#${variantInfix(baseName, opts.repoId, opts.userId)}#r${pad(rev, SEQ_WIDTH)}`,
});

/** Prefix that gathers EVERY revision of EVERY variant of a baseName. */
export const revisionPrefix = (
  scope: ScopeRef,
  kind: CatalogKind,
  baseName: string,
): { PK: string; skPrefix: string } => ({
  PK: `SCOPE#${scopeId(scope)}`,
  // `<baseName>#` matches both `<baseName>#r..` (base variant revs) and
  // `<baseName>#R#..` (fork variant revs), but NOT a different baseName that
  // merely shares a prefix, because every rev SK has a `#` after the baseName.
  skPrefix: `${kind}#${baseName}#`,
});

/** Prefix that gathers the revisions of ONE specific variant. */
export const variantRevisionPrefix = (
  scope: ScopeRef,
  kind: CatalogKind,
  baseName: string,
  opts: { repoId?: string; userId?: string } = {},
): { PK: string; skPrefix: string } => ({
  PK: `SCOPE#${scopeId(scope)}`,
  skPrefix: `${kind}#${variantInfix(baseName, opts.repoId, opts.userId)}#r`,
});

/** The per-baseName ORG-WIDE TRUE pointer row (which variant+rev is the default). */
export const truePointerKey = (
  scope: ScopeRef,
  kind: CatalogKind,
  baseName: string,
): PrimaryKey => ({
  PK: `SCOPE#${scopeId(scope)}`,
  SK: `${kind}#${baseName}#TRUE`,
});

/**
 * Is an SK a versioning side-record (a `#r<N>` revision snapshot or a `#TRUE`
 * pointer) rather than a "current" catalog item record? The catalog list reads
 * (`SKILL#`/`AGENT#`/`MCPSERVER#` prefix scans) must skip these so they only
 * return the live item records, not their revision history / pointers.
 */
export function isVersionSideRecord(sk: string | undefined): boolean {
  if (!sk) return false;
  return sk.endsWith('#TRUE') || /#r\d+$/.test(sk);
}

/**
 * The org-scope partition prefix (`SCOPE#org#`). The org catalog (skills/agents/
 * mcp) and the skill-idea records all partition under `SCOPE#org#<org>`; this is
 * the prefix the stream consumer (U3) gates a SKILL# record on before re-embedding.
 */
export const ORG_SCOPE_PREFIX = 'SCOPE#org#';

/**
 * Extract the `<org>` from an org-scope partition PK (`SCOPE#org#<org>`), or
 * `undefined` if `pk` is not an org-scope partition. The stream consumer uses
 * this to resolve which org's skill-vector index a SKILL# write targets. (Skill
 * records carry `scope.id` too, but the PK is the authoritative partition key.)
 */
export function orgFromScopePartition(pk: string | undefined): string | undefined {
  if (!pk || !pk.startsWith(ORG_SCOPE_PREFIX)) return undefined;
  const org = pk.slice(ORG_SCOPE_PREFIX.length);
  return org.length > 0 ? org : undefined;
}

/**
 * SKILL IDEAS (skill-idea loop, U6). An idea is CO-LOCATED with skills in the
 * org scope partition (`SCOPE#org#<org>`) but under an `IDEA#` SK prefix:
 *
 *   IDEA#<skillBaseName>#<ideaId>
 *
 * The `IDEA#` prefix never collides with the `SKILL#` prefix the catalog list
 * scans, so ideas are invisible to `listSkills` (and `isVersionSideRecord` is
 * irrelevant here — ideas are not version side-records). `ideaPrefixForSkill`
 * gathers every idea of one skill family in one `begins_with` read;
 * `ideaPrefixForOrg` gathers every idea in the org.
 */
export const ideaKey = (org: string, skillBaseName: string, ideaId: string): PrimaryKey => ({
  PK: `SCOPE#org#${org}`,
  SK: `IDEA#${skillBaseName}#${ideaId}`,
});

/** Every idea attached to ONE skill family in an org. */
export const ideaPrefixForSkill = (
  org: string,
  skillBaseName: string,
): { PK: string; skPrefix: string } => ({
  PK: `SCOPE#org#${org}`,
  // The trailing `#` after the baseName makes this an exact-family prefix: it
  // matches `IDEA#<baseName>#<ideaId>` but NOT a different baseName that merely
  // shares a leading substring.
  skPrefix: `IDEA#${skillBaseName}#`,
});

/** Every idea in an org, across all skills. */
export const ideaPrefixForOrg = (org: string): { PK: string; skPrefix: string } => ({
  PK: `SCOPE#org#${org}`,
  skPrefix: 'IDEA#',
});

/**
 * The org's UNASSIGNED bin (the new-skill backlog). Shares the org scope
 * partition under an `IDEABIN#` SK prefix:  IDEABIN#<entryId>.
 */
export const unassignedBinKey = (org: string, entryId: string): PrimaryKey => ({
  PK: `SCOPE#org#${org}`,
  SK: `IDEABIN#${entryId}`,
});

/** Every unassigned-bin entry in an org. */
export const unassignedBinPrefix = (org: string): { PK: string; skPrefix: string } => ({
  PK: `SCOPE#org#${org}`,
  skPrefix: 'IDEABIN#',
});

export const objectiveKey = (org: string, path: string): PrimaryKey => ({
  PK: `ORG#${org}`,
  SK: `RCDO#${path}`,
});

/**
 * The org's own record (name + creator + password salt/hash). It shares the org
 * partition with the objective tree (`RCDO#…`) and the DoD (`CONFIG#DOD`) under
 * a fixed meta SK, so one org's data is a single partition.
 */
export const orgKey = (name: string): PrimaryKey => ({ PK: `ORG#${name}`, SK: 'META' });

/**
 * The org-wide Definition of Done (plan-mapping feature 1). A single record per
 * org — it lives in the org partition under a fixed meta SK, alongside the RCDO
 * tree (`RCDO#…`). Advisory config that `/update-progress` reports against.
 */
export const orgDodKey = (org: string): PrimaryKey => ({
  PK: `ORG#${org}`,
  SK: 'CONFIG#DOD',
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
 * User-code → device-code pointer for the device-code login flow. The human
 * approver only types the short `userCode`, so we persist a pointer item that
 * resolves it to the opaque `deviceCode` used as the record's primary key.
 */
export const deviceUserCodeKey = (userCode: string): PrimaryKey => ({
  PK: `DEVUC#${userCode}`,
  SK: 'USERCODE',
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
