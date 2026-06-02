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
  DeviceAuth,
  Envelope,
  ObjectiveNode,
  Project,
  ScopeRef,
  SessionProjection,
  SessionVector,
  Skill,
  Ticket,
  WeeklyUpdate,
} from '@harness/shared';
import * as k from './keys.js';

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

  async getProject(projectId: string): Promise<Project | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.projectKey(projectId) }),
    );
    return res.Item as Project | undefined;
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
    const live = k.liveSessionIndex(s.status, s.lastEventAt, s.sessionId);
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.sessionKey(s.projectId, s.sessionId), ...s, ...(live ?? {}) },
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

  // --- Tickets ------------------------------------------------------------

  async putTicket(t: Ticket): Promise<void> {
    await this.doc.send(
      new PutCommand({ TableName: this.table, Item: { ...k.ticketKey(t.projectId, t.id), ...t } }),
    );
  }

  async getTicket(projectId: string, ticketId: string): Promise<Ticket | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.ticketKey(projectId, ticketId) }),
    );
    return res.Item as Ticket | undefined;
  }

  async listTickets(projectId: string): Promise<Ticket[]> {
    const res = await this.doc.send(
      new QueryCommand({
        TableName: this.table,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
        ExpressionAttributeValues: { ':pk': `PROJ#${projectId}`, ':sk': 'TICK#' },
      }),
    );
    return (res.Items ?? []) as Ticket[];
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

  /** Fetch all agents across the given scopes (caller resolves narrowest-wins). */
  async listAgents(scopes: ScopeRef[]): Promise<Agent[]> {
    return this.listScoped<Agent>(scopes, 'AGENT#');
  }

  async listSkills(scopes: ScopeRef[]): Promise<Skill[]> {
    return this.listScoped<Skill>(scopes, 'SKILL#');
  }

  private async listScoped<T>(scopes: ScopeRef[], skPrefix: string): Promise<T[]> {
    const results = await Promise.all(
      scopes.map((scope) =>
        this.doc.send(
          new QueryCommand({
            TableName: this.table,
            KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
            ExpressionAttributeValues: { ':pk': k.scopePartition(scope), ':sk': skPrefix },
          }),
        ),
      ),
    );
    return results.flatMap((r) => (r.Items ?? []) as T[]);
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
    await this.doc.send(
      new PutCommand({
        TableName: this.table,
        Item: { ...k.deviceAuthKey(d.deviceCode), ...d },
      }),
    );
  }

  async getDeviceAuth(deviceCode: string): Promise<DeviceAuth | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.deviceAuthKey(deviceCode) }),
    );
    return res.Item as DeviceAuth | undefined;
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
  ): Promise<{ approved: boolean }> {
    try {
      await this.doc.send(
        new UpdateCommand({
          TableName: this.table,
          Key: k.deviceAuthKey(deviceCode),
          UpdateExpression: 'SET #s = :approved, userId = :u, org = :o',
          ConditionExpression: 'attribute_exists(PK) AND #s = :pending',
          ExpressionAttributeNames: { '#s': 'status' },
          ExpressionAttributeValues: {
            ':approved': 'approved',
            ':pending': 'pending',
            ':u': userId,
            ':o': org,
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
