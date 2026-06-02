import {
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
  Skill,
  Ticket,
  WeeklyUpdate,
} from '@harness/shared';
import * as k from './keys.js';

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
  }

  async getSession(projectId: string, sessionId: string): Promise<SessionProjection | undefined> {
    const res = await this.doc.send(
      new GetCommand({ TableName: this.table, Key: k.sessionKey(projectId, sessionId) }),
    );
    return res.Item as SessionProjection | undefined;
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

  async putSkill(s: Skill): Promise<void> {
    await this.doc.send(
      new PutCommand({ TableName: this.table, Item: { ...k.skillKey(s.scope, s.name), ...s } }),
    );
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
}
