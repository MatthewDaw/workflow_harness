import { z } from 'zod';
import { sessionStatusSchema } from './events.js';
import { scopeRefSchema } from './scope.js';
/** Read/write DTOs for the core entities. These shape the REST API surface. */
export const projectSchema = z.object({
    id: z.string().min(1),
    name: z.string().min(1),
    repo: z.string().min(1), // e.g. gh/acme/weekly-compass
    ownerUserId: z.string().min(1),
    prdGoal: z.string().optional(),
    progressPct: z.number().min(0).max(100).optional(),
    liveSessionCount: z.number().int().nonnegative().default(0),
});
/** The current-state projection of a session, derived from its event stream. */
export const sessionProjectionSchema = z.object({
    sessionId: z.string().min(1),
    projectId: z.string().min(1),
    name: z.string().min(1),
    host: z.string().min(1),
    agent: z.string().optional(),
    ticket: z.string().optional(),
    status: sessionStatusSchema,
    tokens: z.number().int().nonnegative().default(0),
    costUsd: z.number().nonnegative().default(0),
    startedAt: z.number().int().nonnegative(),
    lastEventAt: z.number().int().nonnegative(),
});
export const TICKET_STATUSES = ['backlog', 'in_progress', 'in_review', 'done', 'icebox'];
export const ticketStatusSchema = z.enum(TICKET_STATUSES);
export const PRIORITIES = ['high', 'medium', 'low'];
export const prioritySchema = z.enum(PRIORITIES);
export const ticketSchema = z.object({
    id: z.string().min(1),
    projectId: z.string().min(1),
    title: z.string().min(1),
    description: z.string().optional(),
    status: ticketStatusSchema,
    priority: prioritySchema,
    sessionId: z.string().optional(),
    branch: z.string().optional(),
    pr: z.string().optional(),
});
export const agentSchema = z.object({
    name: z.string().min(1),
    scope: scopeRefSchema,
    model: z.string().min(1),
    prompt: z.string().default(''),
    skills: z.array(z.string()).default([]),
    tools: z.array(z.string()).default([]),
});
export const SKILL_KINDS = ['skill', 'bundle'];
export const skillKindSchema = z.enum(SKILL_KINDS);
export const skillSchema = z.object({
    name: z.string().min(1),
    scope: scopeRefSchema,
    kind: skillKindSchema,
    description: z.string().default(''),
    source: z.enum(['built-in', 'local', 'custom']).default('local'),
    /** For bundles: names of member skills (which may themselves be bundles). */
    members: z.array(z.string()).default([]),
});
export const OBJECTIVE_LEVELS = [
    'rally_cry',
    'defining_objective',
    'outcome',
    'supporting_outcome',
];
export const objectiveLevelSchema = z.enum(OBJECTIVE_LEVELS);
export const objectiveNodeSchema = z.object({
    id: z.string().min(1),
    org: z.string().min(1),
    level: objectiveLevelSchema,
    title: z.string().min(1),
    parentId: z.string().optional(),
    pct: z.number().min(0).max(100).optional(),
});
export const weeklyItemSchema = z.object({
    text: z.string().min(1),
    objectiveId: z.string().optional(), // the Supporting Outcome / objective it advances
    completionPct: z.number().min(0).max(100).optional(),
});
export const weeklyUpdateSchema = z.object({
    projectId: z.string().min(1),
    isoWeek: z.string().regex(/^\d{4}-W\d{2}$/), // e.g. 2026-W23
    done: z.array(weeklyItemSchema).default([]),
    plan: z.array(weeklyItemSchema).default([]),
    validated: z.boolean().default(false),
});
//# sourceMappingURL=dto.js.map