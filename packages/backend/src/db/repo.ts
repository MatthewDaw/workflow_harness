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
  ObjectiveNode,
  Project,
  ScopeRef,
  SessionProjection,
  SessionVector,
  Skill,
  WeeklyUpdate,
} from '@harness/shared';
import { DEFAULT_DEFINITION_OF_DONE, orgScope, projectSchema } from '@harness/shared';
import type { z } from 'zod';
import * as k from './keys.js';

/** The pre-parse shape of a Project (enabledSkills/enabledAgents optional via defaults). */
type ProjectInput = z.input<typeof projectSchema>;

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
 * Intent-named access layer over the single `harness` table. Handlers depend on
 * this, never on raw DynamoDB commands. The DynamoDBDocumentClient is injected
 * so it can be mocked in tests.
 */
export class Repo {
  constructor(
    private readonly doc: DynamoDBDocumentClient,
    private readonly table: string = k.tableName(),
  ) {}

  // --- Projects -----------------------------------------------------------

  async putProject(p: Project): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.projectKey(p.id), ...p, ...k.projectOwnerIndex(p.ownerUserId, p.id) },
      }),
    );
  }

  /**
   * Create a Project only if one does not already exist (conditional PutItem).
   * Mirrors `putProject`'s Item but guards on `attribute_not_exists(PK)`, so a
   * later session for the same repo never clobbers a curated name/progress/
   * framing. Called on each daemon `session.start`, so it must be idempotent: a
   * ConditionalCheckFailedException is swallowed and reported as `created:false`.
   */
  async ensureProject(input: ProjectInput): Promise<{ created: boolean }> {
    // Parse so the org-catalog opt-in arrays (enabledSkills/enabledAgents) and
    // other defaults are filled in for callers that supply only the core fields.
    const p: Project = projectSchema.parse(input);
    try {
      await this.doc.send(
        new PutCommand({
          TableName: this.table,
          Item: { ...k.projectKey(p.id), ...p, ...k.projectOwnerIndex(p.ownerUserId, p.id) },
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
        UpdateExpression:
          'SET progressPct = :p, framingReadAt = :r, framingStale = :s',
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
   */
  async appendEvent(env: Envelope): Promise<{ stored: boolean }> {
    try {
      await this.doc.send(
        new PutCommand({
          TableName: this.table,
          Item: { ...k.eventKey(env.event.sessionId, env.seq), ...env },
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

  // --- Agents + skills (scoped) ------------------------------------------

  async putAgent(a: Agent): Promise<void> {
    await this.doc.send(
      new PutCommand({ TableName: this.table, Item: { ...k.agentKey(a.scope, a.name), ...a } }),
    );
  }

  async getAgent(scope: ScopeRef, name: string): Promise<Agent | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.agentKey(scope, name) }),
    );
    return res.Item as Agent | undefined;
  }

  async deleteAgent(scope: ScopeRef, name: string): Promise<void> {
    await this.doc.send(new DeleteCommand({ TableName: this.table, Key: k.agentKey(scope, name) }));
  }

  async putSkill(s: Skill): Promise<void> {
    await this.doc.send(
      new PutCommand({ TableName: this.table, Item: { ...k.skillKey(s.scope, s.name), ...s } }),
    );
  }

  async getSkill(scope: ScopeRef, name: string): Promise<Skill | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.skillKey(scope, name) }),
    );
    return res.Item as Skill | undefined;
  }

  async deleteSkill(scope: ScopeRef, name: string): Promise<void> {
    await this.doc.send(new DeleteCommand({ TableName: this.table, Key: k.skillKey(scope, name) }));
  }

  /** The org catalog of agents (org-only; the 3-tier scope is retired). */
  async listAgents(org: string): Promise<Agent[]> {
    return this.listScoped<Agent>(orgScope(org), 'AGENT#');
  }

  /** The org catalog of skills (org-only; the 3-tier scope is retired). */
  async listSkills(org: string): Promise<Skill[]> {
    return this.listScoped<Skill>(orgScope(org), 'SKILL#');
  }

  private async listScoped<T>(scope: ScopeRef, skPrefix: string): Promise<T[]> {
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': k.scopePartition(scope), ':sk': skPrefix },
      }),
    );
    return (res.Items ?? []) as T[];
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
   * Enable an agent on a project AND union the agent's declared skills (bundles
   * flattened transitively to leaf skills) into `enabledSkills` (de-duped). The
   * agent + its skills are resolved against the org catalog (`org` from the
   * caller's principal). Returns updated Project, or undefined if project/agent
   * is missing.
   */
  async addAgentToProject(
    projectId: string,
    agentName: string,
    org: string,
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    const agent = await this.getAgent(orgScope(org), agentName);
    if (!agent) return undefined;
    const brought = await this.expandAgentSkills(org, agent);

    const enabledAgents = project.enabledAgents ?? [];
    if (!enabledAgents.includes(agentName)) {
      project.enabledAgents = [...enabledAgents, agentName];
    }
    const enabledSkills = new Set(project.enabledSkills ?? []);
    for (const s of brought) enabledSkills.add(s);
    project.enabledSkills = [...enabledSkills];
    await this.putProject(project);
    return project;
  }

  /**
   * Disable an agent on a project. Only `enabledAgents` is pruned — `enabledSkills`
   * is left intact, since a skill may be enabled directly or brought by another
   * agent. Returns the updated Project.
   */
  async removeAgentFromProject(
    projectId: string,
    agentName: string,
  ): Promise<Project | undefined> {
    const project = await this.getProject(projectId);
    if (!project) return undefined;
    project.enabledAgents = (project.enabledAgents ?? []).filter((a) => a !== agentName);
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
    const leaves: string[] = [];
    const seen = new Set<string>();
    const walk = (name: string): void => {
      if (seen.has(name)) return;
      seen.add(name);
      const s = byName.get(name);
      if (s?.kind === 'bundle') {
        for (const m of s.members) walk(m);
      } else {
        leaves.push(name);
      }
    };
    for (const name of agent.skills) walk(name);
    return [...new Set(leaves)];
  }

  // --- Objectives + weekly updates ---------------------------------------

  async putObjective(o: ObjectiveNode): Promise<void> {
    await this.doc.send(
      new PutCommand({ TableName: this.table, Item: { ...k.objectiveKey(o.org, o.id), ...o } }),
    );
  }

  async getObjective(org: string, id: string): Promise<ObjectiveNode | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.objectiveKey(org, id) }),
    );
    return res.Item as ObjectiveNode | undefined;
  }

  async deleteObjective(org: string, id: string): Promise<void> {
    await this.doc.send(new DeleteCommand({ TableName: this.table, Key: k.objectiveKey(org, id) }));
  }

  async listObjectives(org: string): Promise<ObjectiveNode[]> {
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': `ORG#${org}`, ':sk': 'RCDO#' },
      }),
    );
    return (res.Items ?? []) as ObjectiveNode[];
  }

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

  // --- Forge session vectors (U27) ---------------------------------------

  /** Persist a summarized + embedded session vector for the brute-force k-NN. */
  async putSessionVector(v: SessionVector): Promise<void> {
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.sessionVectorKey(v.userId, v.sessionId), ...v },
      }),
    );
  }

  /** All of a user's session vectors, for the brute-force cosine fallback. */
  async listSessionVectors(userId: string): Promise<SessionVector[]> {
    const { PK, skPrefix } = k.sessionVectorPrefix(userId);
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': PK, ':sk': skPrefix },
      }),
    );
    return (res.Items ?? []) as SessionVector[];
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
    now: number = Date.now(),
  ): Promise<{ approved: boolean }> {
    try {
      await this.doc.send(
        new UpdateCommand({
          TableName: this.table,
          Key: k.deviceAuthKey(deviceCode),
          UpdateExpression: 'SET #s = :approved, userId = :u, org = :o',
          // Expiry is part of the guard so a stale pointer (under DynamoDB TTL
          // lag) can't approve a timed-out record — defense in depth beneath the
          // app-layer check in approveDeviceAuthByUserCode.
          ConditionExpression: 'attribute_exists(PK) AND #s = :pending AND expiresAt > :now',
          ExpressionAttributeNames: { '#s': 'status' },
          ExpressionAttributeValues: {
            ':approved': 'approved',
            ':pending': 'pending',
            ':u': userId,
            ':o': org,
            ':now': now,
          },
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
