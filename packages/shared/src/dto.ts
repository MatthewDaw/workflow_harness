import { z } from 'zod';
import { learningStreamSchema, sessionStatusSchema } from './events.js';
import { scopeRefSchema } from './scope.js';

/** Read/write DTOs for the core entities. These shape the REST API surface. */

export const projectSchema = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  repo: z.string().min(1), // e.g. gh/acme/weekly-compass
  ownerUserId: z.string().min(1),
  /**
   * The org this project belongs to, stamped from the creator's EFFECTIVE org at
   * create time. Optional for back-compat with projects created before org-driven
   * membership existed; listing is still by owner, but the stamp lets future
   * partitioning/filtering scope projects by org.
   */
  org: z.string().optional(),
  prdGoal: z.string().optional(),
  progressPct: z.number().min(0).max(100).optional(),
  /** Supporting Outcomes this project owns, parsed from PRD/framing (U7). */
  supportingOutcomeIds: z.array(z.string()).optional(),
  liveSessionCount: z.number().int().nonnegative().default(0),
  /** ISO timestamp of the last successful GitHub framing read (U7). */
  framingReadAt: z.string().optional(),
  /** True when the last refresh could not reach GitHub; served data is stale (U7). */
  framingStale: z.boolean().optional(),
  /**
   * Skills this project has opted into (skill names from the org catalog).
   * A connected repo materializes exactly these (plus the skills brought by
   * enabledAgents via union-on-add) into ~/.claude+. Defaults to [].
   */
  enabledSkills: z.array(z.string()).default([]),
  /**
   * Bundles this project added "as a whole" (bundle names from the org catalog).
   * This is an INTENT annotation, not a second materialization set: a bundle's
   * member skills are always unioned into `enabledSkills` (the flat set the
   * daemon materializes), so the daemon ignores this field entirely. It exists so
   * the UI can tell "the user added the whole bundle" apart from "the user picked
   * some of its members individually", and so removing a bundle can strip the
   * members it contributed. Defaults to [] for legacy/back-compat records.
   */
  enabledBundles: z.array(z.string()).default([]),
  /**
   * Agents this project has opted into (agent names from the org catalog).
   * Adding an agent unions its declared `skills` into `enabledSkills`.
   * Defaults to [].
   */
  enabledAgents: z.array(z.string()).default([]),
  /**
   * MCP servers this project has opted into (server names from the org catalog).
   * A connected repo materializes exactly these (plus the servers brought by
   * enabledAgents via union-on-add) into the daemon's ~/.claude+/.mcp.json.
   * Defaults to [], which keeps legacy project records (written before MCP
   * servers existed) valid.
   */
  enabledMcpServers: z.array(z.string()).default([]),
});
export type Project = z.infer<typeof projectSchema>;

/**
 * Authorship stamp for catalog items (skills + agents). Set from the
 * authenticated principal on create; powers the "filter by author" facet in
 * the web pickers. Optional on read for back-compat with records created
 * before this field existed.
 */
export const createdBySchema = z.object({
  userId: z.string().min(1),
  name: z.string().min(1),
});
export type CreatedBy = z.infer<typeof createdBySchema>;

/**
 * VERSIONING MODEL (KTD6). A catalog item (skill / agent / mcpServer) is
 * versioned per VARIANT, where a variant is identified by `(baseName, repoId,
 * userId)`: editing item S from project R by person P forks/updates the variant
 * `(S, R, P)`; the org-seeded item is the BASE variant (empty repoId + userId).
 * Every edit SNAPSHOTS an immutable revision (monotonic `version`/`rev` per
 * variant). One ORG-WIDE "true" variant per `baseName` is the default the UI
 * shows and a project adds.
 *
 * These fields are MIXED INTO the existing skill/agent/mcpServer schemas (below)
 * and are all OPTIONAL / DEFAULTED, so a legacy record (no version fields)
 * still validates — it is treated as the base variant of its name at rev 1.
 */
