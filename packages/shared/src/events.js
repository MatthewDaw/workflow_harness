import { z } from 'zod';
/**
 * The session event contract. This is the wire format the claude+ wrapper emits
 * and the backend ingests. The Go wrapper re-declares the same envelope and is
 * kept honest by the golden fixture both sides parse (see test/golden).
 */
export const SESSION_STATUSES = ['active', 'needs_input', 'idle', 'done'];
export const sessionStatusSchema = z.enum(SESSION_STATUSES);
const sessionId = z.string().min(1);
export const sessionStartEventSchema = z.object({
    kind: z.literal('session.start'),
    sessionId,
    projectId: z.string().min(1),
    host: z.string().min(1),
    name: z.string().min(1),
    agent: z.string().min(1).optional(),
    ticket: z.string().min(1).optional(),
});
export const sessionRenameEventSchema = z.object({
    kind: z.literal('session.rename'),
    sessionId,
    name: z.string().min(1),
});
export const userMsgEventSchema = z.object({
    kind: z.literal('user.msg'),
    sessionId,
    tokens: z.number().int().nonnegative(),
});
export const assistantMsgEventSchema = z.object({
    kind: z.literal('assistant.msg'),
    sessionId,
    tokens: z.number().int().nonnegative(),
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
export const eventSchema = z.discriminatedUnion('kind', [
    sessionStartEventSchema,
    sessionRenameEventSchema,
    userMsgEventSchema,
    assistantMsgEventSchema,
    toolCallEventSchema,
    toolResultEventSchema,
    costTickEventSchema,
    statusChangeEventSchema,
]);
/** The transport envelope wrapping every event sent from a daemon to HQ. */
export const envelopeSchema = z.object({
    v: z.literal(1),
    instanceId: z.string().min(1),
    host: z.string().min(1),
    ts: z.number().int().nonnegative(), // epoch milliseconds
    seq: z.number().int().nonnegative(), // monotonic per session
    event: eventSchema,
});
/** Parse + validate an unknown value as an Envelope. Throws on invalid input. */
export function parseEnvelope(value) {
    return envelopeSchema.parse(value);
}
/** Safe variant returning a zod SafeParseReturnType. */
export function safeParseEnvelope(value) {
    return envelopeSchema.safeParse(value);
}
//# sourceMappingURL=events.js.map