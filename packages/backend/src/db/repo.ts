import {
  DeleteCommand,
  DynamoDBDocumentClient,
  GetCommand,
  PutCommand,
  QueryCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import type {
  Agent,
  DefinitionOfDone,
  DeviceAuth,
  Envelope,
  GoldenCase,
  Idea,
  LearningRecord,
  McpServer,
  Memory,
  Project,
  ScopeRef,
  SessionProjection,
  Skill,
  TruePointer,
  UnassignedEntry,
  UserProfile,
  WeeklyUpdate,
  Workflow,
  WorkflowRun,
  WorkflowRunNodeState,
} from '@harness/shared';
import {
  DEFAULT_DEFINITION_OF_DONE,
  memorySchema,
  orgScope,
  projectSchema,
  resolveScoped,
  truePointerSchema,
  userProfileSchema,
  userScope,
  variantIdFor,
} from '@harness/shared';
import * as k from './keys.js';
import { flattenBundle } from '../catalog/bundles.js';
import type { PgDb } from './pg/migrate.js';
import { upsertProjectMirror } from './pg/weeklyRepo.js';

/**
 * Default retention window (days) for the append-only event/log stream (U20).
 * After this many days an event's `ttl` lapses and DynamoDB TimeToLive reaps it.
 */
export const DEFAULT_EVENT_RETENTION_DAYS = 90;

/**
 * The configured event-stream retention window in days (U20). Read from the
 * `EVENT_RETENTION_DAYS` env var (so deploy can tune it) and falling back to
 * `DEFAULT_EVENT_RETENTION_DAYS`. A value of `0` (or a non-positive/invalid one)
 * disables expiry entirely — `appendEvent` then omits `ttl` and events never
 * expire.
 */
export function eventRetentionDays(): number {
  const raw = process.env.EVENT_RETENTION_DAYS;
  if (raw === undefined || raw === '') return DEFAULT_EVENT_RETENTION_DAYS;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) return 0;
  return Math.floor(parsed);
}

/**
 * A live WebSocket connection in the registry. Daemon connections also carry the
 * `instanceId` they host (so control frames can be routed to them); web client
 * connections omit it.
 */
export interface ConnectionRecord {
  connectionId: string;
  userId: string;
  org: string;
  /** Present for daemon connections; absent for web clients. */
  instanceId?: string;
  /** 'daemon' (event ingestion) or 'web' (live watch/steer). */
  role: 'daemon' | 'web';
  connectedAt: number;
}

/**
 * A claude+ instance (one per repo+host) as projected from its lifecycle. Used
 * by the Project detail screen to show live activity. Written by the ingestion
 * path; read here for the REST projects API.
 */
export interface InstanceRecord {
  projectId: string;
  host: string;
  online: boolean;
  sessionCount?: number;
  uptimeSec?: number;
}

/**
 * The stored ORG item. It carries the password salt + hash so a join can verify
 * the typed secret. This shape is INTERNAL to the repo and is NEVER returned to
 * a client — handlers project it down to the public `Org` DTO (no password
 * fields) before serializing.
 */
export interface OrgRecord {
  name: string;
  createdBy: string;
  createdAt: number;
  passwordSalt: string;
  passwordHash: string;
}

/**
 * Intent-named access layer over the single `harness` table. Handlers depend on
 * this, never on raw DynamoDB commands. The DynamoDBDocumentClient is injected
 * so it can be mocked in tests.
 */
export class Repo {
  constructor(
    private readonly doc: DynamoDBDocumentClient,
    private readonly table: string = k.tableName(),
    /**
     * Optional Postgres handle (KTD7). When present, `putProject` also syncs the
     * slim `projects` mirror so the Postgres-side manager joins + `listProjectsForOrg`
     * (U2) stay in step with Dynamo. Absent in tests/paths that never touch the
     * strategic-execution domain, so the project write degrades to Dynamo-only.
     */
    private readonly pgDb?: PgDb,
  ) {}

  // --- Users (profiles) + org membership ---------------------------------
  //
  // The PROFILE record (`USER#<id> / PROFILE`) is the SOURCE OF TRUTH for org
  // membership: `profile.org` unset means the user has no org and must onboard.
  // (Membership used to ride on the Cognito token claim, so everyone always had
  // an org; moving it here lets a user genuinely be org-less.)