export const versionFieldsSchema = z.object({
  /**
   * The stable identity of this variant's family: the org-catalog name the
   * variant forks from. Defaults to the record's own `name` for legacy records
   * (where `name === baseName`).
   */
  baseName: z.string().optional(),
  /**
   * Opaque, deterministic id of the variant `(baseName, repoId, userId)`.
   * The base variant uses `baseName` itself; forks append `#R#<repoId>#U#<userId>`.
   * Optional on read so legacy records validate; minted on write.
   */
  variantId: z.string().optional(),
  /** The project/repo this variant was forked from. Empty/absent = the base variant. */
  repoId: z.string().optional(),
  /** The user who authored this variant. Empty/absent = the base variant. */
  authorUserId: z.string().optional(),
  /**
   * Monotonic revision number for this variant. Starts at 1 (minted on write by
   * `putNewVersion`). OPTIONAL on read — a legacy record has no `version`, and
   * consumers treat an absent `version` as rev 1 — so parsing a record never
   * fabricates a `version` field that the record did not actually carry.
   */
  version: z.number().int().positive().optional(),
  /** Epoch-ms this revision was snapshotted. Optional for legacy records. */
  createdAt: z.number().int().nonnegative().optional(),
});
export type VersionFields = z.infer<typeof versionFieldsSchema>;

/**
 * The per-name ORG-WIDE "true" pointer payload: which variant+revision is the
 * default shown in the UI and added to a project. Any authed org member may
 * repoint it via promote; promotion never edits or deletes a variant.
 */
export const truePointerSchema = z.object({
  baseName: z.string().min(1),
  variantId: z.string().min(1),
  /** The promoted revision; absent means "the variant's latest". */
  rev: z.number().int().positive().optional(),
});
export type TruePointer = z.infer<typeof truePointerSchema>;

/**
 * A project enabled-set ENTRY carrying the chosen variant (U-Ver-Pin). A repo's
 * enabled skill/agent/mcp may pin a specific variant (default = the current org
 * TRUE variant). To stay BACK-COMPAT with the bare-string entries every existing
 * project record stores, the entry is a UNION: either a plain `string` (just the
 * name; resolves to the TRUE variant at sync) OR `{ name, variantId? }`.
 * `normalizeEnabledEntry` collapses both forms to the object shape.
 */
export const enabledEntrySchema = z.union([
  z.string().min(1),
  z.object({ name: z.string().min(1), variantId: z.string().optional() }),
]);
export type EnabledEntry = z.infer<typeof enabledEntrySchema>;

/** The object form of an enabled-set entry. */
export interface NormalizedEnabledEntry {
  name: string;
  variantId?: string;
}

/** Collapse a bare-string OR `{name, variantId}` enabled-set entry to the object form. */
export function normalizeEnabledEntry(entry: EnabledEntry): NormalizedEnabledEntry {
  return typeof entry === 'string' ? { name: entry } : { name: entry.name, variantId: entry.variantId };
}

/** The bare name of an enabled-set entry, regardless of form. */
export function enabledEntryName(entry: EnabledEntry): string {
  return typeof entry === 'string' ? entry : entry.name;
}

/** The pinned variantId of an enabled-set entry, or undefined for a bare-string entry. */
export function enabledEntryVariant(entry: EnabledEntry): string | undefined {
  return typeof entry === 'string' ? undefined : entry.variantId;
}

/**
 * Deterministically mint the variantId for a variant family member.
 * The BASE variant (no repo + no user) is just `baseName`; a fork appends the
 * repo + author so `(baseName, repoId, authorUserId)` maps 1:1 to an id. This is
 * the same string used as the revision-row infix in keys.ts.
 */
export function variantIdFor(baseName: string, repoId?: string, authorUserId?: string): string {
  if (!repoId && !authorUserId) return baseName;
  return `${baseName}#R#${repoId ?? ''}#U#${authorUserId ?? ''}`;
}

/**
 * A user's PROFILE record (`USER#<userId> / PROFILE`). This is the SOURCE OF
 * TRUTH for org membership: a user with `org` unset genuinely has NO org and is
 * forced through onboarding (create/join). Membership used to come from the
 * Cognito `custom:org` token claim (auto-assigned, so everyone always "had" an
 * org); moving it to the DB lets a user truly be org-less. `admin` is the
 * org-admin flag (the org creator is the first admin).
 */
