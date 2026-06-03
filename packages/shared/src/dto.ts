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
  /** Supporting Outcomes this project owns, parsed from PRD/framing (U7). */
  supportingOutcomeIds: z.array(z.string()).optional(),
  liveSessionCount: z.number().int().nonnegative().default(0),
  /** ISO timestamp of the last successful GitHub framing read (U7). */
  framingReadAt: z.string().optional(),
  /** True when the last refresh could not reach GitHub; served data is stale (U7). */
  framingStale: z.boolean().optional(),
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

export const PRIORITIES = ['high', 'medium', 'low'] as const;
export const prioritySchema = z.enum(PRIORITIES);
export type Priority = z.infer<typeof prioritySchema>;

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
  /**
   * Full SKILL.md content. HQ stores the body so a daemon can materialize the
   * skill locally on session-start sync (not just show metadata). Empty for
   * bundles / metadata-only records.
   */
  body: z.string().default(''),
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

/**
 * A weekly update is a client-generated report posted to HQ (KTD4). The
 * `/weekly-update` skill, running in claude+, reads the week's git diff,
 * interviews the user on next-week goals, computes a never-blocking conformity
 * score, and POSTs this shape. HQ stores and serves it; it never generates it.
 *
 *  - `done` is a free-form summary of what shipped this week (prose, not items).
 *  - `plan` is a free-form summary of next week's intended work.
 *  - `conformityScore` is a manager-visible 0..100 measure of how well the plan
 *    ladders up to the fixed high-level goals. It is surfaced, never a gate.
 */
export const weeklyUpdateSchema = z.object({
  projectId: z.string().min(1),
  isoWeek: z.string().regex(/^\d{4}-W\d{2}$/), // e.g. 2026-W23
  done: z.string().default(''),
  plan: z.string().default(''),
  conformityScore: z.number().min(0).max(100).optional(),
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
 * A single git commit as read from the GitHub integration (U26). Used by the
 * Weekly "done" assembly (U28): commits are attributed to the objectives their
 * branch/PR/message advances.
 */
export const gitCommitSchema = z.object({
  sha: z.string().min(1),
  message: z.string(),
  author: z.string().default(''),
  /** ISO-8601 timestamp the commit was authored. */
  committedAt: z.string().min(1),
});
export type GitCommit = z.infer<typeof gitCommitSchema>;

/**
 * The project framing read from a repo's `PRD.md` / `PROGRESS.md` on connect or
 * sync (U26). `goal` is the PRD's stated goal; `supportingOutcomeIds` are the
 * Supporting Outcomes the project owns; `progressPct` is parsed from PROGRESS.md.
 */
export const projectFramingSchema = z.object({
  goal: z.string().optional(),
  supportingOutcomeIds: z.array(z.string()).default([]),
  progressPct: z.number().min(0).max(100).optional(),
  /** True when a required file (PRD.md / PROGRESS.md) was absent. */
  missingFiles: z.array(z.string()).default([]),
});
export type ProjectFraming = z.infer<typeof projectFramingSchema>;

/**
 * A summarized + embedded session, the unit Forge searches over (U27). Created
 * on `session.done`: the session's transcript is summarized and embedded
 * (Bedrock), and the skills/tools it used are recorded so similar sessions can
 * be aggregated into an agent proposal. Vectors are stored in DynamoDB for the
 * brute-force cosine fallback (KTD7); OpenSearch indexing is additive.
 */
export const sessionVectorSchema = z.object({
  sessionId: z.string().min(1),
  userId: z.string().min(1),
  projectId: z.string().min(1),
  summary: z.string().default(''),
  /** The embedding vector for the summary. */
  vector: z.array(z.number()),
  /** Skills used during the session (for frequency aggregation). */
  skills: z.array(z.string()).default([]),
  /** Tools used during the session (for frequency aggregation). */
  tools: z.array(z.string()).default([]),
  createdAt: z.number().int().nonnegative(),
});
export type SessionVector = z.infer<typeof sessionVectorSchema>;

/** A session ranked by similarity to a Forge query (U27). */
export const scoredSessionSchema = z.object({
  sessionId: z.string().min(1),
  score: z.number(),
  summary: z.string().default(''),
  skills: z.array(z.string()).default([]),
  tools: z.array(z.string()).default([]),
});
export type ScoredSession = z.infer<typeof scoredSessionSchema>;

/**
 * A drafted agent proposed by Forge from similar sessions (U27). `evidence`
 * lists the sessions that informed it; `lowConfidence` flags skills/tools that
 * appeared in too few sessions to be certain, so the editor can highlight them.
 */
export const agentProposalSchema = z.object({
  name: z.string().min(1),
  model: z.string().min(1),
  prompt: z.string().default(''),
  skills: z.array(z.string()).default([]),
  tools: z.array(z.string()).default([]),
  lowConfidence: z.array(z.string()).default([]),
  evidence: z.array(scoredSessionSchema).default([]),
  /** True when there was not enough history to mine; the draft is a blank slate. */
  insufficientHistory: z.boolean().default(false),
});
export type AgentProposal = z.infer<typeof agentProposalSchema>;

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