  /** A user's PROFILE record, or undefined when they have never been seen. */
  async getUser(userId: string): Promise<UserProfile | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.userKey(userId) }),
    );
    if (!res.Item) return undefined;
    // Parse through the schema to strip table PK/SK attributes; fall back to the
    // raw item on a parse miss, mirroring getProject's tolerance of legacy rows.
    const parsed = userProfileSchema.safeParse(res.Item);
    return parsed.success ? parsed.data : (res.Item as UserProfile);
  }

  /**
   * Upsert a user's full PROFILE record. When the profile carries a
   * `managerUserId`, the GSI1 keys are stamped so the user surfaces in that
   * manager's `listReports` query; with no manager the keys are absent (a plain
   * PutCommand overwrites the whole item, so dropping them removes the user from
   * every manager's reports — KTD6).
   */
  async putUser(profile: UserProfile): Promise<void> {
    const index = k.managerReportIndex(profile.managerUserId, profile.userId);
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.userKey(profile.userId), ...profile, ...(index ?? {}) },
      }),
    );
  }

  /**
   * Set (or clear, with `null`) a user's manager edge (KTD6) without disturbing
   * the rest of the profile (org membership, admin flags). Read-modify-write on
   * the PROFILE so `putUser` re-stamps/strips the GSI1 reports index. Creates a
   * bare profile if the user has none yet (a manager can be set before onboarding).
   */
  async setManager(userId: string, managerUserId: string | null): Promise<void> {
    const existing = await this.getUser(userId);
    await this.putUser({
      ...(existing ?? {}),
      userId,
      managerUserId: managerUserId ?? undefined,
    });
  }

  /**
   * Every user whose `managerUserId` is `managerUserId` — the manager's team
   * (KTD6). A single GSI1 query on the `MANAGER#<id>` partition (no table scan),
   * mirroring `listProjectsForUser`. A manager with no reports → `[]`.
   */
  async listReports(managerUserId: string): Promise<UserProfile[]> {
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        IndexName: k.GSI1,
        KeyConditionExpression: 'GSI1PK = :pk',
        ExpressionAttributeValues: { ':pk': `MANAGER#${managerUserId}` },
      }),
    );
    return (res.Items ?? []).map((item) => {
      const parsed = userProfileSchema.safeParse(item);
      return parsed.success ? parsed.data : (item as UserProfile);
    });
  }

  /**
   * Add `org` to the user's membership set and make it the ACTIVE org — the write
   * that ends onboarding (create/join). Upsert + accumulate: the org is unioned
   * into `orgs` (so a user builds up the set they can switch between) and into
   * `adminOrgs` when `{ admin: true }` (an org's creator). Legacy single-`org`
   * profiles are migrated forward (their old org seeds the set). `admin` is kept
   * as a denormalized "admin of the ACTIVE org" flag for the simple reads.
   */
  async setUserOrg(
    userId: string,
    org: string,
    opts: { name?: string; admin?: boolean } = {},
  ): Promise<void> {
    const existing = await this.getUser(userId);
    const orgs = new Set(existing?.orgs ?? (existing?.org ? [existing.org] : []));
    orgs.add(org);
    const adminOrgs = new Set(
      existing?.adminOrgs ?? (existing?.admin && existing?.org ? [existing.org] : []),
    );
    if (opts.admin) adminOrgs.add(org);
    await this.putUser({
      userId,
      org,
      name: opts.name ?? existing?.name,
      admin: adminOrgs.has(org),
      orgs: [...orgs],
      adminOrgs: [...adminOrgs],
      // Preserve the manager edge across onboarding (create/join) — this object is
      // built fresh, so without carrying it forward, joining an org would wipe it.
      managerUserId: existing?.managerUserId,
    });
  }

  /**
   * Flip the ACTIVE org to one the user has already joined (no password — a
   * member is just changing context). Returns `{ switched: false }` when the user
   * has no profile or is not a member of `org`, so the handler can 403 without
   * leaking whether the org exists. `admin` is recomputed for the new active org.
   */
  async switchActiveOrg(userId: string, org: string): Promise<{ switched: boolean }> {
    const existing = await this.getUser(userId);
    const orgs = existing?.orgs ?? (existing?.org ? [existing.org] : []);
    if (!existing || !orgs.includes(org)) return { switched: false };
    const adminOrgs = existing.adminOrgs ?? (existing.admin && existing.org ? [existing.org] : []);
    await this.putUser({ ...existing, org, admin: adminOrgs.includes(org), orgs, adminOrgs });
    return { switched: true };
  }

  // --- Organizations -----------------------------------------------------
  //
  // The ORG record lives in the org partition (`ORG#<name> / META`) beside the
  // objective tree + DoD. It stores the password salt/hash so a join can verify
  // the typed secret; getOrg returns the internal shape and the handler strips
  // the password fields before responding.

  /** The stored org record (with password fields), or undefined if no such org. */
  async getOrg(name: string): Promise<OrgRecord | undefined> {
    const res = await this.doc.send(new GetCommand({ TableName: this.table, Key: k.orgKey(name) }));
    return res.Item as OrgRecord | undefined;
  }

  /**
   * Create an org exactly once. The conditional write fails if the org name is
   * already taken (names are the join key, so they must be unique); we surface
   * that as `{ created: false }` rather than throwing, mirroring appendEvent.
   */
  async createOrg(rec: OrgRecord): Promise<{ created: boolean }> {
    try {
      await this.doc.send(
        new PutCommand({
          TableName: this.table,
          Item: { ...k.orgKey(rec.name), ...rec },
          ConditionExpression: 'attribute_not_exists(PK)',
        }),
      );
      return { created: true };
    } catch (err) {
      if ((err as { name?: string }).name === 'ConditionalCheckFailedException') {
        return { created: false };
      }
      throw err;
    }
  }

  // --- Projects -----------------------------------------------------------

  async putProject(p: Project): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.projectKey(p.id), ...p, ...k.projectOwnerIndex(p.ownerUserId, p.id) },
      }),
    );
    // Sync the slim Postgres mirror (id/org/owner/name only — KTD7/U2) so the
    // relational manager joins + `listProjectsForOrg` agree with Dynamo. Skipped
    // when no Postgres handle is wired, or when the project has no org (the mirror
    // requires one — back-compat projects without an org never appear in an
    // org-scoped listing anyway).
    if (this.pgDb && p.org) {
      await upsertProjectMirror(this.pgDb, {
        id: p.id,
        org: p.org,
        ownerUserId: p.ownerUserId,
        name: p.name,
      });
    }
  }

  async getProject(projectId: string): Promise<Project | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.projectKey(projectId) }),
    );
    if (!res.Item) return undefined;
    // Parse through the schema so legacy records (created before enabledSkills/
    // enabledAgents existed) read those arrays as [] via the Zod defaults, and
    // strip the table's PK/SK/GSI attributes.
    const parsed = projectSchema.safeParse(res.Item);
    return parsed.success ? parsed.data : (res.Item as Project);
  }

  async listProjectsForUser(userId: string): Promise<Project[]> {
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        IndexName: k.GSI1,
        KeyConditionExpression: 'GSI1PK = :pk',
        ExpressionAttributeValues: { ':pk': `USER#${userId}` },
      }),
    );
    return (res.Items ?? []) as Project[];
  }

  /**
   * Record the `owner/repo` -> projectId mapping (U26). GitHub webhooks arrive
   * keyed by repo full-name; this lets the webhook handler resolve the project
   * without scanning. Idempotent: re-connecting the same repo overwrites.
   */
  async linkRepoToProject(repoFullName: string, projectId: string): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.repoProjectKey(repoFullName), repoFullName, projectId },
      }),
    );
  }

  /** Resolve the projectId a `owner/repo` is connected to, if any (U26). */
  async getProjectIdForRepo(repoFullName: string): Promise<string | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.repoProjectKey(repoFullName) }),
    );
    return (res.Item as { projectId?: string } | undefined)?.projectId;
  }

  /**
   * Delete a project and everything under its partition (sessions, instances,
   * weekly snapshots, framing), plus the `owner/repo` -> projectId pointer so the
   * repo can be reconnected cleanly. Everything for a project lives under
   * `PK = PROJ#<id>`, so a single partition query enumerates the rows to delete;
   * the repo pointer lives under its own key and is removed separately. Returns
   * `{ deleted }` — false when the project did not exist, so the caller can 404.
   */
  async deleteProject(projectId: string): Promise<{ deleted: boolean }> {
    const existing = await this.getProject(projectId);
    if (!existing) return { deleted: false };

    // Enumerate every row in the project's partition (project record + sessions +
    // instances + weekly), keyed only by PK so SK variants all come back.
    const rows = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': `PROJ#${projectId}` },
        ProjectionExpression: 'PK, SK',
      }),
    );
    for (const item of rows.Items ?? []) {
      await this.doc.send(
        new DeleteCommand({
          TableName: this.table,
          Key: { PK: (item as { PK: string }).PK, SK: (item as { SK: string }).SK },
        }),
      );
    }

    // Drop the repo -> project pointer (best-effort; absent is fine).
    if (existing.repo) {
      const repoFullName = existing.repo.replace(/^gh\//, '');
      await this.doc.send(
        new DeleteCommand({ TableName: this.table, Key: k.repoProjectKey(repoFullName) }),
      );
    }

    return { deleted: true };
  }

  // --- U7: GitHub-sourced framing (progress + PRD goal + owned outcomes) --
  //
  // Additive, intent-named writers kept separate from `putProject` so the merge
  // with the de-ticket refactor stays mechanical. Both are partial updates on an
  // existing Project; they no-op silently if the project is missing (the refresh
  // handler 404s before calling these).

  /**
   * Store the GitHub-sourced progress percentage on a Project (U7). `progressPct`
   * is clamped 0..100 by the caller; `framingReadAt`/`framingStale` record when
   * the read succeeded and whether the value is the last-known (stale) one.
   */
  async setProjectProgress(
    projectId: string,
    progressPct: number,
    opts: { readAt?: string; stale?: boolean } = {},
  ): Promise<void> {
    await this.doc.send(
      new UpdateCommand({
        TableName: this.table,
        Key: k.projectKey(projectId),
        UpdateExpression: 'SET progressPct = :p, framingReadAt = :r, framingStale = :s',
        ConditionExpression: 'attribute_exists(PK)',
        ExpressionAttributeValues: {
          ':p': Math.max(0, Math.min(100, progressPct)),
          ':r': opts.readAt ?? new Date().toISOString(),
          ':s': opts.stale ?? false,
        },
      }),
    );
  }

  /**
   * Store the full GitHub-sourced framing on a Project (U7): progress, PRD goal,
   * and the owned Supporting Outcome ids. A partial update that leaves all other
   * Project fields (name, repo, ownerUserId, …) untouched.
   */
  async putProjectFraming(
    projectId: string,
    framing: {
      progressPct: number;
      prdGoal?: string;
      supportingOutcomeIds?: string[];
      readAt?: string;
      stale?: boolean;
    },
  ): Promise<void> {
    await this.doc.send(
      new UpdateCommand({
        TableName: this.table,
        Key: k.projectKey(projectId),
        UpdateExpression:
          'SET progressPct = :p, prdGoal = :g, supportingOutcomeIds = :o, framingReadAt = :r, framingStale = :s',
        ConditionExpression: 'attribute_exists(PK)',
        ExpressionAttributeValues: {
          ':p': Math.max(0, Math.min(100, framing.progressPct)),
          ':g': framing.prdGoal ?? null,
          ':o': framing.supportingOutcomeIds ?? [],
          ':r': framing.readAt ?? new Date().toISOString(),
          ':s': framing.stale ?? false,
        },
      }),
    );
  }

  // --- Events + session projections --------------------------------------

  /**
   * Append an event idempotently. Duplicate (sessionId, seq) is a no-op — the
   * conditional write fails and we swallow it, so replays after reconnect are
   * safe.
   *
   * The append-only log stream self-expires (U20/KTD11): a numeric epoch-SECONDS
   * `ttl` attribute is stamped on the envelope item so DynamoDB TimeToLive reaps
   * old events (the table-level TTL on `ttl` is already enabled in infra — the
   * same attribute device-auth uses, so NO infra change). The retention window is
   * `eventRetentionDays()` (env-configurable); a window of 0 disables expiry and
   * the `ttl` is omitted so those events never expire. The "now" timestamp is
   * supplied by the caller (the ingest handler) rather than read here, so this
   * stays free of an ambient `Date.now()` in the pure-ish repo path; it falls back
   * to the envelope's own `ts` (epoch ms) when not passed.
   */
  async appendEvent(env: Envelope, opts: { nowMs?: number } = {}): Promise<{ stored: boolean }> {
    const retentionDays = eventRetentionDays();
    const ttl =
      retentionDays > 0
        ? Math.floor((opts.nowMs ?? env.ts) / 1000) + retentionDays * 86_400
        : undefined;
    try {
      await this.doc.send(
        new PutCommand({
          TableName: this.table,
          Item: {
            ...k.eventKey(env.event.sessionId, env.seq),
            ...env,
            ...(ttl !== undefined ? { ttl } : {}),
          },
          ConditionExpression: 'attribute_not_exists(PK)',
        }),
      );
      return { stored: true };
    } catch (err) {
      if ((err as { name?: string }).name === 'ConditionalCheckFailedException') {
        return { stored: false };
      }
      throw err;
    }
  }

  async listEvents(
    sessionId: string,
    opts: { limit?: number; ascending?: boolean } = {},
  ): Promise<Envelope[]> {
    const { PK, skPrefix } = k.eventPrefix(sessionId);
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': PK, ':sk': skPrefix },
        ScanIndexForward: opts.ascending ?? true,
        Limit: opts.limit,
      }),
    );
    return (res.Items ?? []) as Envelope[];
  }

  async putSessionProjection(s: SessionProjection): Promise<void> {
    await this.writeSessionProjection(s, false);
  }

  /**
   * Conditional projection upsert for optimistic concurrency (U6 lost-update
   * fix). `expectedMaxSeq` is the `maxSeq` the caller folded on top of:
   *  - `undefined` requires the projection not to exist yet (first write),
   *  - a number requires the stored projection's `maxSeq` to still equal it.
   * A `ConditionalCheckFailedException` (a concurrent writer advanced it) is
   * surfaced as `{ written: false }` so the caller can re-read, re-fold, and
   * retry instead of clobbering the concurrent update.
   */
  async putSessionProjectionConditional(
    s: SessionProjection,
    expectedMaxSeq: number | undefined,
  ): Promise<{ written: boolean }> {
    try {
      await this.writeSessionProjection(s, true, expectedMaxSeq);
      return { written: true };
    } catch (err) {
      if ((err as { name?: string }).name === 'ConditionalCheckFailedException') {
        return { written: false };
      }
      throw err;
    }
  }

  private async writeSessionProjection(
    s: SessionProjection,
    conditional: boolean,
    expectedMaxSeq?: number,
  ): Promise<void> {
    const live = k.liveSessionIndex(s.status, s.lastEventAt, s.sessionId);
    // Optimistic-concurrency guard, only when `conditional`:
    //  - no prior projection (expectedMaxSeq undefined) -> require it not exist,
    //  - otherwise require the stored maxSeq still equal what we folded on.
    let guard: Record<string, unknown> = {};
    if (conditional) {
      guard =
        expectedMaxSeq === undefined
          ? { ConditionExpression: 'attribute_not_exists(PK)' }
          : {
              ConditionExpression: 'maxSeq = :expected',
              ExpressionAttributeValues: { ':expected': expectedMaxSeq },
            };
    }
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.sessionKey(s.projectId, s.sessionId), ...s, ...(live ?? {}) },
        ...guard,
      }),
    );
    // Maintain the sessionId -> projectId pointer for projectId-less lookups.
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.sessionPointerKey(s.sessionId), projectId: s.projectId },
      }),
    );
  }

  async getSession(projectId: string, sessionId: string): Promise<SessionProjection | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.sessionKey(projectId, sessionId) }),
    );
    return res.Item as SessionProjection | undefined;
  }

  /**
   * Resolve a session projection from the sessionId alone, via the pointer
   * record. Used by event ingestion (events after `session.start` omit the
   * projectId) and the control gateway. Returns undefined if unknown.
   */
  async getSessionById(sessionId: string): Promise<SessionProjection | undefined> {
    const ptr = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.sessionPointerKey(sessionId) }),
    );
    const projectId = (ptr.Item as { projectId?: string } | undefined)?.projectId;
    if (!projectId) return undefined;
    return this.getSession(projectId, sessionId);
  }

  async listLiveSessions(): Promise<SessionProjection[]> {
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        IndexName: k.GSI1,
        KeyConditionExpression: 'GSI1PK = :pk',
        ExpressionAttributeValues: { ':pk': k.LIVE_PARTITION },
        ScanIndexForward: false, // most recent first
      }),
    );
    return (res.Items ?? []) as SessionProjection[];
  }

  async listSessionsForProject(projectId: string): Promise<SessionProjection[]> {
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': `PROJ#${projectId}`, ':sk': 'SESS#' },
      }),
    );
    return (res.Items ?? []) as SessionProjection[];
  }

  async getInstance(projectId: string, host: string): Promise<InstanceRecord | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.instanceKey(projectId, host) }),
    );
    return res.Item as InstanceRecord | undefined;
  }

  async listInstances(projectId: string): Promise<InstanceRecord[]> {
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': `PROJ#${projectId}`, ':sk': 'INST#' },
      }),
    );
    return (res.Items ?? []) as InstanceRecord[];
  }

  // --- Catalog items (skills / agents / mcp servers / workflows) ----------
  //
  // All four kinds share the same persistence shape: the live record lives
  // under its per-kind item key (see `itemKey`), and the catalog list is a
  // per-scope `<KIND>#` prefix scan. The public per-kind methods are thin
  // typed wrappers over these generics.

  private async putCatalogItem<T extends { scope: ScopeRef; name: string }>(
    kind: k.CatalogKind,
    item: T,
  ): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...this.itemKey(kind, item.scope, item.name), ...item },
      }),
    );
  }

  private async getCatalogItem<T>(
    kind: k.CatalogKind,
    scope: ScopeRef,
    name: string,
  ): Promise<T | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: this.itemKey(kind, scope, name) }),
    );
    return res.Item as T | undefined;
  }

  private async deleteCatalogItem(kind: k.CatalogKind, scope: ScopeRef, name: string): Promise<void> {
    await this.doc.send(
      new DeleteCommand({ TableName: this.table, Key: this.itemKey(kind, scope, name) }),
    );
  }

  /**
   * The catalog of one kind visible to a viewer. With no `userId` this is the
   * org-only catalog (back-compat with every existing caller/test). With a
   * `userId` it merges the org scope AND the user scope, so user-scoped items
   * are included and a user-scoped item SHADOWS an org-scoped one of the same
   * name (resolveScoped: narrowest scope wins).
   */
  private async listCatalog<T extends { name: string; scope: ScopeRef }>(
    kind: k.CatalogKind,
    org: string,
    userId?: string,
  ): Promise<T[]> {
    const orgItems = await this.listScoped<T>(orgScope(org), `${kind}#`);
    if (userId === undefined) return orgItems;
    const userItems = await this.listScoped<T>(userScope(userId), `${kind}#`);
    return resolveScoped([...orgItems, ...userItems], { org, userId });
  }

  async putAgent(a: Agent): Promise<void> {
    return this.putCatalogItem('AGENT', a);
  }

  async getAgent(scope: ScopeRef, name: string): Promise<Agent | undefined> {
    return this.getCatalogItem<Agent>('AGENT', scope, name);
  }

  async deleteAgent(scope: ScopeRef, name: string): Promise<void> {
    return this.deleteCatalogItem('AGENT', scope, name);
  }

  async putWorkflow(w: Workflow): Promise<void> {
    return this.putCatalogItem('WORKFLOW', w);
  }

  async getWorkflow(scope: ScopeRef, name: string): Promise<Workflow | undefined> {
    return this.getCatalogItem<Workflow>('WORKFLOW', scope, name);
  }

  async deleteWorkflow(scope: ScopeRef, name: string): Promise<void> {
    return this.deleteCatalogItem('WORKFLOW', scope, name);
  }

  // --- Workflow runs (live execution status, M5) -------------------------

  /** Create/overwrite a workflow run's status record under its project partition. */
  async putWorkflowRun(run: WorkflowRun): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.workflowRunKey(run.projectId, run.runId), ...run },
      }),
    );
  }

  async getWorkflowRun(projectId: string, runId: string): Promise<WorkflowRun | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.workflowRunKey(projectId, runId) }),
    );
    return res.Item as WorkflowRun | undefined;
  }

  /** Every run in a project, across all workflows, in one partition read. */
  async listWorkflowRuns(projectId: string): Promise<WorkflowRun[]> {
    const { PK, skPrefix } = k.workflowRunPrefix(projectId);
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': PK, ':sk': skPrefix },
      }),
    );
    return (res.Items ?? []) as WorkflowRun[];
  }

  /**
   * Read-modify-write a single node's slice of a run (the executor reports node
   * transitions one at a time). Merges `partial` onto the node's current entry —
   * creating it if absent — so a state/runs/outputTail update never clobbers the
   * fields it does not carry. Returns the updated run, or undefined if the run is
   * missing.
   */
  async updateWorkflowRunNode(
    projectId: string,
    runId: string,
    nodeId: string,
    partial: Partial<{ state: WorkflowRunNodeState; runs: number; outputTail: string }>,
  ): Promise<WorkflowRun | undefined> {
    const run = await this.getWorkflowRun(projectId, runId);
    if (!run) return undefined;
    const current = run.nodes[nodeId] ?? { state: 'pending', runs: 0, outputTail: '' };
    run.nodes[nodeId] = { ...current, ...partial };
    await this.putWorkflowRun(run);
    return run;
  }

  async putSkill(s: Skill): Promise<void> {
    return this.putCatalogItem('SKILL', s);
  }

  async getSkill(scope: ScopeRef, name: string): Promise<Skill | undefined> {
    return this.getCatalogItem<Skill>('SKILL', scope, name);
  }

  async deleteSkill(scope: ScopeRef, name: string): Promise<void> {
    return this.deleteCatalogItem('SKILL', scope, name);
  }

  async putMcpServer(s: McpServer): Promise<void> {
    return this.putCatalogItem('MCPSERVER', s);
  }

  async getMcpServer(scope: ScopeRef, name: string): Promise<McpServer | undefined> {
    return this.getCatalogItem<McpServer>('MCPSERVER', scope, name);
  }

  async deleteMcpServer(scope: ScopeRef, name: string): Promise<void> {
    return this.deleteCatalogItem('MCPSERVER', scope, name);
  }

  // --- Versioning: variants + revisions + TRUE pointer (KTD6) -------------
  //
  // A catalog item is versioned per VARIANT, where a variant is `(baseName,
  // repoId, userId)`. Every content edit SNAPSHOTS an immutable revision row;
  // the "current"/latest record stays under its plain item key (skillKey etc.)
  // so existing reads are unchanged. One per-baseName ORG-WIDE TRUE pointer says
  // which variant+rev is the default the UI shows and a project adds. Promotion
  // only repoints TRUE — it never edits or deletes a variant.

  /**
   * Snapshot a NEW revision of a variant. Computes the variant's next rev
   * (max existing + 1), stamps `baseName`/`variantId`/`version`/`createdAt` onto
   * the record, writes the immutable revision row, and upserts the live "current"
   * item record under its plain item key. The FIRST time a baseName is seen, the
   * TRUE pointer is initialized to this variant+rev (so a freshly-created item is
   * immediately the org default); subsequent edits leave TRUE untouched (promotion
   * is explicit). Returns the stamped record (with `variantId`/`version` set).
   */
  async putNewVersion<T extends { name: string; scope: ScopeRef } & Record<string, unknown>>(
    kind: k.CatalogKind,
    item: T,
    opts: { repoId?: string; authorUserId?: string; now?: number } = {},
  ): Promise<T> {
    const scope = item.scope;
    const baseName = (item.baseName as string | undefined) ?? item.name;
    const repoId = opts.repoId ?? (item.repoId as string | undefined);
    const authorUserId = opts.authorUserId ?? (item.authorUserId as string | undefined);
    const variantId = variantIdFor(baseName, repoId, authorUserId);
    const now = opts.now ?? Date.now();

    const existing = await this.listRevisions(scope, kind, baseName, {
      repoId,
      userId: authorUserId,
    });
    const nextRev = existing.reduce((m, r) => Math.max(m, (r.version as number) ?? 1), 0) + 1;

    // Strip any stray primary-key attributes the caller carried onto the item.
    // A caller that builds the item from a `getSkill`/`getAgent` read brings the
    // SOURCE row's `PK`/`SK` along; spreading that into the rows below would let
    // the live record's `SK` overwrite the revision row's `SK` (both collapse onto
    // the same key and only ONE row is written — the revision snapshot is lost).
    // The row key is supplied explicitly per write, so these must never ride along.
    const itemNoKeys = { ...item };
    delete (itemNoKeys as Record<string, unknown>).PK;
    delete (itemNoKeys as Record<string, unknown>).SK;
    const stamped = {
      ...itemNoKeys,
      baseName,
      variantId,
      version: nextRev,
      createdAt: now,
      ...(repoId !== undefined ? { repoId } : {}),
      ...(authorUserId !== undefined ? { authorUserId } : {}),
    } as T;

    // Immutable revision snapshot.
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: {
          ...k.revisionKey(scope, kind, baseName, nextRev, { repoId, userId: authorUserId }),
          ...stamped,
        },
      }),
    );
    // Live "current" record under the plain item key (unchanged read path).
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...this.itemKey(kind, scope, item.name), ...stamped },
      }),
    );
    // Initialize the TRUE pointer the first time this baseName is seen.
    const truth = await this.getTrueVariant(scope, kind, baseName);
    if (!truth) {
      await this.setTrueVariant(scope, kind, { baseName, variantId, rev: nextRev });
    }
    return stamped;
  }

  /** The "current" item key for a catalog kind (the live, latest record). */
  private itemKey(kind: k.CatalogKind, scope: ScopeRef, name: string): k.PrimaryKey {
    switch (kind) {
      case 'SKILL':
        return k.skillKey(scope, name);
      case 'AGENT':
        return k.agentKey(scope, name);
      case 'MCPSERVER':
        return k.mcpServerKey(scope, name);
      case 'WORKFLOW':
        return k.workflowKey(scope, name);
    }
  }

  /**
   * Every revision row for a baseName across ALL its variants (or one variant
   * when `repoId`/`userId` are given). Ordered by SK (variant, then rev).
   */
  async listRevisions(
    scope: ScopeRef,
    kind: k.CatalogKind,
    baseName: string,
    opts: { repoId?: string; userId?: string } = {},
  ): Promise<Array<Record<string, unknown>>> {
    const one = opts.repoId !== undefined || opts.userId !== undefined;
    const { PK, skPrefix } = one
      ? k.variantRevisionPrefix(scope, kind, baseName, opts)
      : k.revisionPrefix(scope, kind, baseName);
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': PK, ':sk': skPrefix },
      }),
    );
    // The shared revision prefix also matches the `#TRUE` pointer when (and only
    // when) it begins with the same baseName, so filter side-records that are not
    // `#r<N>` rows.
    return (res.Items ?? []).filter((it) => /#r\d+$/.test((it as { SK: string }).SK)) as Array<
      Record<string, unknown>
    >;
  }

  /** One specific revision snapshot of a variant, or undefined. */
  async getRevision(
    scope: ScopeRef,
    kind: k.CatalogKind,
    baseName: string,
    rev: number,
    opts: { repoId?: string; userId?: string } = {},
  ): Promise<Record<string, unknown> | undefined> {
    const res = await this.doc.send(
      new GetCommand({
        TableName: this.table,
        Key: k.revisionKey(scope, kind, baseName, rev, opts),
      }),
    );
    return res.Item as Record<string, unknown> | undefined;
  }

  /**
   * The distinct variants of a baseName: the LATEST revision row per variantId.
   * Used by the per-repo dropdown ("pick another variant") and the UI's variant
   * list. Ordered by variantId for determinism.
   */
  async listVariants(
    scope: ScopeRef,
    kind: k.CatalogKind,
    baseName: string,
  ): Promise<Array<Record<string, unknown>>> {
    const revs = await this.listRevisions(scope, kind, baseName);
    const latestByVariant = new Map<string, Record<string, unknown>>();
    for (const r of revs) {
      const vid = (r.variantId as string) ?? baseName;
      const cur = latestByVariant.get(vid);
      if (!cur || ((r.version as number) ?? 1) > ((cur.version as number) ?? 1)) {
        latestByVariant.set(vid, r);
      }
    }
    return [...latestByVariant.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([, r]) => r);
  }

  /** The per-baseName ORG-WIDE TRUE pointer, or undefined if none set yet. */
  async getTrueVariant(
    scope: ScopeRef,
    kind: k.CatalogKind,
    baseName: string,
  ): Promise<TruePointer | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.truePointerKey(scope, kind, baseName) }),
    );
    if (!res.Item) return undefined;
    const parsed = truePointerSchema.safeParse(res.Item);
    return parsed.success ? parsed.data : (res.Item as TruePointer);
  }

  /**
   * Repoint the per-baseName TRUE pointer to a variant (+ optional rev). This is
   * the ONLY write `promote` makes — it never edits or deletes a variant, so any
   * authed org member may call it. Idempotent (overwrites the single pointer row).
   */
  async setTrueVariant(scope: ScopeRef, kind: k.CatalogKind, pointer: TruePointer): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.truePointerKey(scope, kind, pointer.baseName), ...pointer },
      }),
    );
  }

  async listAgents(org: string, userId?: string): Promise<Agent[]> {
    return this.listCatalog<Agent>('AGENT', org, userId);
  }

  async listWorkflows(org: string, userId?: string): Promise<Workflow[]> {
    return this.listCatalog<Workflow>('WORKFLOW', org, userId);
  }

  async listSkills(org: string, userId?: string): Promise<Skill[]> {
    return this.listCatalog<Skill>('SKILL', org, userId);
  }

  async listMcpServers(org: string, userId?: string): Promise<McpServer[]> {
    return this.listCatalog<McpServer>('MCPSERVER', org, userId);
  }

  private async listScoped<T>(scope: ScopeRef, skPrefix: string): Promise<T[]> {
    // PAGINATE: a DynamoDB Query returns at most 1MB per page, so a single send
    // silently truncates a large catalog. Skill/MCP records carry whole-directory
    // `files` maps (SKILL.md + sibling scripts), so ~20 of them already exceed 1MB
    // — without this loop, `GET /skills` dropped every name past the first page
    // (e.g. all `hq-*`, which sort after `ce-*`), and the per-project sync then
    // couldn't see them and pruned them locally. Follow LastEvaluatedKey to the end.
    const items: Record<string, unknown>[] = [];
    let exclusiveStartKey: Record<string, unknown> | undefined;
    do {
      const res = await this.doc.send(
        new QueryCommand({
          TableName: this.table,
          KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
          ExpressionAttributeValues: { ':pk': k.scopePartition(scope), ':sk': skPrefix },
          ExclusiveStartKey: exclusiveStartKey,
        }),
      );
      for (const it of res.Items ?? []) items.push(it as Record<string, unknown>);
      exclusiveStartKey = res.LastEvaluatedKey as Record<string, unknown> | undefined;
    } while (exclusiveStartKey);
    // The catalog prefix scan (`SKILL#`/`AGENT#`/`MCPSERVER#`) now also matches
    // the versioning side-records (revision snapshots `#r<N>` and TRUE pointers
    // `#TRUE`) that share the same partition + prefix. Return only the live
    // "current" item records, never their history/pointers.
    return items.filter((it) => {
      const sk = (it as { SK?: string }).SK;
      // An item with no SK attribute (e.g. a projected/mocked row) is kept — only
      // skip rows whose SK is an actual versioning side-record.
      return sk === undefined || !k.isVersionSideRecord(sk);
    }) as T[];
  }

  // --- Project opt-in: enabledSkills / enabledAgents (org catalog) --------
  //
  // Opt-in lives on the project META record so a project's effective skill+agent
  // set is a single atomic read. All mutators load the project, edit the arrays,
  // and re-put through `putProject` (which persists both arrays). `getProject`
  // already returns the arrays (Zod defaults them to [] on legacy records).

  /** Idempotently enable a catalog skill on a project. Returns the updated Project. */
  async addSkillToProject(projectId: string, skillName: string): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    const enabledSkills = project.enabledSkills ?? [];
    if (!enabledSkills.includes(skillName)) {
      project.enabledSkills = [...enabledSkills, skillName];
      await this.putProject(project);
    }
    return project;
  }

  /** Disable a skill on a project. Returns the updated Project. */
  async removeSkillFromProject(projectId: string, skillName: string): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    project.enabledSkills = (project.enabledSkills ?? []).filter((s) => s !== skillName);
    await this.putProject(project);
    return project;
  }

  /**
   * Idempotently enable a whole BUNDLE on a project. `enabledBundles` records the
   * INTENT (the user added the bundle as a unit, so the UI can distinguish a
   * whole-bundle from an individually-picked member, and a later removal can strip
   * just the members this bundle contributed). `memberLeaves` is the bundle's
   * transitively-flattened leaf skills, which are ALWAYS also unioned into the flat
   * `enabledSkills` set the daemon materializes. This repo stays catalog-agnostic:
   * the REST layer flattens the bundle against the org catalog and passes the
   * leaves in. Returns the updated Project, or undefined if the project is missing.
   */
  async addBundleToProject(
    projectId: string,
    bundleName: string,
    memberLeaves: string[],
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    const enabledBundles = project.enabledBundles ?? [];
    if (!enabledBundles.includes(bundleName)) {
      project.enabledBundles = [...enabledBundles, bundleName];
    }
    const enabledSkills = new Set(project.enabledSkills ?? []);
    for (const leaf of memberLeaves) enabledSkills.add(leaf);
    project.enabledSkills = [...enabledSkills];
    await this.putProject(project);
    return project;
  }

  /**
   * Remove a BUNDLE intent from a project AND strip the leaf skills it contributed.
   * `leavesToRemove` is the set of leaves to drop from `enabledSkills`; the REST
   * layer computes it as the bundle's leaves MINUS any leaf still covered by another
   * still-enabled bundle, so a member shared between two enabled bundles survives.
   * Keeping that accounting in REST (where the org catalog is loaded) keeps this
   * repo catalog-agnostic. Returns the updated Project, or undefined if missing.
   */
  async removeBundleFromProject(
    projectId: string,
    bundleName: string,
    leavesToRemove: string[],
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    project.enabledBundles = (project.enabledBundles ?? []).filter((b) => b !== bundleName);
    const drop = new Set(leavesToRemove);
    project.enabledSkills = (project.enabledSkills ?? []).filter((s) => !drop.has(s));
    await this.putProject(project);
    return project;
  }

  /** Idempotently enable a catalog MCP server on a project. Returns the updated Project. */
  async addMcpServerToProject(projectId: string, serverName: string): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    const enabledMcpServers = project.enabledMcpServers ?? [];
    if (!enabledMcpServers.includes(serverName)) {
      project.enabledMcpServers = [...enabledMcpServers, serverName];
      await this.putProject(project);
    }
    return project;
  }

  /** Disable an MCP server on a project. Returns the updated Project. */
  async removeMcpServerFromProject(
    projectId: string,
    serverName: string,
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    project.enabledMcpServers = (project.enabledMcpServers ?? []).filter((s) => s !== serverName);
    await this.putProject(project);
    return project;
  }

  /**
   * Enable an agent on a project AND union the agent's declared skills (bundles
   * flattened transitively to leaf skills) into `enabledSkills`, plus the agent's
   * declared MCP servers (PLAIN names — there are no MCP bundles, so no flatten)
   * into `enabledMcpServers` — both de-duped. The agent + its skills are resolved
   * against the org catalog (`org` from the caller's principal). Returns updated
   * Project, or undefined if project/agent is missing.
   */
  async addAgentToProject(
    projectId: string,
    agentName: string,
    org: string,
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    const brought = await this.bringAgentInto(project, agentName, org);
    if (!brought) return undefined;
    await this.putProject(project);
    return project;
  }

  /**
   * Mutate an IN-MEMORY project to enable a single agent: union the agent's name
   * into `enabledAgents`, its declared skills (skill-bundles flattened to leaves)
   * into `enabledSkills`, and its declared MCP servers (plain names — no bundles)
   * into `enabledMcpServers`. Does NOT persist — the caller calls `putProject`
   * once after enabling one agent or a whole bundle of them. Returns false (and
   * leaves the project untouched) when the agent is not in the org catalog, so a
   * dangling bundle member is skipped rather than aborting the whole bundle.
   */
  private async bringAgentInto(project: Project, agentName: string, org: string): Promise<boolean> {
    const agent = await this.getAgent(orgScope(org), agentName);
    if (!agent) return false;
    const brought = await this.expandAgentSkills(org, agent);

    const enabledAgents = project.enabledAgents ?? [];
    project.enabledAgents = enabledAgents.includes(agentName)
      ? enabledAgents
      : [...enabledAgents, agentName];
    const enabledSkills = new Set(project.enabledSkills ?? []);
    for (const s of brought) enabledSkills.add(s);
    project.enabledSkills = [...enabledSkills];
    // Union the agent's declared MCP servers (flat plain names — no bundles).
    const enabledMcpServers = new Set(project.enabledMcpServers ?? []);
    for (const s of agent.mcpServers ?? []) enabledMcpServers.add(s);
    project.enabledMcpServers = [...enabledMcpServers];
    return true;
  }

  /**
   * Idempotently enable a whole AGENT BUNDLE on a project. `enabledAgentBundles`
   * records the INTENT (the user added the bundle as a unit, so the UI can tell a
   * whole-bundle from an individually-picked member, and a later removal can strip
   * just the members this bundle contributed). `memberAgents` is the bundle's
   * transitively-flattened leaf agents; each is brought in exactly as if enabled
   * individually (its skills + MCP servers union into `enabledSkills` /
   * `enabledMcpServers`). The REST layer flattens the bundle against the org
   * catalog and passes the members in, so this repo stays catalog-agnostic.
   * Returns the updated Project, or undefined if the project is missing.
   */
  async addAgentBundleToProject(
    projectId: string,
    bundleName: string,
    memberAgents: string[],
    org: string,
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    const enabledAgentBundles = project.enabledAgentBundles ?? [];
    if (!enabledAgentBundles.includes(bundleName)) {
      project.enabledAgentBundles = [...enabledAgentBundles, bundleName];
    }
    for (const member of memberAgents) {
      // A member that has vanished from the catalog is skipped, but the bundle
      // intent is still recorded so the project never holds a dead reference.
      await this.bringAgentInto(project, member, org);
    }
    await this.putProject(project);
    return project;
  }

  /**
   * Remove an AGENT BUNDLE intent from a project AND drop the member agents it
   * contributed from `enabledAgents`. `memberAgentsToRemove` is computed by the
   * REST layer as the bundle's members MINUS any member still covered by another
   * still-enabled agent bundle, so a member shared between two enabled bundles
   * survives. Mirrors `removeAgentFromProject`: `enabledSkills`/`enabledMcpServers`
   * are left intact, since a skill/server may be enabled directly or brought by
   * another agent. Returns the updated Project, or undefined if missing.
   */
  async removeAgentBundleFromProject(
    projectId: string,
    bundleName: string,
    memberAgentsToRemove: string[],
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    project.enabledAgentBundles = (project.enabledAgentBundles ?? []).filter(
      (b) => b !== bundleName,
    );
    const drop = new Set(memberAgentsToRemove);
    project.enabledAgents = (project.enabledAgents ?? []).filter((a) => !drop.has(a));
    await this.putProject(project);
    return project;
  }

  /**
   * Disable an agent on a project. Only `enabledAgents` is pruned — `enabledSkills`
   * is left intact, since a skill may be enabled directly or brought by another
   * agent. Returns the updated Project.
   */
  async removeAgentFromProject(projectId: string, agentName: string): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    project.enabledAgents = (project.enabledAgents ?? []).filter((a) => a !== agentName);
    await this.putProject(project);
    return project;
  }

  /**
   * Mutate an IN-MEMORY project to enable a single workflow: union the workflow's
   * name into `enabledWorkflows`, then for EACH node bring its referenced agent in
   * via `bringAgentInto` (so every agent the workflow runs — and transitively its
   * skills + MCP servers — is unioned into the project's enabled sets). Does NOT
   * persist — the caller calls `putProject` once. Returns false (and leaves the
   * project untouched) when the workflow is not in the org catalog, mirroring
   * `bringAgentInto`. A node whose agent has vanished from the catalog is skipped
   * (bringAgentInto returns false) rather than aborting the whole workflow.
   */
  private async bringWorkflowInto(
    project: Project,
    workflowName: string,
    org: string,
  ): Promise<boolean> {
    const workflow = await this.getWorkflow(orgScope(org), workflowName);
    if (!workflow) return false;

    const enabledWorkflows = project.enabledWorkflows ?? [];
    project.enabledWorkflows = enabledWorkflows.includes(workflowName)
      ? enabledWorkflows
      : [...enabledWorkflows, workflowName];
    // Union every referenced agent (and transitively its skills + MCP servers).
    for (const node of workflow.nodes) {
      await this.bringAgentInto(project, node.agent, org);
    }
    return true;
  }

  /**
   * Enable a workflow on a project AND union every agent its nodes reference (and
   * transitively those agents' skills + MCP servers) into the project's enabled
   * sets, reusing the same machinery that enabling an agent uses. The workflow +
   * its agents are resolved against the org catalog (`org` from the caller's
   * principal). Returns updated Project, or undefined if project/workflow is
   * missing.
   */
  async addWorkflowToProject(
    projectId: string,
    workflowName: string,
    org: string,
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    const brought = await this.bringWorkflowInto(project, workflowName, org);
    if (!brought) return undefined;
    await this.putProject(project);
    return project;
  }

  /**
   * Disable a workflow on a project. Only `enabledWorkflows` is pruned —
   * `enabledAgents`/`enabledSkills`/`enabledMcpServers` are left intact, since an
   * agent (or its skills/servers) may be enabled directly or brought by another
   * agent or workflow. Returns the updated Project.
   */
  async removeWorkflowFromProject(
    projectId: string,
    workflowName: string,
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    project.enabledWorkflows = (project.enabledWorkflows ?? []).filter((w) => w !== workflowName);
    await this.putProject(project);
    return project;
  }

  /**
   * Flatten an agent's declared `skills` to leaf-skill names, expanding any that
   * are bundles transitively (cycle-guarded) against the project's org catalog.
   */
  private async expandAgentSkills(org: string, agent: Agent): Promise<string[]> {
    const catalog = await this.listSkills(org);
    const byName = new Map(catalog.map((s) => [s.name, s]));
    return flattenBundle({ members: agent.skills }, byName);
  }

  // --- Weekly updates ----------------------------------------------------
  //
  // Objectives moved OUT of DynamoDB to Postgres (KTD7/U16) — see
  // `db/pg/objectivesRepo.ts`. The legacy Dynamo `RCDO#` items are read exactly
  // once by `db/pg/migrateObjectives.ts` (a Scan) and then are dead.

  // --- Org Definition of Done (plan-mapping feature 1) -------------------
  //
  // A single org-scoped record declaring what `/update-progress` must verify
  // before work is "done". Advisory — surfaced + reported on, never a gate.

  /**
   * The org's configured Definition of Done, or the org-wide default floor
   * (unit tests required, prod-E2E optional) when none has been set. Returning
   * the default means callers never have to special-case "unset".
   */
  async getOrgDod(org: string): Promise<DefinitionOfDone> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.orgDodKey(org) }),
    );
    const item = res.Item as (DefinitionOfDone & { org?: string }) | undefined;
    if (!item) return { ...DEFAULT_DEFINITION_OF_DONE };
    return {
      requiresUnitTests: item.requiresUnitTests,
      requiresProdE2E: item.requiresProdE2E,
      ...(item.notes !== undefined ? { notes: item.notes } : {}),
    };
  }

  /** Store the org's Definition of Done (full overwrite of the single record). */
  async putOrgDod(org: string, dod: DefinitionOfDone): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.orgDodKey(org), org, ...dod },
      }),
    );
  }

  async putWeekly(w: WeeklyUpdate): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.weeklyKey(w.projectId, w.isoWeek), ...w },
      }),
    );
  }

  async getWeekly(projectId: string, isoWeek: string): Promise<WeeklyUpdate | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.weeklyKey(projectId, isoWeek) }),
    );
    return res.Item as WeeklyUpdate | undefined;
  }

  async listWeekly(projectId: string): Promise<WeeklyUpdate[]> {
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': `PROJ#${projectId}`, ':sk': 'WEEK#' },
      }),
    );
    return (res.Items ?? []) as WeeklyUpdate[];
  }

  // --- Project memories (synced up from claude+) -------------------------

  /**
   * Every memory in a project, across all authors, in one partition read
   * (`begins_with(SK, 'MEM#')`). Each is parsed through memorySchema (falling
   * back to the raw item on failure, like getProject) so a malformed legacy item
   * never blanks the whole tab. The "Memories" tab groups these by `userId`.
   */
  async listMemories(projectId: string): Promise<Memory[]> {
    const { PK, skPrefix } = k.memoryPrefix(projectId);
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': PK, ':sk': skPrefix },
      }),
    );
    return (res.Items ?? []).map((item) => {
      const parsed = memorySchema.safeParse(item);
      return parsed.success ? parsed.data : (item as Memory);
    });
  }

  /**
   * Reconcile ONE author's memory set for a project to exactly `items`: PUT every
   * incoming memory (idempotent by key) and DELETE any of the author's existing
   * memories not in the incoming set. This is what makes deletions on disk
   * propagate — the daemon sends the whole current set and HQ mirrors it. Scoped
   * to `userId` via the per-author SK prefix, so one author's sync never touches
   * another's memories. `items` must already be stamped with projectId/userId.
   */
  async replaceUserMemories(projectId: string, userId: string, items: Memory[]): Promise<void> {
    const { PK, skPrefix } = k.memoryUserPrefix(projectId, userId);
    const existing = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': PK, ':sk': skPrefix },
        ProjectionExpression: 'SK',
      }),
    );
    const incomingSks = new Set(items.map((m) => k.memoryKey(projectId, userId, m.name).SK));
    const toDelete = (existing.Items ?? [])
      .map((it) => (it as { SK: string }).SK)
      .filter((sk) => !incomingSks.has(sk));

    // The set is small (a handful of facts per project), so individual writes via
    // Promise.all are simpler than BatchWrite and avoid its 25-item chunking.
    await Promise.all([
      ...items.map((m) =>
        this.doc.send(
          new PutCommand({
            TableName: this.table,
            Item: { ...k.memoryKey(projectId, userId, m.name), ...m },
          }),
        ),
      ),
      ...toDelete.map((sk) =>
        this.doc.send(new DeleteCommand({ TableName: this.table, Key: { PK, SK: sk } })),
      ),
    ]);
  }

  // --- Session learnings (topic-focus logging) ---------------------------

  /**
   * Persist a learning mined from a correction turn. Stored under the owning
   * PROJECT partition so a project's whole corpus is one partition read. The put
   * is UNCONDITIONAL: the SK is `LEARN#<sessionId>#<turnId>`, so re-emitting the
   * same `(sessionId, turnId)` overwrites the same item rather than duplicating
   * (idempotency is by key, not a conditional write).
   */
  async putLearning(rec: LearningRecord): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.learningKey(rec.projectId, rec.sessionId, rec.turnId), ...rec },
      }),
    );
  }

  /**
   * All learnings for a project, across every session, in one partition read
   * (`begins_with(SK, 'LEARN#')`). They come back sorted by the SK, so a
   * session's learnings group together and order deterministically by turnId.
   */
  async listLearnings(projectId: string): Promise<LearningRecord[]> {
    const { PK, skPrefix } = k.learningPrefix(projectId);
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': PK, ':sk': skPrefix },
      }),
    );
    return (res.Items ?? []) as LearningRecord[];
  }

  // --- Skill ideas + unassigned bin (skill-idea loop, U6) ----------------
  //
  // Ideas are co-located with skills in the org scope partition under an `IDEA#`
  // SK prefix, so they are invisible to `listSkills` (which scans `SKILL#`).
  // Corroboration is DERIVED (distinct sessionIds in `sources`), never stored.
  // The bin is the org's new-skill backlog under `IDEABIN#`.

  /**
   * Upsert an idea UNCONDITIONALLY (create or full overwrite). The SK is
   * `IDEA#<skillBaseName>#<ideaId>`, so re-putting the same id overwrites in
   * place. Use `corroborateIdeaConditional` for the optimistic-concurrency path
   * (adding a source / flipping status) where a concurrent fold must not clobber.
   */
  async putIdea(idea: Idea): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.ideaKey(idea.org, idea.skillBaseName, idea.ideaId), ...idea },
      }),
    );
  }

  async getIdea(org: string, skillBaseName: string, ideaId: string): Promise<Idea | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.ideaKey(org, skillBaseName, ideaId) }),
    );
    return res.Item as Idea | undefined;
  }

  /** Every idea attached to one skill family, in one partition read. */
  async listIdeasForSkill(org: string, skillBaseName: string): Promise<Idea[]> {
    const { PK, skPrefix } = k.ideaPrefixForSkill(org, skillBaseName);
    return this.queryPrefix<Idea>(PK, skPrefix);
  }

  /** Every idea in an org, across all skills, in one partition read. */
  async listIdeasForOrg(org: string): Promise<Idea[]> {
    const { PK, skPrefix } = k.ideaPrefixForOrg(org);
    return this.queryPrefix<Idea>(PK, skPrefix);
  }

  async deleteIdea(org: string, skillBaseName: string, ideaId: string): Promise<void> {
    await this.doc.send(
      new DeleteCommand({ TableName: this.table, Key: k.ideaKey(org, skillBaseName, ideaId) }),
    );
  }

  /**
   * U21 — skill delete/rename → idea orphan policy. When a skill family
   * (`baseName`) is removed from an org, its ideas must NOT be left as rows
   * pointing at a now-nonexistent skill. Instead each open idea CASCADES to the
   * org's unassigned bin (the new-skill backlog), preserving its corroboration
   * SIGNAL (the distinct-session `sources`) and provenance (`text` + snippets),
   * and the idea row is then deleted.
   *
   * Rename is delete+create today (there is no rename op), so the same cascade
   * covers it: the old name's ideas land in the bin, and the topic→skill loop
   * re-associates them onto the new name on its next topic event. HQ therefore
   * never shows ideas under a dead/renamed skill name.
   *
   * The bin `entryId` is derived deterministically from the idea
   * (`<skillBaseName>#<ideaId>`) so re-running the cascade is idempotent rather
   * than duplicating bin entries. `now` is injectable for deterministic tests.
   * Returns the number of ideas cascaded.
   */
  async cascadeSkillIdeasToBin(
    org: string,
    skillBaseName: string,
    opts: { now?: number } = {},
  ): Promise<{ cascaded: number }> {
    const now = opts.now ?? Date.now();
    const ideas = await this.listIdeasForSkill(org, skillBaseName);
    for (const idea of ideas) {
      const entry: UnassignedEntry = {
        entryId: `${skillBaseName}#${idea.ideaId}`,
        org,
        // The synthesized concept is preserved verbatim; an empty idea text
        // falls back to nothing rather than fabricating prose.
        text: idea.text,
        // Carry the full distinct-session provenance so corroboration is
        // preserved as a signal in the bin (a corroborated idea that lost its
        // home is a strong new-skill candidate).
        sources: idea.sources,
        createdAt: idea.createdAt,
        updatedAt: now,
      };
      await this.putUnassigned(entry);
      await this.deleteIdea(org, skillBaseName, idea.ideaId);
    }
    return { cascaded: ideas.length };
  }

  /**
   * Optimistic-concurrency idea write, mirroring `putSessionProjectionConditional`.
   * `expectedVersion` is the `corroborationVersion` the caller read before
   * mutating (adding a source, flipping status, recording a fold):
   *  - `undefined` requires the idea not to exist yet (first write),
   *  - a number requires the stored `corroborationVersion` to still equal it.
   *
   * On success the idea is written with `corroborationVersion` bumped to
   * `(expectedVersion ?? -1) + 1`, so the next conditional writer must observe
   * this write. A `ConditionalCheckFailedException` (a concurrent writer — e.g. a
   * fold that flipped `status` — advanced it) is surfaced as `{ written: false }`
   * so the caller can re-read, re-merge, and retry instead of clobbering the
   * concurrent update. The fold↔corroboration race is therefore safe.
   */
  async corroborateIdeaConditional(
    idea: Idea,
    expectedVersion: number | undefined,
  ): Promise<{ written: boolean }> {
    const next: Idea = { ...idea, corroborationVersion: (expectedVersion ?? -1) + 1 };
    const guard =
      expectedVersion === undefined
        ? { ConditionExpression: 'attribute_not_exists(PK)' }
        : {
            ConditionExpression: 'corroborationVersion = :expected',
            ExpressionAttributeValues: { ':expected': expectedVersion },
          };
    try {
      await this.doc.send(
        new PutCommand({
          TableName: this.table,
          Item: { ...k.ideaKey(next.org, next.skillBaseName, next.ideaId), ...next },
          ...guard,
        }),
      );
      return { written: true };
    } catch (err) {
      if ((err as { name?: string }).name === 'ConditionalCheckFailedException') {
        return { written: false };
      }
      throw err;
    }
  }

  /** Upsert an unassigned-bin entry (the org's new-skill backlog). */
  async putUnassigned(entry: UnassignedEntry): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.unassignedBinKey(entry.org, entry.entryId), ...entry },
      }),
    );
  }

  /** Every unassigned-bin entry in an org, in one partition read. */
  async listUnassignedForOrg(org: string): Promise<UnassignedEntry[]> {
    const { PK, skPrefix } = k.unassignedBinPrefix(org);
    return this.queryPrefix<UnassignedEntry>(PK, skPrefix);
  }

  // --- Golden-set regression cases (skill-idea loop, U18) ----------------
  //
  // Golden cases are co-located with skills under an `IDEAGOLD#` SK prefix, so
  // (like ideas) they are invisible to `listSkills`. Each fold records one case
  // (the before→after expectation); a later promote replays the family's cases
  // against the candidate body via the OpenRouter golden judge. The `caseId` is the
  // folded `ideaId`, so re-folding the same idea overwrites its case in place.

  /**
   * Upsert a golden case UNCONDITIONALLY. The SK is `IDEAGOLD#<baseName>#<caseId>`,
   * so re-putting the same `caseId` overwrites in place (re-folding an idea
   * refreshes its before→after rather than duplicating the case).
   */
  async putGoldenCase(c: GoldenCase): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.goldenCaseKey(c.org, c.skillBaseName, c.caseId), ...c },
      }),
    );
  }

  async getGoldenCase(
    org: string,
    skillBaseName: string,
    caseId: string,
  ): Promise<GoldenCase | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.goldenCaseKey(org, skillBaseName, caseId) }),
    );
    return res.Item as GoldenCase | undefined;
  }

  /** Every golden case guarding one skill family, in one partition read. */
  async listGoldenCasesForSkill(org: string, skillBaseName: string): Promise<GoldenCase[]> {
    const { PK, skPrefix } = k.goldenCasePrefixForSkill(org, skillBaseName);
    return this.queryPrefix<GoldenCase>(PK, skPrefix);
  }

  /** Paginated `PK = :pk AND begins_with(SK, :sk)` read (follows LastEvaluatedKey). */
  private async queryPrefix<T>(PK: string, skPrefix: string): Promise<T[]> {
    const items: Record<string, unknown>[] = [];
    let exclusiveStartKey: Record<string, unknown> | undefined;
    do {
      const res = await this.doc.send(
        new QueryCommand({
          TableName: this.table,
          KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
          ExpressionAttributeValues: { ':pk': PK, ':sk': skPrefix },
          ExclusiveStartKey: exclusiveStartKey,
        }),
      );
      for (const it of res.Items ?? []) items.push(it as Record<string, unknown>);
      exclusiveStartKey = res.LastEvaluatedKey as Record<string, unknown> | undefined;
    } while (exclusiveStartKey);
    return items as T[];
  }

  // --- Device-auth (wrapper device-code login) ---------------------------

  /** Persist a freshly-started pending device-auth record. */
  async putDeviceAuth(d: DeviceAuth): Promise<void> {
    // DynamoDB TimeToLive reaps items by a numeric epoch-SECONDS attribute named
    // `ttl` (infra enables TTL on this attribute). `expiresAt` is epoch ms, so
    // convert. Written on BOTH the record and its pointer so neither lingers.
    const ttl = Math.floor(d.expiresAt / 1000);
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.deviceAuthKey(d.deviceCode), ...d, ttl },
      }),
    );
    // userCode -> deviceCode pointer so an approver (who only holds the short
    // userCode) can resolve the opaque deviceCode the record is keyed by.
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: {
          ...k.deviceUserCodeKey(d.userCode),
          deviceCode: d.deviceCode,
          expiresAt: d.expiresAt,
          ttl,
        },
      }),
    );
  }

  async getDeviceAuth(deviceCode: string): Promise<DeviceAuth | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.deviceAuthKey(deviceCode) }),
    );
    return res.Item as DeviceAuth | undefined;
  }

  /** Resolve a user_code to its device_code via the pointer item (or undefined). */
  async getDeviceCodeByUserCode(userCode: string): Promise<string | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.deviceUserCodeKey(userCode) }),
    );
    return (res.Item as { deviceCode?: string } | undefined)?.deviceCode;
  }

  /**
   * Approve a pending record, binding it to an identity. Conditional on the
   * record existing and still being `pending`, so a double-approve is a no-op
   * that surfaces as `approved: false`.
   */
  async approveDeviceAuth(
    deviceCode: string,
    userId: string,
    org: string,
    opts: { now?: number; name?: string } = {},
  ): Promise<{ approved: boolean }> {
    const now = opts.now ?? Date.now();
    // The display name is optional; only set it when present so the minted device
    // token (and the wrapper's status line) can show the approver's username.
    const names: Record<string, string> = { '#s': 'status' };
    const values: Record<string, unknown> = {
      ':approved': 'approved',
      ':pending': 'pending',
      ':u': userId,
      ':o': org,
      ':now': now,
    };
    let setExpr = 'SET #s = :approved, userId = :u, org = :o';
    if (opts.name) {
      setExpr += ', #n = :n';
      names['#n'] = 'name';
      values[':n'] = opts.name;
    }
    try {
      await this.doc.send(
        new UpdateCommand({
          TableName: this.table,
          Key: k.deviceAuthKey(deviceCode),
          UpdateExpression: setExpr,
          // Expiry is part of the guard so a stale pointer (under DynamoDB TTL
          // lag) can't approve a timed-out record — defense in depth beneath the
          // app-layer check in approveDeviceAuthByUserCode.
          ConditionExpression: 'attribute_exists(PK) AND #s = :pending AND expiresAt > :now',
          ExpressionAttributeNames: names,
          ExpressionAttributeValues: values,
        }),
      );
      return { approved: true };
    } catch (err) {
      if ((err as { name?: string }).name === 'ConditionalCheckFailedException') {
        return { approved: false };
      }
      throw err;
    }
  }

  /**
   * Atomically consume an approved record so its token is issued exactly once.
   * Conditional on `status = approved`; a second consume fails the condition and
   * returns `consumed: false`, which the poll endpoint treats as already-claimed.
   */
  async consumeDeviceAuth(deviceCode: string): Promise<{ consumed: boolean }> {
    try {
      await this.doc.send(
        new UpdateCommand({
          TableName: this.table,
          Key: k.deviceAuthKey(deviceCode),
          UpdateExpression: 'SET #s = :consumed',
          ConditionExpression: 'attribute_exists(PK) AND #s = :approved',
          ExpressionAttributeNames: { '#s': 'status' },
          ExpressionAttributeValues: { ':consumed': 'consumed', ':approved': 'approved' },
        }),
      );
      return { consumed: true };
    } catch (err) {
      if ((err as { name?: string }).name === 'ConditionalCheckFailedException') {
        return { consumed: false };
      }
      throw err;
    }
  }

  // --- WebSocket connection registry (U6/U7) ------------------------------

  /**
   * Record a freshly-opened connection. For daemon connections we also write a
   * reverse `instanceId -> connectionId` index so the control gateway can route
   * a steer frame to the owning daemon without scanning.
   */
  async putConnection(c: ConnectionRecord): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.connectionKey(c.connectionId), ...c },
      }),
    );
    if (c.instanceId) {
      await this.doc.send(
        new PutCommand({
          TableName: this.table,
          Item: {
            ...k.instanceConnKey(c.instanceId),
            instanceId: c.instanceId,
            connectionId: c.connectionId,
            userId: c.userId,
          },
        }),
      );
    }
  }

  async getConnection(connectionId: string): Promise<ConnectionRecord | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.connectionKey(connectionId) }),
    );
    return res.Item as ConnectionRecord | undefined;
  }

  /** Resolve the daemon connectionId currently hosting an instance, if any. */
  async getInstanceConnectionId(instanceId: string): Promise<string | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.instanceConnKey(instanceId) }),
    );
    return (res.Item as { connectionId?: string } | undefined)?.connectionId;
  }

  /**
   * The userId currently bound to an instanceId via the reverse index, if any.
   * Used at `$connect` to reject one user claiming another user's instanceId
   * (control-routing hijack).
   */
  async getInstanceOwner(instanceId: string): Promise<string | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.instanceConnKey(instanceId) }),
    );
    return (res.Item as { userId?: string } | undefined)?.userId;
  }

  /** Remove a connection record (and its instance reverse index, if any). */
  async deleteConnection(connectionId: string): Promise<ConnectionRecord | undefined> {
    const existing = await this.getConnection(connectionId);
    await this.doc.send(
      new DeleteCommand({ TableName: this.table, Key: k.connectionKey(connectionId) }),
    );
    if (existing?.instanceId) {
      // Only clear the reverse index if it still points at this connection, so a
      // reconnect that already re-claimed the instance is not clobbered.
      const current = await this.getInstanceConnectionId(existing.instanceId);
      if (current === connectionId) {
        await this.doc.send(
          new DeleteCommand({
            TableName: this.table,
            Key: k.instanceConnKey(existing.instanceId),
          }),
        );
      }
    }
    return existing;
  }

  // --- Live-feed subscriptions (fan-out listeners) -----------------------

  /** Register a web connection as a listener on a session's live feed. */
  async addListener(sessionId: string, connectionId: string): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.listenerKey(sessionId, connectionId), sessionId, connectionId },
      }),
    );
  }

  async removeListener(sessionId: string, connectionId: string): Promise<void> {
    await this.doc.send(
      new DeleteCommand({ TableName: this.table, Key: k.listenerKey(sessionId, connectionId) }),
    );
  }

  /** All connectionIds currently subscribed to a session, for event fan-out. */
  async listListeners(sessionId: string): Promise<string[]> {
    const { PK, skPrefix } = k.listenerPrefix(sessionId);
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': PK, ':sk': skPrefix },
      }),
    );
    return (res.Items ?? []).map((i) => (i as { connectionId: string }).connectionId);
  }
}