export const userProfileSchema = z.object({
  userId: z.string().min(1),
  /** The ACTIVE org — the one the app is currently scoped to. */
  org: z.string().optional(),
  name: z.string().optional(),
  /** `true` when the user is an admin of the ACTIVE org (derived from adminOrgs). */
  admin: z.boolean().optional(),
  /** Every org the user has joined or created — the set they can switch between. */
  orgs: z.array(z.string()).optional(),
  /** The subset of `orgs` the user is an admin of (an org's creator). */
  adminOrgs: z.array(z.string()).optional(),
});
export type UserProfile = z.infer<typeof userProfileSchema>;

/**
 * The PUBLIC org record returned to clients. The stored ORG item also carries a
 * password salt + hash (see backend `OrgRecord`); those are NEVER serialized to
 * a client, so this shape deliberately omits them.
 */
export const orgSchema = z.object({
  name: z.string().min(1),
  createdBy: z.string().min(1),
  createdAt: z.number().int().nonnegative(),
});
export type Org = z.infer<typeof orgSchema>;

/**
 * The `GET /me` response. Carries the authenticated identity plus the effective
 * org membership. `org` is nullable (not optional) so the web's OrgGate can
 * distinguish "no org yet -> onboard" from a present org explicitly.
 */
export const meResponseSchema = z.object({
  userId: z.string().min(1),
  name: z.string().optional(),
  org: z.string().nullable(),
  admin: z.boolean().optional(),
  /** Every org the user belongs to, so the header can offer a switcher. */
  orgs: z.array(z.string()).default([]),
});
export type MeResponse = z.infer<typeof meResponseSchema>;

/**
 * Request validation for org create/join. An org name must be typed EXACTLY to
 * join, so it is trimmed (no leading/trailing whitespace surprises) and capped.
 * The password is deliberately UNRESTRICTED — no length or complexity floor —
 * so users can pick whatever shared secret they like; it only has to be present
 * (the field is required) so create/join always have something to hash/compare.
 */
export const orgNameSchema = z.string().trim().min(1).max(64);
export const orgPasswordSchema = z.string();

export const createOrgRequestSchema = z.object({
  name: orgNameSchema,
  password: orgPasswordSchema,
});
export type CreateOrgRequest = z.infer<typeof createOrgRequestSchema>;

export const joinOrgRequestSchema = z.object({
  name: orgNameSchema,
  password: orgPasswordSchema,
});
export type JoinOrgRequest = z.infer<typeof joinOrgRequestSchema>;

/**
 * Switch the ACTIVE org to one the user has ALREADY joined. No password — this
 * is not a join, just flipping which membership the app is scoped to; the server
 * still verifies the target is in the caller's `orgs` set.
 */
export const switchOrgRequestSchema = z.object({
  org: orgNameSchema,
});
export type SwitchOrgRequest = z.infer<typeof switchOrgRequestSchema>;

