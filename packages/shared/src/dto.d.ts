import { z } from 'zod';
/** Read/write DTOs for the core entities. These shape the REST API surface. */
export declare const projectSchema: z.ZodObject<{
    id: z.ZodString;
    name: z.ZodString;
    repo: z.ZodString;
    ownerUserId: z.ZodString;
    prdGoal: z.ZodOptional<z.ZodString>;
    progressPct: z.ZodOptional<z.ZodNumber>;
    liveSessionCount: z.ZodDefault<z.ZodNumber>;
}, "strip", z.ZodTypeAny, {
    name: string;
    id: string;
    repo: string;
    ownerUserId: string;
    liveSessionCount: number;
    prdGoal?: string | undefined;
    progressPct?: number | undefined;
}, {
    name: string;
    id: string;
    repo: string;
    ownerUserId: string;
    prdGoal?: string | undefined;
    progressPct?: number | undefined;
    liveSessionCount?: number | undefined;
}>;
export type Project = z.infer<typeof projectSchema>;
/** The current-state projection of a session, derived from its event stream. */
export declare const sessionProjectionSchema: z.ZodObject<{
    sessionId: z.ZodString;
    projectId: z.ZodString;
    name: z.ZodString;
    host: z.ZodString;
    agent: z.ZodOptional<z.ZodString>;
    ticket: z.ZodOptional<z.ZodString>;
    status: z.ZodEnum<["active", "needs_input", "idle", "done"]>;
    tokens: z.ZodDefault<z.ZodNumber>;
    costUsd: z.ZodDefault<z.ZodNumber>;
    startedAt: z.ZodNumber;
    lastEventAt: z.ZodNumber;
}, "strip", z.ZodTypeAny, {
    status: "active" | "needs_input" | "idle" | "done";
    sessionId: string;
    projectId: string;
    host: string;
    name: string;
    tokens: number;
    costUsd: number;
    startedAt: number;
    lastEventAt: number;
    agent?: string | undefined;
    ticket?: string | undefined;
}, {
    status: "active" | "needs_input" | "idle" | "done";
    sessionId: string;
    projectId: string;
    host: string;
    name: string;
    startedAt: number;
    lastEventAt: number;
    agent?: string | undefined;
    ticket?: string | undefined;
    tokens?: number | undefined;
    costUsd?: number | undefined;
}>;
export type SessionProjection = z.infer<typeof sessionProjectionSchema>;
export declare const TICKET_STATUSES: readonly ["backlog", "in_progress", "in_review", "done", "icebox"];
export declare const ticketStatusSchema: z.ZodEnum<["backlog", "in_progress", "in_review", "done", "icebox"]>;
export type TicketStatus = z.infer<typeof ticketStatusSchema>;
export declare const PRIORITIES: readonly ["high", "medium", "low"];
export declare const prioritySchema: z.ZodEnum<["high", "medium", "low"]>;
export type Priority = z.infer<typeof prioritySchema>;
export declare const ticketSchema: z.ZodObject<{
    id: z.ZodString;
    projectId: z.ZodString;
    title: z.ZodString;
    description: z.ZodOptional<z.ZodString>;
    status: z.ZodEnum<["backlog", "in_progress", "in_review", "done", "icebox"]>;
    priority: z.ZodEnum<["high", "medium", "low"]>;
    sessionId: z.ZodOptional<z.ZodString>;
    branch: z.ZodOptional<z.ZodString>;
    pr: z.ZodOptional<z.ZodString>;
}, "strip", z.ZodTypeAny, {
    status: "done" | "backlog" | "in_progress" | "in_review" | "icebox";
    projectId: string;
    id: string;
    title: string;
    priority: "high" | "medium" | "low";
    sessionId?: string | undefined;
    description?: string | undefined;
    branch?: string | undefined;
    pr?: string | undefined;
}, {
    status: "done" | "backlog" | "in_progress" | "in_review" | "icebox";
    projectId: string;
    id: string;
    title: string;
    priority: "high" | "medium" | "low";
    sessionId?: string | undefined;
    description?: string | undefined;
    branch?: string | undefined;
    pr?: string | undefined;
}>;
export type Ticket = z.infer<typeof ticketSchema>;
export declare const agentSchema: z.ZodObject<{
    name: z.ZodString;
    scope: z.ZodObject<{
        tier: z.ZodEnum<["org", "user", "project"]>;
        id: z.ZodString;
    }, "strip", z.ZodTypeAny, {
        tier: "org" | "user" | "project";
        id: string;
    }, {
        tier: "org" | "user" | "project";
        id: string;
    }>;
    model: z.ZodString;
    prompt: z.ZodDefault<z.ZodString>;
    skills: z.ZodDefault<z.ZodArray<z.ZodString, "many">>;
    tools: z.ZodDefault<z.ZodArray<z.ZodString, "many">>;
}, "strip", z.ZodTypeAny, {
    name: string;
    scope: {
        tier: "org" | "user" | "project";
        id: string;
    };
    model: string;
    prompt: string;
    skills: string[];
    tools: string[];
}, {
    name: string;
    scope: {
        tier: "org" | "user" | "project";
        id: string;
    };
    model: string;
    prompt?: string | undefined;
    skills?: string[] | undefined;
    tools?: string[] | undefined;
}>;
export type Agent = z.infer<typeof agentSchema>;
export declare const SKILL_KINDS: readonly ["skill", "bundle"];
export declare const skillKindSchema: z.ZodEnum<["skill", "bundle"]>;
export type SkillKind = z.infer<typeof skillKindSchema>;
export declare const skillSchema: z.ZodObject<{
    name: z.ZodString;
    scope: z.ZodObject<{
        tier: z.ZodEnum<["org", "user", "project"]>;
        id: z.ZodString;
    }, "strip", z.ZodTypeAny, {
        tier: "org" | "user" | "project";
        id: string;
    }, {
        tier: "org" | "user" | "project";
        id: string;
    }>;
    kind: z.ZodEnum<["skill", "bundle"]>;
    description: z.ZodDefault<z.ZodString>;
    source: z.ZodDefault<z.ZodEnum<["built-in", "local", "custom"]>>;
    /** For bundles: names of member skills (which may themselves be bundles). */
    members: z.ZodDefault<z.ZodArray<z.ZodString, "many">>;
}, "strip", z.ZodTypeAny, {
    kind: "skill" | "bundle";
    name: string;
    description: string;
    scope: {
        tier: "org" | "user" | "project";
        id: string;
    };
    source: "custom" | "built-in" | "local";
    members: string[];
}, {
    kind: "skill" | "bundle";
    name: string;
    scope: {
        tier: "org" | "user" | "project";
        id: string;
    };
    description?: string | undefined;
    source?: "custom" | "built-in" | "local" | undefined;
    members?: string[] | undefined;
}>;
export type Skill = z.infer<typeof skillSchema>;
export declare const OBJECTIVE_LEVELS: readonly ["rally_cry", "defining_objective", "outcome", "supporting_outcome"];
export declare const objectiveLevelSchema: z.ZodEnum<["rally_cry", "defining_objective", "outcome", "supporting_outcome"]>;
export type ObjectiveLevel = z.infer<typeof objectiveLevelSchema>;
export declare const objectiveNodeSchema: z.ZodObject<{
    id: z.ZodString;
    org: z.ZodString;
    level: z.ZodEnum<["rally_cry", "defining_objective", "outcome", "supporting_outcome"]>;
    title: z.ZodString;
    parentId: z.ZodOptional<z.ZodString>;
    pct: z.ZodOptional<z.ZodNumber>;
}, "strip", z.ZodTypeAny, {
    org: string;
    id: string;
    title: string;
    level: "rally_cry" | "defining_objective" | "outcome" | "supporting_outcome";
    parentId?: string | undefined;
    pct?: number | undefined;
}, {
    org: string;
    id: string;
    title: string;
    level: "rally_cry" | "defining_objective" | "outcome" | "supporting_outcome";
    parentId?: string | undefined;
    pct?: number | undefined;
}>;
export type ObjectiveNode = z.infer<typeof objectiveNodeSchema>;
export declare const weeklyItemSchema: z.ZodObject<{
    text: z.ZodString;
    objectiveId: z.ZodOptional<z.ZodString>;
    completionPct: z.ZodOptional<z.ZodNumber>;
}, "strip", z.ZodTypeAny, {
    text: string;
    objectiveId?: string | undefined;
    completionPct?: number | undefined;
}, {
    text: string;
    objectiveId?: string | undefined;
    completionPct?: number | undefined;
}>;
export type WeeklyItem = z.infer<typeof weeklyItemSchema>;
export declare const weeklyUpdateSchema: z.ZodObject<{
    projectId: z.ZodString;
    isoWeek: z.ZodString;
    done: z.ZodDefault<z.ZodArray<z.ZodObject<{
        text: z.ZodString;
        objectiveId: z.ZodOptional<z.ZodString>;
        completionPct: z.ZodOptional<z.ZodNumber>;
    }, "strip", z.ZodTypeAny, {
        text: string;
        objectiveId?: string | undefined;
        completionPct?: number | undefined;
    }, {
        text: string;
        objectiveId?: string | undefined;
        completionPct?: number | undefined;
    }>, "many">>;
    plan: z.ZodDefault<z.ZodArray<z.ZodObject<{
        text: z.ZodString;
        objectiveId: z.ZodOptional<z.ZodString>;
        completionPct: z.ZodOptional<z.ZodNumber>;
    }, "strip", z.ZodTypeAny, {
        text: string;
        objectiveId?: string | undefined;
        completionPct?: number | undefined;
    }, {
        text: string;
        objectiveId?: string | undefined;
        completionPct?: number | undefined;
    }>, "many">>;
    validated: z.ZodDefault<z.ZodBoolean>;
}, "strip", z.ZodTypeAny, {
    done: {
        text: string;
        objectiveId?: string | undefined;
        completionPct?: number | undefined;
    }[];
    projectId: string;
    isoWeek: string;
    plan: {
        text: string;
        objectiveId?: string | undefined;
        completionPct?: number | undefined;
    }[];
    validated: boolean;
}, {
    projectId: string;
    isoWeek: string;
    done?: {
        text: string;
        objectiveId?: string | undefined;
        completionPct?: number | undefined;
    }[] | undefined;
    plan?: {
        text: string;
        objectiveId?: string | undefined;
        completionPct?: number | undefined;
    }[] | undefined;
    validated?: boolean | undefined;
}>;
export type WeeklyUpdate = z.infer<typeof weeklyUpdateSchema>;
