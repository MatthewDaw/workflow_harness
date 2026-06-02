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
export type Project = z.infer<typeof projectSchema>;

/** The current-state projection of a session, derived from its event stream. */
export const sessionProjectionSchema = z.object({
  sessionId: z.string().min(1),
  projectId: z.string().min(1),
  name: z.string().min(1),
  host: z.string().min(1),
  /** The claude+ instance hosting this session; used to route control frames. */
  instanceId: z.string().optional(),
  /** The owning user; control authorizes that the requester matches this uid. */
  ownerUserId: z.string().optional(),
  agent: z.string().optional(),
  ticket: z.string().optional(),
  status: sessionStatusSchema,
  tokens: z.number().int().nonnegative().default(0),
  costUsd: z.number().nonnegative().default(0),
  startedAt: z.number().int().nonnegative(),
  lastEventAt: z.number().int().nonnegative(),
  /**
   * Highest event `seq` folded into this projection. Out-of-order or duplicate
   * envelopes (seq <= this) do not regress the latest-activity fields.
   */
  maxSeq: z.number().int().nonnegative().default(0),
});
export type SessionProjection = z.infer<typeof sessionProjectionSchema>;

export const TICKET_STATUSES = ['backlog', 'in_progress', 'in_review', 'done', 'icebox'] as const;
export const ticketStatusSchema = z.enum(TICKET_STATUSES);
export type TicketStatus = z.infer<typeof ticketStatusSchema>;

export const PRIORITIES = ['high', 'medium', 'low'] as const;
export const prioritySchema = z.enum(PRIORITIES);
export type Priority = z.infer<typeof prioritySchema>;

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
  /** The Supporting Outcome (objective node) this ticket advances; feeds roll-up. */
  objectiveId: z.string().optional(),
});
export type Ticket = z.infer<typeof ticketSchema>;

/**
 * Allowed ticket status transitions. The board advances a ticket forward through
 * backlog -> in_progress -> in_review -> done; any status may be moved to/from
 * `icebox` (deferred). Anything else is rejected as an invalid transition.
 */
export const TICKET_TRANSITIONS: Record<TicketStatus, readonly TicketStatus[]> = {
  backlog: ['in_progress', 'icebox'],
  in_progress: ['in_review', 'backlog', 'icebox'],
  in_review: ['done', 'in_progress', 'icebox'],
  done: ['icebox'],
  icebox: ['backlog'],
};

export function isValidTicketTransition(from: TicketStatus, to: TicketStatus): boolean {
  if (from === to) return true;
  return TICKET_TRANSITIONS[from].includes(to);
}

export const agentSchema = z.object({
  name: z.string().min(1),
  scope: scopeRefSchema,
  model: z.string().min(1),
  prompt: z.string().default(''),
  skills: z.array(z.string()).default([]),
  tools: z.array(z.string()).default([]),
});
export type Agent = z.infer<typeof agentSchema>;

/**
 * Request to elevate/demote an agent (or skill) to a new scope. The handler
 * rewrites the scope key (delete old, put new), so the item moves between the
 * org/user/project tiers. `elevate` widens (project -> user -> org); `demote`
 * narrows.
 */
export const scopeChangeSchema = z.object({
  scope: scopeRefSchema,
});
export type ScopeChange = z.infer<typeof scopeChangeSchema>;

export const SKILL_KINDS = ['skill', 'bundle'] as const;
export const skillKindSchema = z.enum(SKILL_KINDS);
export type SkillKind = z.infer<typeof skillKindSchema>;

export const skillSchema = z.object({
  name: z.string().min(1),
  scope: scopeRefSchema,
  kind: skillKindSchema,
  description: z.string().default(''),
  source: z.enum(['built-in', 'local', 'custom']).default('local'),
  /** For bundles: names of member skills (which may themselves be bundles). */
  members: z.array(z.string()).default([]),
});
export type Skill = z.infer<typeof skillSchema>;

export const OBJECTIVE_LEVELS = [
  'rally_cry',
  'defining_objective',
  'outcome',
  'supporting_outcome',
] as const;
export const objectiveLevelSchema = z.enum(OBJECTIVE_LEVELS);
export type ObjectiveLevel = z.infer<typeof objectiveLevelSchema>;

export const objectiveNodeSchema = z.object({
  id: z.string().min(1),
  org: z.string().min(1),
  level: objectiveLevelSchema,
  title: z.string().min(1),
  parentId: z.string().optional(),
  pct: z.number().min(0).max(100).optional(),
});
export type ObjectiveNode = z.infer<typeof objectiveNodeSchema>;

export const weeklyItemSchema = z.object({
  text: z.string().min(1),
  objectiveId: z.string().optional(), // the Supporting Outcome / objective it advances
  completionPct: z.number().min(0).max(100).optional(),
});
export type WeeklyItem = z.infer<typeof weeklyItemSchema>;

export const weeklyUpdateSchema = z.object({
  projectId: z.string().min(1),
  isoWeek: z.string().regex(/^\d{4}-W\d{2}$/), // e.g. 2026-W23
  done: z.array(weeklyItemSchema).default([]),
  plan: z.array(weeklyItemSchema).default([]),
  validated: z.boolean().default(false),
});
export type WeeklyUpdate = z.infer<typeof weeklyUpdateSchema>;

/**
 * A pending device-authorization record for the wrapper's device-code login.
 * Created on `start`, transitioned to `approved` (with the issuing user/org)
 * when the human approves in HQ, and consumed exactly once on the next `poll`.
 */
export const DEVICE_AUTH_STATUSES = ['pending', 'approved', 'consumed'] as const;
export const deviceAuthStatusSchema = z.enum(DEVICE_AUTH_STATUSES);
export type DeviceAuthStatus = z.infer<typeof deviceAuthStatusSchema>;

export const deviceAuthSchema = z.object({
  deviceCode: z.string().min(1),
  userCode: z.string().min(1),
  status: deviceAuthStatusSchema,
  createdAt: z.number().int().nonnegative(),
  expiresAt: z.number().int().nonnegative(),
  /** Populated once approved: the identity the minted token is scoped to. */
  userId: z.string().min(1).optional(),
  org: z.string().min(1).optional(),
});
export type DeviceAuth = z.infer<typeof deviceAuthSchema>;

/**
 * A control frame sent from HQ web down to a live session via the control
 * gateway (U7) and applied by the wrapper's control receiver (U15). The backend
 * authorizes that the requesting user owns `sessionId`, then routes the frame to
 * the owning daemon's WebSocket connection. `inject` writes `payload.text` to
 * the session's PTY stdin; `pause`/`interrupt` map to signals on the daemon side.
 */
export const CONTROL_ACTIONS = ['inject', 'pause', 'interrupt'] as const;
export const controlActionSchema = z.enum(CONTROL_ACTIONS);
export type ControlAction = z.infer<typeof controlActionSchema>;

export const controlFrameSchema = z.object({
  sessionId: z.string().min(1),
  action: controlActionSchema,
  /** Action payload; `inject` carries `{ text }`, the others may be empty. */
  payload: z.object({ text: z.string() }).partial().default({}),
});
export type ControlFrame = z.infer<typeof controlFrameSchema>;