/** The current-state projection of a session, derived from its event stream. */
export const sessionProjectionSchema = z.object({
  sessionId: z.string().min(1),
  projectId: z.string().min(1),
  name: z.string().min(1),
  /** The first prompt the user typed (from the UserPromptSubmit hook); shown in the Sessions list. */
  summary: z.string().optional(),
  /** The session's current topic, relabeled as focus drifts (from `session.topic`); distinct from the stable `name`. */
  topic: z.string().optional(),
  /** A rich, self-contained summary of the current topic, rolled forward as it evolves (from `session.topic`). */
  description: z.string().optional(),
  /** Epoch ms of the last topic/description fold; lets clients show how fresh the rolling summary is. */
  summaryUpdatedAt: z.number().int().nonnegative().optional(),
  /** Where the displayed title came from: the stable slug, an AI rename, a manual user rename, or the evolving topic. */
  titleSource: z.enum(['slug', 'ai', 'user', 'topic']).optional(),
  host: z.string().min(1),
  /** The claude+ instance hosting this session; used to route control frames. */
  instanceId: z.string().optional(),
  /** The owning user; control authorizes that the requester matches this uid. */
  ownerUserId: z.string().optional(),
  agent: z.string().optional(),
  status: sessionStatusSchema,
  tokens: z.number().int().nonnegative().default(0),
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
  /**
   * Catalog scope. `org` is the default tier (the org-wide catalog), but
   * user-scoped agents are also representable so the catalog partitions by org
   * AND by user. `scopeRefSchema` is a strict superset of the old org-only
   * shape, so every existing org-scoped record still validates.
   */
  scope: scopeRefSchema,
  model: z.string().min(1),
  prompt: z.string().default(''),
  /**
   * Delegation trigger — the human-readable cue the orchestrator uses to decide
   * WHEN to spawn this agent. Renders to the materialized subagent file's
   * `description:` frontmatter. Defaults to '' for back-compat with agent
   * records (and tests) written before this field existed.
   */
  description: z.string().default(''),
  skills: z.array(z.string()).default([]),
  tools: z.array(z.string()).default([]),
  /**
   * MCP servers this agent declares (server names from the org catalog). Adding
   * the agent to a project unions these (plain names — no bundles) into the
   * project's `enabledMcpServers`. Defaults to [] for back-compat with agent
   * records written before MCP servers existed.
   */
  mcpServers: z.array(z.string()).default([]),
  /** Authorship stamp set on create; optional on read for back-compat. */
  createdBy: createdBySchema.optional(),
}).merge(versionFieldsSchema);
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

/**
 * The transports an MCP server can speak. `stdio` launches a local subprocess
 * (the daemon spawns `command args` with `env`); `http`/`sse` are remote
 * transports addressed by `url` with static `headers`. This is the
 * discriminator for `mcpServerSchema`.
 */
export const MCP_TRANSPORTS = ['stdio', 'http', 'sse'] as const;
export const mcpTransportSchema = z.enum(MCP_TRANSPORTS);
export type McpTransport = z.infer<typeof mcpTransportSchema>;

/**
 * An MCP server in the org catalog — the third pillar of a Claude setup
 * alongside skills and agents. Unlike a skill (a freeform `body`), this is a
 * STRUCTURED record: a `transport` discriminator with per-transport fields. The
 * wrapper reconstructs it into a `~/.claude+/.mcp.json` entry; there is no
 * separate body. Org-only and flat — there is no bundle concept.
 *
 * SECURITY: `env`/`headers` values (API keys, tokens) are stored in DynamoDB as
 * PLAINTEXT and served to any authed org member — the same trust model skills'
 * bodies already use. This is a deliberate v1 simplification; KMS / env-ref
 * resolution is a documented follow-up. Never log these values.
 */
export const mcpServerSchema = z.discriminatedUnion('transport', [
  z
    .object({
      name: z.string().min(1),
      /** Catalog scope. Org-only today, but kept as a full `scopeRefSchema` for
       * parity with skills/agents (every existing org-scoped record validates). */
      scope: scopeRefSchema,
      transport: z.literal('stdio'),
      /** The executable the daemon spawns for a local (stdio) server. */
      command: z.string().min(1),
      /** Arguments passed to `command`. Defaults to []. */
      args: z.array(z.string()).default([]),
      /** Environment variables for the subprocess (plaintext secrets — see note). */
      env: z.record(z.string()).default({}),
      /** Authorship stamp set on create; optional on read for back-compat. */
      createdBy: createdBySchema.optional(),
    })
    .merge(versionFieldsSchema),
  z
    .object({
      name: z.string().min(1),
      scope: scopeRefSchema,
      transport: z.literal('http'),
      /** The remote endpoint URL the daemon connects to. */
      url: z.string().url(),
      /** Static request headers (plaintext secrets — see note). Defaults to {}. */
      headers: z.record(z.string()).default({}),
      createdBy: createdBySchema.optional(),
    })
    .merge(versionFieldsSchema),
  z
    .object({
      name: z.string().min(1),
      scope: scopeRefSchema,
      transport: z.literal('sse'),
      url: z.string().url(),
      headers: z.record(z.string()).default({}),
      createdBy: createdBySchema.optional(),
    })
    .merge(versionFieldsSchema),
]);
export type McpServer = z.infer<typeof mcpServerSchema>;

