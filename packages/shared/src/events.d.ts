import { z } from 'zod';
/**
 * The session event contract. This is the wire format the claude+ wrapper emits
 * and the backend ingests. The Go wrapper re-declares the same envelope and is
 * kept honest by the golden fixture both sides parse (see test/golden).
 */
export declare const SESSION_STATUSES: readonly ["active", "needs_input", "idle", "done"];
export declare const sessionStatusSchema: z.ZodEnum<["active", "needs_input", "idle", "done"]>;
export type SessionStatus = z.infer<typeof sessionStatusSchema>;
export declare const sessionStartEventSchema: z.ZodObject<{
    kind: z.ZodLiteral<"session.start">;
    sessionId: z.ZodString;
    projectId: z.ZodString;
    host: z.ZodString;
    name: z.ZodString;
    agent: z.ZodOptional<z.ZodString>;
    ticket: z.ZodOptional<z.ZodString>;
}, "strip", z.ZodTypeAny, {
    kind: "session.start";
    sessionId: string;
    projectId: string;
    host: string;
    name: string;
    agent?: string | undefined;
    ticket?: string | undefined;
}, {
    kind: "session.start";
    sessionId: string;
    projectId: string;
    host: string;
    name: string;
    agent?: string | undefined;
    ticket?: string | undefined;
}>;
export declare const sessionRenameEventSchema: z.ZodObject<{
    kind: z.ZodLiteral<"session.rename">;
    sessionId: z.ZodString;
    name: z.ZodString;
}, "strip", z.ZodTypeAny, {
    kind: "session.rename";
    sessionId: string;
    name: string;
}, {
    kind: "session.rename";
    sessionId: string;
    name: string;
}>;
export declare const userMsgEventSchema: z.ZodObject<{
    kind: z.ZodLiteral<"user.msg">;
    sessionId: z.ZodString;
    tokens: z.ZodNumber;
}, "strip", z.ZodTypeAny, {
    kind: "user.msg";
    sessionId: string;
    tokens: number;
}, {
    kind: "user.msg";
    sessionId: string;
    tokens: number;
}>;
export declare const assistantMsgEventSchema: z.ZodObject<{
    kind: z.ZodLiteral<"assistant.msg">;
    sessionId: z.ZodString;
    tokens: z.ZodNumber;
}, "strip", z.ZodTypeAny, {
    kind: "assistant.msg";
    sessionId: string;
    tokens: number;
}, {
    kind: "assistant.msg";
    sessionId: string;
    tokens: number;
}>;
export declare const toolCallEventSchema: z.ZodObject<{
    kind: z.ZodLiteral<"tool.call">;
    sessionId: z.ZodString;
    tool: z.ZodString;
    argsSummary: z.ZodDefault<z.ZodString>;
}, "strip", z.ZodTypeAny, {
    kind: "tool.call";
    sessionId: string;
    tool: string;
    argsSummary: string;
}, {
    kind: "tool.call";
    sessionId: string;
    tool: string;
    argsSummary?: string | undefined;
}>;
export declare const toolResultEventSchema: z.ZodObject<{
    kind: z.ZodLiteral<"tool.result">;
    sessionId: z.ZodString;
    ok: z.ZodBoolean;
    ms: z.ZodNumber;
    summary: z.ZodDefault<z.ZodString>;
}, "strip", z.ZodTypeAny, {
    kind: "tool.result";
    sessionId: string;
    ok: boolean;
    ms: number;
    summary: string;
}, {
    kind: "tool.result";
    sessionId: string;
    ok: boolean;
    ms: number;
    summary?: string | undefined;
}>;
export declare const costTickEventSchema: z.ZodObject<{
    kind: z.ZodLiteral<"cost.tick">;
    sessionId: z.ZodString;
    deltaUsd: z.ZodNumber;
    totalUsd: z.ZodNumber;
    tokens: z.ZodNumber;
}, "strip", z.ZodTypeAny, {
    kind: "cost.tick";
    sessionId: string;
    tokens: number;
    deltaUsd: number;
    totalUsd: number;
}, {
    kind: "cost.tick";
    sessionId: string;
    tokens: number;
    deltaUsd: number;
    totalUsd: number;
}>;
export declare const statusChangeEventSchema: z.ZodObject<{
    kind: z.ZodLiteral<"status.change">;
    sessionId: z.ZodString;
    from: z.ZodEnum<["active", "needs_input", "idle", "done"]>;
    to: z.ZodEnum<["active", "needs_input", "idle", "done"]>;
}, "strip", z.ZodTypeAny, {
    kind: "status.change";
    sessionId: string;
    from: "active" | "needs_input" | "idle" | "done";
    to: "active" | "needs_input" | "idle" | "done";
}, {
    kind: "status.change";
    sessionId: string;
    from: "active" | "needs_input" | "idle" | "done";
    to: "active" | "needs_input" | "idle" | "done";
}>;
export declare const eventSchema: z.ZodDiscriminatedUnion<"kind", [z.ZodObject<{
    kind: z.ZodLiteral<"session.start">;
    sessionId: z.ZodString;
    projectId: z.ZodString;
    host: z.ZodString;
    name: z.ZodString;
    agent: z.ZodOptional<z.ZodString>;
    ticket: z.ZodOptional<z.ZodString>;
}, "strip", z.ZodTypeAny, {
    kind: "session.start";
    sessionId: string;
    projectId: string;
    host: string;
    name: string;
    agent?: string | undefined;
    ticket?: string | undefined;
}, {
    kind: "session.start";
    sessionId: string;
    projectId: string;
    host: string;
    name: string;
    agent?: string | undefined;
    ticket?: string | undefined;
}>, z.ZodObject<{
    kind: z.ZodLiteral<"session.rename">;
    sessionId: z.ZodString;
    name: z.ZodString;
}, "strip", z.ZodTypeAny, {
    kind: "session.rename";
    sessionId: string;
    name: string;
}, {
    kind: "session.rename";
    sessionId: string;
    name: string;
}>, z.ZodObject<{
    kind: z.ZodLiteral<"user.msg">;
    sessionId: z.ZodString;
    tokens: z.ZodNumber;
}, "strip", z.ZodTypeAny, {
    kind: "user.msg";
    sessionId: string;
    tokens: number;
}, {
    kind: "user.msg";
    sessionId: string;
    tokens: number;
}>, z.ZodObject<{
    kind: z.ZodLiteral<"assistant.msg">;
    sessionId: z.ZodString;
    tokens: z.ZodNumber;
}, "strip", z.ZodTypeAny, {
    kind: "assistant.msg";
    sessionId: string;
    tokens: number;
}, {
    kind: "assistant.msg";
    sessionId: string;
    tokens: number;
}>, z.ZodObject<{
    kind: z.ZodLiteral<"tool.call">;
    sessionId: z.ZodString;
    tool: z.ZodString;
    argsSummary: z.ZodDefault<z.ZodString>;
}, "strip", z.ZodTypeAny, {
    kind: "tool.call";
    sessionId: string;
    tool: string;
    argsSummary: string;
}, {
    kind: "tool.call";
    sessionId: string;
    tool: string;
    argsSummary?: string | undefined;
}>, z.ZodObject<{
    kind: z.ZodLiteral<"tool.result">;
    sessionId: z.ZodString;
    ok: z.ZodBoolean;
    ms: z.ZodNumber;
    summary: z.ZodDefault<z.ZodString>;
}, "strip", z.ZodTypeAny, {
    kind: "tool.result";
    sessionId: string;
    ok: boolean;
    ms: number;
    summary: string;
}, {
    kind: "tool.result";
    sessionId: string;
    ok: boolean;
    ms: number;
    summary?: string | undefined;
}>, z.ZodObject<{
    kind: z.ZodLiteral<"cost.tick">;
    sessionId: z.ZodString;
    deltaUsd: z.ZodNumber;
    totalUsd: z.ZodNumber;
    tokens: z.ZodNumber;
}, "strip", z.ZodTypeAny, {
    kind: "cost.tick";
    sessionId: string;
    tokens: number;
    deltaUsd: number;
    totalUsd: number;
}, {
    kind: "cost.tick";
    sessionId: string;
    tokens: number;
    deltaUsd: number;
    totalUsd: number;
}>, z.ZodObject<{
    kind: z.ZodLiteral<"status.change">;
    sessionId: z.ZodString;
    from: z.ZodEnum<["active", "needs_input", "idle", "done"]>;
    to: z.ZodEnum<["active", "needs_input", "idle", "done"]>;
}, "strip", z.ZodTypeAny, {
    kind: "status.change";
    sessionId: string;
    from: "active" | "needs_input" | "idle" | "done";
    to: "active" | "needs_input" | "idle" | "done";
}, {
    kind: "status.change";
    sessionId: string;
    from: "active" | "needs_input" | "idle" | "done";
    to: "active" | "needs_input" | "idle" | "done";
}>]>;
export type Event = z.infer<typeof eventSchema>;
export type EventKind = Event['kind'];
/** The transport envelope wrapping every event sent from a daemon to HQ. */
export declare const envelopeSchema: z.ZodObject<{
    v: z.ZodLiteral<1>;
    instanceId: z.ZodString;
    host: z.ZodString;
    ts: z.ZodNumber;
    seq: z.ZodNumber;
    event: z.ZodDiscriminatedUnion<"kind", [z.ZodObject<{
        kind: z.ZodLiteral<"session.start">;
        sessionId: z.ZodString;
        projectId: z.ZodString;
        host: z.ZodString;
        name: z.ZodString;
        agent: z.ZodOptional<z.ZodString>;
        ticket: z.ZodOptional<z.ZodString>;
    }, "strip", z.ZodTypeAny, {
        kind: "session.start";
        sessionId: string;
        projectId: string;
        host: string;
        name: string;
        agent?: string | undefined;
        ticket?: string | undefined;
    }, {
        kind: "session.start";
        sessionId: string;
        projectId: string;
        host: string;
        name: string;
        agent?: string | undefined;
        ticket?: string | undefined;
    }>, z.ZodObject<{
        kind: z.ZodLiteral<"session.rename">;
        sessionId: z.ZodString;
        name: z.ZodString;
    }, "strip", z.ZodTypeAny, {
        kind: "session.rename";
        sessionId: string;
        name: string;
    }, {
        kind: "session.rename";
        sessionId: string;
        name: string;
    }>, z.ZodObject<{
        kind: z.ZodLiteral<"user.msg">;
        sessionId: z.ZodString;
        tokens: z.ZodNumber;
    }, "strip", z.ZodTypeAny, {
        kind: "user.msg";
        sessionId: string;
        tokens: number;
    }, {
        kind: "user.msg";
        sessionId: string;
        tokens: number;
    }>, z.ZodObject<{
        kind: z.ZodLiteral<"assistant.msg">;
        sessionId: z.ZodString;
        tokens: z.ZodNumber;
    }, "strip", z.ZodTypeAny, {
        kind: "assistant.msg";
        sessionId: string;
        tokens: number;
    }, {
        kind: "assistant.msg";
        sessionId: string;
        tokens: number;
    }>, z.ZodObject<{
        kind: z.ZodLiteral<"tool.call">;
        sessionId: z.ZodString;
        tool: z.ZodString;
        argsSummary: z.ZodDefault<z.ZodString>;
    }, "strip", z.ZodTypeAny, {
        kind: "tool.call";
        sessionId: string;
        tool: string;
        argsSummary: string;
    }, {
        kind: "tool.call";
        sessionId: string;
        tool: string;
        argsSummary?: string | undefined;
    }>, z.ZodObject<{
        kind: z.ZodLiteral<"tool.result">;
        sessionId: z.ZodString;
        ok: z.ZodBoolean;
        ms: z.ZodNumber;
        summary: z.ZodDefault<z.ZodString>;
    }, "strip", z.ZodTypeAny, {
        kind: "tool.result";
        sessionId: string;
        ok: boolean;
        ms: number;
        summary: string;
    }, {
        kind: "tool.result";
        sessionId: string;
        ok: boolean;
        ms: number;
        summary?: string | undefined;
    }>, z.ZodObject<{
        kind: z.ZodLiteral<"cost.tick">;
        sessionId: z.ZodString;
        deltaUsd: z.ZodNumber;
        totalUsd: z.ZodNumber;
        tokens: z.ZodNumber;
    }, "strip", z.ZodTypeAny, {
        kind: "cost.tick";
        sessionId: string;
        tokens: number;
        deltaUsd: number;
        totalUsd: number;
    }, {
        kind: "cost.tick";
        sessionId: string;
        tokens: number;
        deltaUsd: number;
        totalUsd: number;
    }>, z.ZodObject<{
        kind: z.ZodLiteral<"status.change">;
        sessionId: z.ZodString;
        from: z.ZodEnum<["active", "needs_input", "idle", "done"]>;
        to: z.ZodEnum<["active", "needs_input", "idle", "done"]>;
    }, "strip", z.ZodTypeAny, {
        kind: "status.change";
        sessionId: string;
        from: "active" | "needs_input" | "idle" | "done";
        to: "active" | "needs_input" | "idle" | "done";
    }, {
        kind: "status.change";
        sessionId: string;
        from: "active" | "needs_input" | "idle" | "done";
        to: "active" | "needs_input" | "idle" | "done";
    }>]>;
}, "strip", z.ZodTypeAny, {
    host: string;
    v: 1;
    instanceId: string;
    ts: number;
    seq: number;
    event: {
        kind: "session.start";
        sessionId: string;
        projectId: string;
        host: string;
        name: string;
        agent?: string | undefined;
        ticket?: string | undefined;
    } | {
        kind: "session.rename";
        sessionId: string;
        name: string;
    } | {
        kind: "user.msg";
        sessionId: string;
        tokens: number;
    } | {
        kind: "assistant.msg";
        sessionId: string;
        tokens: number;
    } | {
        kind: "tool.call";
        sessionId: string;
        tool: string;
        argsSummary: string;
    } | {
        kind: "tool.result";
        sessionId: string;
        ok: boolean;
        ms: number;
        summary: string;
    } | {
        kind: "cost.tick";
        sessionId: string;
        tokens: number;
        deltaUsd: number;
        totalUsd: number;
    } | {
        kind: "status.change";
        sessionId: string;
        from: "active" | "needs_input" | "idle" | "done";
        to: "active" | "needs_input" | "idle" | "done";
    };
}, {
    host: string;
    v: 1;
    instanceId: string;
    ts: number;
    seq: number;
    event: {
        kind: "session.start";
        sessionId: string;
        projectId: string;
        host: string;
        name: string;
        agent?: string | undefined;
        ticket?: string | undefined;
    } | {
        kind: "session.rename";
        sessionId: string;
        name: string;
    } | {
        kind: "user.msg";
        sessionId: string;
        tokens: number;
    } | {
        kind: "assistant.msg";
        sessionId: string;
        tokens: number;
    } | {
        kind: "tool.call";
        sessionId: string;
        tool: string;
        argsSummary?: string | undefined;
    } | {
        kind: "tool.result";
        sessionId: string;
        ok: boolean;
        ms: number;
        summary?: string | undefined;
    } | {
        kind: "cost.tick";
        sessionId: string;
        tokens: number;
        deltaUsd: number;
        totalUsd: number;
    } | {
        kind: "status.change";
        sessionId: string;
        from: "active" | "needs_input" | "idle" | "done";
        to: "active" | "needs_input" | "idle" | "done";
    };
}>;
export type Envelope = z.infer<typeof envelopeSchema>;
/** Parse + validate an unknown value as an Envelope. Throws on invalid input. */
export declare function parseEnvelope(value: unknown): Envelope;
/** Safe variant returning a zod SafeParseReturnType. */
export declare function safeParseEnvelope(value: unknown): z.SafeParseReturnType<{
    host: string;
    v: 1;
    instanceId: string;
    ts: number;
    seq: number;
    event: {
        kind: "session.start";
        sessionId: string;
        projectId: string;
        host: string;
        name: string;
        agent?: string | undefined;
        ticket?: string | undefined;
    } | {
        kind: "session.rename";
        sessionId: string;
        name: string;
    } | {
        kind: "user.msg";
        sessionId: string;
        tokens: number;
    } | {
        kind: "assistant.msg";
        sessionId: string;
        tokens: number;
    } | {
        kind: "tool.call";
        sessionId: string;
        tool: string;
        argsSummary?: string | undefined;
    } | {
        kind: "tool.result";
        sessionId: string;
        ok: boolean;
        ms: number;
        summary?: string | undefined;
    } | {
        kind: "cost.tick";
        sessionId: string;
        tokens: number;
        deltaUsd: number;
        totalUsd: number;
    } | {
        kind: "status.change";
        sessionId: string;
        from: "active" | "needs_input" | "idle" | "done";
        to: "active" | "needs_input" | "idle" | "done";
    };
}, {
    host: string;
    v: 1;
    instanceId: string;
    ts: number;
    seq: number;
    event: {
        kind: "session.start";
        sessionId: string;
        projectId: string;
        host: string;
        name: string;
        agent?: string | undefined;
        ticket?: string | undefined;
    } | {
        kind: "session.rename";
        sessionId: string;
        name: string;
    } | {
        kind: "user.msg";
        sessionId: string;
        tokens: number;
    } | {
        kind: "assistant.msg";
        sessionId: string;
        tokens: number;
    } | {
        kind: "tool.call";
        sessionId: string;
        tool: string;
        argsSummary: string;
    } | {
        kind: "tool.result";
        sessionId: string;
        ok: boolean;
        ms: number;
        summary: string;
    } | {
        kind: "cost.tick";
        sessionId: string;
        tokens: number;
        deltaUsd: number;
        totalUsd: number;
    } | {
        kind: "status.change";
        sessionId: string;
        from: "active" | "needs_input" | "idle" | "done";
        to: "active" | "needs_input" | "idle" | "done";
    };
}>;
