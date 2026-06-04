import type { Envelope, SessionProjection } from '@harness/shared';

/**
 * Fold a single event envelope into a session's current-state projection.
 *
 * The projection is the read model behind the Sessions list and the live-watch
 * header (status, name, tokens, cost). Ingestion (U6) calls this for every
 * accepted envelope and persists the result.
 *
 * Ordering: events can arrive out of order or be replayed after a daemon
 * reconnect. We track the highest `seq` folded so far (`maxSeq`) and only let an
 * envelope advance the "latest activity" fields when its seq is newer. A
 * `session.start` always establishes/normalises the identity fields even if it
 * arrives late, since those are stable for the session's lifetime.
 */

/** Owner context attached to a freshly-created projection (for control authz). */
export interface ProjectionOwner {
  userId: string;
  instanceId: string;
}

export function applyEvent(
  prev: SessionProjection | undefined,
  env: Envelope,
  owner?: ProjectionOwner,
): SessionProjection {
  const ev = env.event;

  // Bootstrap a projection on the first event we see for a session. A
  // `session.start` gives us full identity; any other first event still yields a
  // usable placeholder so the session is visible while start replays.
  const base: SessionProjection =
    prev ??
    (ev.kind === 'session.start'
      ? {
          sessionId: ev.sessionId,
          projectId: ev.projectId,
          name: ev.name,
          host: env.host,
          agent: ev.agent,
          status: 'active',
          tokens: 0,
          costUsd: 0,
          startedAt: env.ts,
          lastEventAt: env.ts,
          maxSeq: 0,
        }
      : {
          sessionId: ev.sessionId,
          projectId: 'unknown',
          name: ev.sessionId,
          host: env.host,
          status: 'active',
          tokens: 0,
          costUsd: 0,
          startedAt: env.ts,
          lastEventAt: env.ts,
          maxSeq: 0,
        });

  const next: SessionProjection = {
    ...base,
    instanceId: base.instanceId ?? owner?.instanceId ?? env.instanceId,
    ownerUserId: base.ownerUserId ?? owner?.userId,
  };

  const isNewer = env.seq > base.maxSeq;

  // Identity-establishing fields apply regardless of ordering.
  if (ev.kind === 'session.start') {
    next.projectId = ev.projectId;
    next.name = ev.name;
    next.host = env.host;
    next.agent = ev.agent;
    if (base.startedAt === 0 || env.ts < base.startedAt) next.startedAt = env.ts;
  }

  // Latest-activity fields only advance for the newest seq seen so far. This is
  // what makes out-of-order delivery safe and duplicates a no-op.
  if (!isNewer) {
    return next;
  }

  next.maxSeq = env.seq;
  next.lastEventAt = Math.max(base.lastEventAt, env.ts);

  switch (ev.kind) {
    case 'session.rename':
      next.name = ev.name;
      if (ev.summary !== undefined) next.summary = ev.summary;
      break;
    case 'user.msg':
    case 'assistant.msg':
      next.tokens = base.tokens + ev.tokens;
      break;
    case 'cost.tick':
      next.tokens = ev.tokens;
      next.costUsd = ev.totalUsd;
      break;
    case 'status.change':
      next.status = ev.to;
      break;
    case 'tool.call':
    case 'tool.result':
    case 'session.start':
      // Activity timestamp already bumped above; no field change.
      break;
  }

  return next;
}