export const SKILL_KINDS = ['skill', 'bundle'] as const;
export const skillKindSchema = z.enum(SKILL_KINDS);
export type SkillKind = z.infer<typeof skillKindSchema>;

export const skillSchema = z.object({
  name: z.string().min(1),
  /**
   * Catalog scope. `org` is the default tier (the org-wide catalog), but
   * user-scoped skills are also representable so the catalog partitions by org
   * AND by user. `scopeRefSchema` is a strict superset of the old org-only
   * shape, so every existing org-scoped record still validates.
   */
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
  /**
   * WHOLE-DIRECTORY skill storage (U-Skill-Dirs). A skill is a directory:
   * `SKILL.md` PLUS sibling scripts/resources. This maps each file's relative
   * path WITHIN the skill dir (e.g. `SKILL.md`, `scripts/run.sh`) to its
   * contents, so the daemon can materialize the entire `.claude/skills/<name>/`
   * tree — not just the body. Optional for back-compat: legacy body-only records
   * (no `files`) still materialize just `SKILL.md` from `body`.
   */
  files: z.record(z.string()).optional(),
  /**
   * Read-only annotation populated by the resolve endpoint for bundles: the
   * transitively-flattened leaf-skill member names (nested bundles expanded).
   * Never written by clients; present only on GET /skills responses.
   */
  resolvedMembers: z.array(z.string()).optional(),
  /** Who created the catalog record (the seed stamps `system`; REST stamps the
   * authenticated principal). Optional for legacy records written before it. */
  createdBy: createdBySchema.optional(),
}).merge(versionFieldsSchema);
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
 * The org-wide **Definition of Done** (plan-mapping feature 1). It declares what
 * `/update-progress` must verify before work is considered complete. It is
 * ADVISORY — surfaced in HQ and reported on by the compliance report, but it
 * NEVER hard-blocks a progress push ("conformity never blocks").
 *
 *  - `requiresUnitTests` is the org-wide floor / default (true): unit tests pass.
 *  - `requiresProdE2E` (default false) tightens it: prod E2E suite verified green.
 *  - `notes` is free-form guidance for the team (e.g. how to find the E2E suite).
 *
 * Storage is a single org-scoped record (one DoD per org), so it is not keyed by
 * project — every repo in the org ladders up to the same floor.
 */
export const definitionOfDoneSchema = z.object({
  requiresUnitTests: z.boolean().default(true),
  requiresProdE2E: z.boolean().default(false),
  notes: z.string().optional(),
});
export type DefinitionOfDone = z.infer<typeof definitionOfDoneSchema>;

/** The org-wide default DoD when none has been configured (the floor). */
export const DEFAULT_DEFINITION_OF_DONE: DefinitionOfDone = {
  requiresUnitTests: true,
  requiresProdE2E: false,
};

/**
 * Org-level configuration block. Today it carries only the optional Definition
 * of Done; it is the natural home for future org-wide settings. Stored as a
 * single org-scoped record (see backend `getOrgDod`/`putOrgDod`).
 */
export const orgConfigSchema = z.object({
  dod: definitionOfDoneSchema.optional(),
});
export type OrgConfig = z.infer<typeof orgConfigSchema>;

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
  /** The approver's display name, carried into the minted token for the wrapper's
   * status line (username @ org). Optional for back-compat with older records. */
  name: z.string().min(1).optional(),
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
 * A summarized + embedded session, the unit Forge searches over (U27). The
 * session's transcript summary + embedding and the skills/tools it used are
 * recorded so similar sessions can be aggregated into an agent proposal. This
 * is a stored contract shape; the vector/fuzzy-Forge read path is deferred and
 * not wired to any deployed handler.
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

