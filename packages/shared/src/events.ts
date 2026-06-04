import { z } from 'zod';

/**
 * The session event contract. This is the wire format the claude+ wrapper emits
 * and the backend ingests. The Go wrapper re-declares the same envelope and is
 * kept honest by the golden fixture both sides parse (see test/golden).
 */

export const SESSION_STATUSES = ['active', 'needs_input', 'idle', 'done'] as const;
export const sessionStatusSchema = z.enum(SESSION_STATUSES);
export type SessionStatus = z.infer<typeof sessionStatusSchema>;

const sessionId = z.string().min(1);

export const sessionStartEventSchema = z.object({
  kind: z.literal('session.start'),
  sessionId,
  projectId: z.string().min(1),
  host: z.string().min(1),
  name: z.string().min(1),
  agent: z.string().min(1).optional(),
  // Human/repo display name for the project — e.g. the git remote "owner/repo"
  // or the actual repo folder name. Optional for backward compatibility: older
  // daemons omit it, in which case the backend falls back to the projectId slug.
  repo: z.string().min(1).optional(),
});

export const sessionRenameEventSchema = z.object({
  kind: z.literal('session.rename'),
  sessionId,
  name: z.string().min(1),
  // The raw first prompt the user typed, carried by the UserPromptSubmit hook so
  // the Sessions read model can show it. Optional for backward compatibility:
  // manual renames and older daemons omit it.
  summary: z.string().optional(),
});

// Cap on any single carried-content field (user/assistant text, tool args, tool
// result) so a giant transcript block can't blow up an envelope or the table
// item. The wrapper truncates to this; the schema does not re-validate length.
export const MAX_CONTENT_CHARS = 8000;

export const userMsgEventSchema = z.object({
  kind: z.literal('user.msg'),
  sessionId,
  tokens: z.number().int().nonnegative(),
  // The actual user-turn text (truncated). Optional for backward compatibility:
  // older daemons emit only the token count. When present the live-watch feed
  // renders the real prompt instead of just "you · N tok".
  text: z.string().optional(),
});

export const assistantMsgEventSchema = z.object({
  kind: z.literal('assistant.msg'),
  sessionId,
  tokens: z.number().int().nonnegative(),
  // The actual assistant-reply text (truncated). Optional for backward
  // compatibility; when present the feed renders Claude's real words.
  text: z.string().optional(),
});

export const toolCallEventSchema = z.object({
  kind: z.literal('tool.call'),
  sessionId,
  tool: z.string().min(1),
  argsSummary: z.string().default(''),
});

export const toolResultEventSchema = z.object({
  kind: z.literal('tool.result'),
  sessionId,
  ok: z.boolean(),
  ms: z.number().int().nonnegative(),
  summary: z.string().default(''),
});

export const costTickEventSchema = z.object({
  kind: z.literal('cost.tick'),
  sessionId,
  deltaUsd: z.number().nonnegative(),
  totalUsd: z.number().nonnegative(),
  tokens: z.number().int().nonnegative(),
});

export const statusChangeEventSchema = z.object({
  kind: z.literal('status.change'),
  sessionId,
  from: sessionStatusSchema,
  to: sessionStatusSchema,
});

// Periodic liveness ping a daemon emits (~every 20s) for each LIVE session, even
// when the session is idle. The backend bumps lastEventAt on receipt so a
// genuinely-alive idle session stays live; a powered-off laptop stops sending
// these, so read-time freshness drops its sessions within the stale window.
export const sessionHeartbeatEventSchema = z.object({
  kind: z.literal('session.heartbeat'),
  sessionId,
});

export const eventSchema = z.discriminatedUnion('kind', [
  sessionStartEventSchema,
  sessionRenameEventSchema,
  userMsgEventSchema,
  assistantMsgEventSchema,
  toolCallEventSchema,
  toolResultEventSchema,
  costTickEventSchema,
  statusChangeEventSchema,
  sessionHeartbeatEventSchema,
]);
export type Event = z.infer<typeof eventSchema>;
export type EventKind = Event['kind'];

/** The transport envelope wrapping every event sent from a daemon to HQ. */
export const envelopeSchema = z.object({
  v: z.literal(1),
  instanceId: z.string().min(1),
  host: z.string().min(1),
  ts: z.number().int().nonnegative(), // epoch milliseconds
  seq: z.number().int().nonnegative(), // monotonic per session
  event: eventSchema,
});
export type Envelope = z.infer<typeof envelopeSchema>;

/** Parse + validate an unknown value as an Envelope. Throws on invalid input. */
export function parseEnvelope(value: unknown): Envelope {
  return envelopeSchema.parse(value);
}

/** Safe variant returning a zod SafeParseReturnType. */
export function safeParseEnvelope(value: unknown) {
  return envelopeSchema.safeParse(value);
}