/**
 * A persisted learning mined from a correction turn (topic-focus logging). The
 * `session.learning` event is appended as one of these records under the owning
 * PROJECT partition, so a project's whole corpus is one partition read. It is
 * NOT folded into the session projection. `(sessionId, turnId)` is the
 * idempotency key (the storage SK) — a re-emitted learning overwrites in place.
 * `docRef` is set only on the `doc` stream; `ts`/`seq` carry the envelope's
 * transport stamps for ordering/observability.
 */
export const learningRecordSchema = z.object({
  projectId: z.string().min(1),
  sessionId: z.string().min(1),
  segmentId: z.string().min(1),
  topicLabel: z.string().min(1),
  stream: learningStreamSchema,
  text: z.string().min(1),
  /** Present only on the `doc` stream: the nearest feature doc contradicted. */
  docRef: z.string().optional(),
  turnId: z.string().min(1),
  /** Envelope epoch-ms timestamp the learning was emitted. */
  ts: z.number().int().nonnegative(),
  /** Envelope per-session monotonic seq, for ordering/observability. */
  seq: z.number().int().nonnegative(),
});
export type LearningRecord = z.infer<typeof learningRecordSchema>;

/**
 * A project memory synced up from a developer's machine. Claude Code persists
 * per-project "memories" as small markdown files (one fact per file, with
 * `name` / `description` / `metadata.type` frontmatter) under the config root's
 * project dir; the claude+ daemon reconciles that directory up to HQ as Claude
 * saves them. Each memory is stamped with the AUTHOR who generated it so the
 * Project Details "Memories" tab can group/filter by user — they live under the
 * owning PROJECT partition, sub-keyed by `userId` (see keys.ts `memoryKey`).
 *
 *  - `name` is the file's kebab-case slug, unique per (project, user).
 *  - `content` is the full raw markdown of the file (the authoritative body).
 *  - `description` / `type` are parsed out of the frontmatter for display.
 */
export const memoryTypeSchema = z.enum(['user', 'feedback', 'project', 'reference']);
export type MemoryType = z.infer<typeof memoryTypeSchema>;

export const memorySchema = z.object({
  projectId: z.string().min(1),
  userId: z.string().min(1),
  /** The author's display name, for the tab's per-user grouping. */
  userName: z.string().optional(),
  name: z.string().min(1),
  description: z.string().optional(),
  type: memoryTypeSchema.optional(),
  content: z.string(),
  /** Epoch-ms of the last sync that wrote this memory. */
  updatedAt: z.number().int().nonnegative(),
});
export type Memory = z.infer<typeof memorySchema>;

/**
 * One memory as the daemon sends it: just the on-disk fields. The server stamps
 * `projectId` (from the path), `userId` / `userName` (from the principal), and
 * `updatedAt`, so they are intentionally absent here.
 */
export const memoryInputSchema = memorySchema.pick({
  name: true,
  description: true,
  type: true,
  content: true,
});
export type MemoryInput = z.infer<typeof memoryInputSchema>;

/**
 * The reconcile payload (PUT /projects/:id/memories). It carries the caller's
 * WHOLE current memory set for the project; the server replaces the caller's
 * stored set with it, so memories deleted on disk are removed from HQ too. An
 * empty array is valid and clears the caller's set.
 */
export const reconcileMemoriesRequestSchema = z.object({
  memories: z.array(memoryInputSchema),
});
export type ReconcileMemoriesRequest = z.infer<typeof reconcileMemoriesRequestSchema>;

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
 * `shutdown` terminates the session gracefully (SIGTERM, escalating to a force
 * kill if it does not exit in time); `kill` is an immediate force terminate.
 */
export const CONTROL_ACTIONS = ['inject', 'pause', 'interrupt', 'shutdown', 'kill'] as const;
export const controlActionSchema = z.enum(CONTROL_ACTIONS);
export type ControlAction = z.infer<typeof controlActionSchema>;

export const controlFrameSchema = z.object({
  sessionId: z.string().min(1),
  action: controlActionSchema,
  /** Action payload; `inject` carries `{ text }`, the others may be empty. */
  payload: z.object({ text: z.string() }).partial().default({}),
});
export type ControlFrame = z.infer<typeof controlFrameSchema>;
