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
   * Workflows this project has opted into (workflow names from the org catalog).
   * Adding a workflow unions every agent its nodes reference into
   * `enabledAgents` — and transitively their skills/MCP servers into
   * `enabledSkills`/`enabledMcpServers`. Defaults to [].
   */
  enabledWorkflows: z.array(z.string()).default([]),
  /**
   * Agent bundles this project added "as a whole" (bundle names from the org
   * catalog). Like `enabledBundles` for skills, this is an INTENT annotation,
   * not a second materialization set: a bundle's member agents are always
   * unioned into `enabledAgents` (and their skills/MCP servers into
   * `enabledSkills`/`enabledMcpServers`), so the daemon ignores this field for
   * materialization. It exists so the UI can tell "the user added the whole
   * bundle" apart from "the user picked some members individually", and so
   * removing a bundle can strip the members it contributed. Defaults to [] for
   * legacy/back-compat records.
   */
  enabledAgentBundles: z.array(z.string()).default([]),
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
  return typeof entry === 'string'
    ? { name: entry }
    : { name: entry.name, variantId: entry.variantId };
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

/**
 * An agent record is either a runnable `agent` or a `bundle` — a grouping of
 * other agents (mirrors `SKILL_KINDS`). A bundle has `members` (member agent
 * names, possibly nested bundles) and no model/prompt of its own; it is never
 * materialized as a subagent file, only expanded into its members.
 */
export const AGENT_KINDS = ['agent', 'bundle'] as const;
export const agentKindSchema = z.enum(AGENT_KINDS);
export type AgentKind = z.infer<typeof agentKindSchema>;

export const agentSchema = z
  .object({
    name: z.string().min(1),
    /**
     * Catalog scope. `org` is the default tier (the org-wide catalog), but
     * user-scoped agents are also representable so the catalog partitions by org
     * AND by user. `scopeRefSchema` is a strict superset of the old org-only
     * shape, so every existing org-scoped record still validates.
     */
    scope: scopeRefSchema,
    /** `agent` (runnable) or `bundle` (a grouping of member agents). Defaults to
     * `agent` for back-compat with records/tests written before bundles existed. */
    kind: agentKindSchema.default('agent'),
    /**
     * The model a runnable agent uses. Required for `kind:'agent'` (enforced by
     * the superRefine below), but may be empty for a `kind:'bundle'` record —
     * a bundle is a grouping with nothing to run, the analog of a skill bundle's
     * empty `body`. Relaxed from `.min(1)` to a defaulted string so a bundle
     * record validates without a model.
     */
    model: z.string().default(''),
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
    /** For bundles: names of member agents (which may themselves be bundles). */
    members: z.array(z.string()).default([]),
    /**
     * Read-only annotation populated by the resolve endpoint for bundles: the
     * transitively-flattened leaf-agent member names (nested bundles expanded).
     * Never written by clients; present only on GET /agents responses.
     */
    resolvedMembers: z.array(z.string()).optional(),
    /** Authorship stamp set on create; optional on read for back-compat. */
    createdBy: createdBySchema.optional(),
  })
  .merge(versionFieldsSchema)
  .superRefine((a, ctx) => {
    // A runnable agent must declare a model; a bundle (a grouping record with
    // nothing to run) may omit it — mirrors a skill bundle's empty body.
    if (a.kind !== 'bundle' && a.model.trim().length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['model'],
        message: 'model is required for an agent',
      });
    }
  });
export type Agent = z.infer<typeof agentSchema>;

/**
 * The rerun-until-done rule on a workflow node. A node with a rerun rule loops
 * after each run until it is satisfied or the `maxRuns` safety cap is hit:
 *  - `self` — a Haiku judge asks whether `endCriteria` is satisfied (DONE/CONTINUE).
 *  - `declared-by` — node N reruns until checker node `declaredBy` declares N done
 *    (the generator↔checker loop). `declaredBy` is a CONTROL edge, not a DAG edge,
 *    so it is excluded from the acyclicity check below.
 * `maxRuns` guarantees a workflow can never loop forever.
 */
export const workflowRerunSchema = z.object({
  mode: z.enum(['self', 'declared-by']),
  /** `self` mode: the end-criteria a Haiku judge evaluates after each run. */
  endCriteria: z.string().default(''),
  /** `declared-by` mode: the node id of the checker that gates this node's loop. */
  declaredBy: z.string().optional(),
  /** Safety cap on the number of runs, so the loop can never run forever. */
  maxRuns: z.number().int().positive().default(10),
});
export type WorkflowRerun = z.infer<typeof workflowRerunSchema>;

/**
 * One node in a workflow DAG. It points to a catalog `agent` by name (the same
 * name pointers agents use for skills), carries per-node `prompt` instructions,
 * and declares its upstream dependencies as `dependsOn` node ids — those are the
 * DAG edges. An optional `rerun` rule makes the node loop until done.
 */
export const workflowNodeSchema = z.object({
  id: z.string().min(1),
  /** Name pointer to a catalog agent the node runs. */
  agent: z.string().min(1),
  label: z.string().default(''),
  /** Per-node task instructions handed to the agent. */
  prompt: z.string().default(''),
  /** Upstream node ids → the DAG edges feeding this node. */
  dependsOn: z.array(z.string()).default([]),
  rerun: workflowRerunSchema.optional(),
});
export type WorkflowNode = z.infer<typeof workflowNodeSchema>;

/**
 * A workflow record. Unlike agents/skills there is no bundle concept — a
 * workflow is itself the composition unit — so `kind` is the single literal
 * `'workflow'` (the field is kept for catalog parity and future bundling).
 */
export const WORKFLOW_KINDS = ['workflow'] as const;
export const workflowKindSchema = z.enum(WORKFLOW_KINDS);
export type WorkflowKind = z.infer<typeof workflowKindSchema>;

export const workflowSchema = z
  .object({
    name: z.string().min(1),
    /**
     * Catalog scope. `org` is the default tier (the org-wide catalog), but
     * user-scoped workflows are also representable so the catalog partitions by
     * org AND by user. `scopeRefSchema` is a strict superset of the old org-only
     * shape, so every existing org-scoped record still validates.
     */
    scope: scopeRefSchema,
    /** Single literal `'workflow'`; defaults for back-compat / catalog parity. */
    kind: workflowKindSchema.default('workflow'),
    description: z.string().default(''),
    /** The DAG: nodes carry their own `dependsOn` edges. */
    nodes: z.array(workflowNodeSchema).default([]),
    /** Authorship stamp set on create; optional on read for back-compat. */
    createdBy: createdBySchema.optional(),
  })
  .merge(versionFieldsSchema)
  .superRefine((wf, ctx) => {
    // (1) Node ids must be unique within the workflow.
    const seen = new Set<string>();
    wf.nodes.forEach((node, i) => {
      if (seen.has(node.id)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['nodes', i, 'id'],
          message: `duplicate node id '${node.id}'`,
        });
      }
      seen.add(node.id);
    });
    const ids = new Set(wf.nodes.map((n) => n.id));

    wf.nodes.forEach((node, i) => {
      // (2) Every dependsOn entry must resolve to an existing node id.
      node.dependsOn.forEach((dep, j) => {
        if (!ids.has(dep)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['nodes', i, 'dependsOn', j],
            message: `dependsOn references unknown node id '${dep}'`,
          });
        }
      });
      // (3) A declared-by rerun's checker must resolve to an existing node id.
      if (node.rerun?.mode === 'declared-by') {
        const checker = node.rerun.declaredBy;
        if (!checker || !ids.has(checker)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['nodes', i, 'rerun', 'declaredBy'],
            message: `rerun.declaredBy references unknown node id '${checker ?? ''}'`,
          });
        }
      }
    });

    // (4) The dependsOn graph must be ACYCLIC. `declaredBy` is a control edge and
    // is EXCLUDED here. Kahn's algorithm: if any node remains after peeling off
    // zero-indegree nodes, there is a cycle.
    const indegree = new Map<string, number>();
    const adj = new Map<string, string[]>();
    for (const id of ids) {
      indegree.set(id, 0);
      adj.set(id, []);
    }
    for (const node of wf.nodes) {
      for (const dep of node.dependsOn) {
        // edge dep -> node (dep must run before node).
        if (ids.has(dep)) {
          adj.get(dep)!.push(node.id);
          indegree.set(node.id, (indegree.get(node.id) ?? 0) + 1);
        }
      }
    }
    const queue: string[] = [];
    for (const [id, deg] of indegree) {
      if (deg === 0) queue.push(id);
    }
    let visited = 0;
    while (queue.length > 0) {
      const id = queue.shift()!;
      visited += 1;
      for (const next of adj.get(id) ?? []) {
        const deg = (indegree.get(next) ?? 0) - 1;
        indegree.set(next, deg);
        if (deg === 0) queue.push(next);
      }
    }
    if (visited < ids.size) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['nodes'],
        message: 'workflow dependsOn graph has a cycle',
      });
    }
  });
export type Workflow = z.infer<typeof workflowSchema>;

/**
 * A workflow RUN is the live execution status the executor reports and the web
 * tab polls. Unlike a workflow (a versioned catalog item), a run is NOT
 * versioned — it is a transient per-project record under `WORKFLOWRUN#<runId>`
 * carrying per-node progress. Kept deliberately minimal: just enough for the
 * Workflows tab to overlay live status onto the DAG view.
 *
 * Per-node `state`:
 *  - `pending`  — not yet started (its deps are still running / queued).
 *  - `running`  — the node's agent is executing.
 *  - `looping`  — the node finished a run and its rerun rule said CONTINUE.
 *  - `done`     — the node (and its rerun loop) completed successfully.
 *  - `failed`   — the node's run errored or its rerun cap was hit unsatisfied.
 */
export const workflowRunNodeStateSchema = z.enum([
  'pending',
  'running',
  'looping',
  'done',
  'failed',
]);
export type WorkflowRunNodeState = z.infer<typeof workflowRunNodeStateSchema>;

export const workflowRunSchema = z.object({
  runId: z.string().min(1),
  workflowName: z.string().min(1),
  projectId: z.string().min(1),
  /** Overall run lifecycle: running until every node settles, then done/failed. */
  status: z.enum(['running', 'done', 'failed']),
  /** Per-node progress, keyed by node id. `runs` counts rerun-loop iterations and
   * `outputTail` is the last slice of the node's captured stdout (for the tooltip). */
  nodes: z.record(
    z.object({
      state: workflowRunNodeStateSchema,
      runs: z.number().int().default(0),
      outputTail: z.string().default(''),
    }),
  ),
  /** Epoch-ms the run started / finished; absent until set by the executor. */
  startedAt: z.number().optional(),
  endedAt: z.number().optional(),
});
export type WorkflowRun = z.infer<typeof workflowRunSchema>;

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
/** Fields every transport branch shares (identity + scope + authorship + versioning). */
const mcpServerBase = z
  .object({
    name: z.string().min(1),
    /** Catalog scope. Org-only today, but kept as a full `scopeRefSchema` for
     * parity with skills/agents (every existing org-scoped record validates). */
    scope: scopeRefSchema,
    /** Authorship stamp set on create; optional on read for back-compat. */
    createdBy: createdBySchema.optional(),
  })
  .merge(versionFieldsSchema);

export const mcpServerSchema = z.discriminatedUnion('transport', [
  mcpServerBase.extend({
    transport: z.literal('stdio'),
    /** The executable the daemon spawns for a local (stdio) server. */
    command: z.string().min(1),
    /** Arguments passed to `command`. Defaults to []. */
    args: z.array(z.string()).default([]),
    /** Environment variables for the subprocess (plaintext secrets — see note). */
    env: z.record(z.string()).default({}),
  }),
  mcpServerBase.extend({
    transport: z.literal('http'),
    /** The remote endpoint URL the daemon connects to. */
    url: z.string().url(),
    /** Static request headers (plaintext secrets — see note). Defaults to {}. */
    headers: z.record(z.string()).default({}),
  }),
  mcpServerBase.extend({
    transport: z.literal('sse'),
    url: z.string().url(),
    headers: z.record(z.string()).default({}),
  }),
]);
export type McpServer = z.infer<typeof mcpServerSchema>;

export const SKILL_KINDS = ['skill', 'bundle'] as const;
export const skillKindSchema = z.enum(SKILL_KINDS);
export type SkillKind = z.infer<typeof skillKindSchema>;

export const skillSchema = z
  .object({
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
    /**
     * EMBEDDING IDEMPOTENCY STAMPS (skill-idea loop, U3). The stream consumer
     * re-embeds a skill on every mutation, but it is keyed off a content hash so
     * the bulk seed write of every skill × every org does not storm the embedding
     * API. `descHash` is a hash of `description + body` at the time the skill's
     * vector was last (re)generated; a MODIFY whose recomputed hash equals the
     * stored `descHash` is a no-op (the desc/body did not change). `embeddingVersion`
     * is the Bedrock embedding-version stamp the current vector was produced with
     * (mirrors the vector's metadata), so a model change can be detected and
     * trigger a reindex (U5). Both are absent on legacy / never-embedded records.
     */
    descHash: z.string().optional(),
    embeddingVersion: z.string().optional(),
  })
  .merge(versionFieldsSchema);
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
 * @deprecated The legacy two-state prose weekly report (KTD4). Superseded by the
 * itemized weekly-commit lifecycle (`weeklyPlanSchema` + `weeklyCommitSchema`
 * below). Kept ONLY so the legacy-migration unit (U14) and any straggler reader
 * can still parse the old `{ done, plan, conformityScore, validated }` records
 * already stored on Dynamo; new writes go through the commit lifecycle. Do not
 * extend.
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
/** @deprecated See {@link weeklyUpdateSchema}. */
export type WeeklyUpdate = z.infer<typeof weeklyUpdateSchema>;

// --- Weekly commit lifecycle (weekly-commit-lifecycle, U1) ------------------
//
// The PRD's structured weekly commit lifecycle that REPLACES the prose blob
// above. Two relations (KTD1): a `weekly_plan` (the week — `(projectId, isoWeek)`,
// carrying lifecycle `status` + declared concentration `posture`) and N
// `weekly_commit` items (each hard-linked to a Supporting Outcome OR carrying a
// typed `orphanReason` — KTD10). The "chess layer" (`category` + `priorityNumeric`)
// is DERIVED server-side (KTD4), not client-authored. The lifecycle is a full
// state machine (KTD2): DRAFT -> LOCKED -> RECONCILING -> RECONCILED.

/**
 * The weekly plan lifecycle (KTD2). Each transition is its own server endpoint
 * guarded by `canTransition`; an illegal transition is a 409. "Carry Forward" is
 * the OUTPUT of `/reconcile/complete`, not a fifth persistent state.
 *  - `DRAFT`       — editable; add / edit / delete commits.
 *  - `LOCKED`      — committed for the week (the new "publish"); planned fields frozen.
 *  - `RECONCILING` — recording per-commit actual status + outcome.
 *  - `RECONCILED`  — closed; carry-forward + roll-up/metrics recompute have run.
 */
export const WEEKLY_STATUSES = ['DRAFT', 'LOCKED', 'RECONCILING', 'RECONCILED'] as const;
export const weeklyStatusSchema = z.enum(WEEKLY_STATUSES);
export type WeeklyStatus = z.infer<typeof weeklyStatusSchema>;

/**
 * The DERIVED chess-layer category (KTD4). Computed server-side from the plan
 * implementation-unit + the linked SO's RCDO position — clients don't author it;
 * an override that contradicts the derived value is flagged, not silently taken.
 * Vocabulary is SHARED with the orphan reasons (KTD10): the orphan subset is
 * `KTLO | Incident | Exploration | ExternalAsk`, and the linked-work additions are
 * `Delivery | Strategic`. An orphan commit's reason IS its category.
 */
export const COMMIT_CATEGORIES = [
  'KTLO',
  'Incident',
  'Exploration',
  'ExternalAsk',
  'Delivery',
  'Strategic',
] as const;
export const commitCategorySchema = z.enum(COMMIT_CATEGORIES);
export type CommitCategory = z.infer<typeof commitCategorySchema>;

/**
 * The typed orphan lane (KTD10). A commit must carry EITHER a
 * `supportingOutcomeId` OR one of these reasons; "neither" is rejected (mirrors
 * the DB CHECK + the lock guard). The set is the subset of {@link COMMIT_CATEGORIES}
 * that can stand alone without an SO link — an orphan commit's reason is its
 * category. A recurring orphan reason is the trigger to add a new Supporting
 * Outcome.
 */
export const ORPHAN_REASONS = ['KTLO', 'Incident', 'Exploration', 'ExternalAsk'] as const;
export const orphanReasonSchema = z.enum(ORPHAN_REASONS);
export type OrphanReason = z.infer<typeof orphanReasonSchema>;

/**
 * The reconciliation outcome of a single commit (planned-vs-actual). `planned`
 * is the pre-reconciliation default; the others are terminal. Roll-up credit
 * (KTD5): `done = 1.0`, `partial = 0.5`, `planned`/`dropped = 0.0`. Carry-forward
 * (KTD3) clones `planned`/`partial` into next week; `dropped`/`done` never carry.
 */
export const COMMIT_OUTCOME_STATUSES = ['planned', 'done', 'partial', 'dropped'] as const;
export const commitOutcomeStatusSchema = z.enum(COMMIT_OUTCOME_STATUSES);
export type CommitOutcomeStatus = z.infer<typeof commitOutcomeStatusSchema>;

/**
 * The week's declared concentration posture (KTD8). The Strategic Concentration
 * Index is reported as DIVERGENCE from this declared intent, so legitimate
 * breadth is never punished: `focus` expects high concentration, `explore` low.
 */
export const POSTURES = ['focus', 'explore'] as const;
export const postureSchema = z.enum(POSTURES);
export type Posture = z.infer<typeof postureSchema>;

/** An ISO-week string, e.g. `2026-W23`. */
export const isoWeekSchema = z.string().regex(/^\d{4}-W\d{2}$/);

/**
 * One itemized weekly commit (KTD1). It carries EITHER a primary
 * `supportingOutcomeId` (the FK that drives ALL roll-up + concentration math —
 * KTD9) OR a typed `orphanReason` (KTD10) — enforced by the refinement below,
 * mirroring the DB CHECK. `alsoAdvances` is an OPTIONAL, INFORMATIONAL list of
 * secondary SO ids that surface in the leverage/manager view but NEVER split
 * roll-up credit (KTD9). `category` + `priorityNumeric` are present on the record
 * but DERIVED server-side (KTD4) — clients don't author them; the override path
 * is U10. Carry provenance (KTD3): `carriedFromWeek` / `carriedToWeek` / `carryDepth`.
 */
export const weeklyCommitSchema = z
  .object({
    id: z.string().min(1),
    projectId: z.string().min(1),
    isoWeek: isoWeekSchema,
    title: z.string().min(1),
    /** Primary SO link — drives ALL roll-up + concentration math. Null only when
     * `orphanReason` is set (enforced by the refinement). */
    supportingOutcomeId: z.string().optional(),
    /** Typed non-link (KTD10). Required when there is no `supportingOutcomeId`. */
    orphanReason: orphanReasonSchema.optional(),
    /** Informational secondary SO ids; never split roll-up credit (KTD9). */
    alsoAdvances: z.array(z.string()).default([]),
    /** DERIVED server-side (KTD4); not client-authored. */
    category: commitCategorySchema,
    /** DERIVED WSJF-from-the-tree leverage (KTD4); the commit list self-sorts by it. */
    priorityNumeric: z.number(),
    status: commitOutcomeStatusSchema.default('planned'),
    /** Free-form actual result recorded during reconciliation. */
    actualOutcome: z.string().optional(),
    /** Set on a carried clone: the week it was carried FROM (KTD3). */
    carriedFromWeek: isoWeekSchema.optional(),
    /** Stamped on the SOURCE when it is carried forward: the week it was carried TO. */
    carriedToWeek: isoWeekSchema.optional(),
    /** How many times this line has carried; `>= 3` raises a decompose/kill nudge. */
    carryDepth: z.number().int().nonnegative().default(0),
  })
  .superRefine((c, ctx) => {
    // KTD10: a commit must carry EITHER a primary SO link OR a typed orphan
    // reason — "neither" is rejected (mirrors the DB CHECK + the lock guard).
    if (!c.supportingOutcomeId && !c.orphanReason) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['supportingOutcomeId'],
        message: 'a commit must have a supportingOutcomeId or an orphanReason',
      });
    }
  });
export type WeeklyCommit = z.infer<typeof weeklyCommitSchema>;

/**
 * The weekly plan — the week itself (KTD1), keyed `(projectId, isoWeek)`. Carries
 * the lifecycle `status`, the declared concentration `posture`, and the transition
 * timestamps stamped by the lifecycle endpoints.
 */
export const weeklyPlanSchema = z.object({
  projectId: z.string().min(1),
  isoWeek: isoWeekSchema,
  status: weeklyStatusSchema.default('DRAFT'),
  posture: postureSchema.default('focus'),
  /** Epoch-ms stamped on DRAFT -> LOCKED. */
  lockedAt: z.number().int().nonnegative().optional(),
  /** Epoch-ms stamped on RECONCILING -> RECONCILED. */
  reconciledAt: z.number().int().nonnegative().optional(),
});
export type WeeklyPlan = z.infer<typeof weeklyPlanSchema>;

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

// --- Skill ideas (skill-idea loop, U6) -----------------------------------
//
// An IDEA is a SYNTHESIZED, skill-ready learned concept attached to its
// best-matching skill — prose that could be folded into a skill body as-is, NOT
// a raw transcript. Ideas are co-located with skills in the org scope partition
// (`SCOPE#org#<org>`) but live under an `IDEA#` SK prefix, so the `SKILL#`-prefix
// catalog scan never returns them. Corroboration is DERIVED: `|distinct
// sessionId|` over `sources` (deduped across segments), so one long multi-segment
// session counts once. An idea is "corroborated" at `>= K` distinct sessions.

export const IDEA_STATUSES = ['open', 'folded'] as const;
export const ideaStatusSchema = z.enum(IDEA_STATUSES);
export type IdeaStatus = z.infer<typeof ideaStatusSchema>;

/**
 * One contribution to an idea, keyed by `sessionId`. The corroboration unit is
 * the SESSION, so the `sources` array carries AT MOST one entry per distinct
 * `sessionId` (segments within a session collapse onto the same entry). The
 * `snippet` is provenance/evidence ONLY — it is never the idea's prose; the
 * synthesized concept lives on the idea's `text`.
 */
export const ideaSourceSchema = z.object({
  /** The corroboration unit. Distinct sessionIds drive the corroboration count. */
  sessionId: z.string().min(1),
  /** The segment within the session this finding came from (one of possibly many). */
  segmentId: z.string().min(1),
  /** Envelope per-session monotonic seq, for ordering/observability. */
  seq: z.number().int().nonnegative(),
  /** Raw evidence/provenance for this source — NOT the synthesized idea text. */
  snippet: z.string().default(''),
  /** Where the finding originated, for provenance + variant-scoped folding. */
  projectId: z.string().optional(),
  repoId: z.string().optional(),
});
export type IdeaSource = z.infer<typeof ideaSourceSchema>;

/**
 * A skill-attached idea. Persisted under `SCOPE#org#<org>` / `IDEA#<skillBaseName>#<ideaId>`
 * (see keys.ts `ideaKey`). Corroboration is NOT stored — it is derived as the
 * count of distinct `sessionId`s in `sources` (use `corroborationCount`).
 *
 * `corroborationVersion` is the optimistic-concurrency token: every conditional
 * corroboration update requires it to still equal what the writer read, so a
 * fold↔corroboration race never clobbers (mirrors the session projection's
 * `maxSeq` guard).
 */
export const ideaSchema = z.object({
  /** Stable id within `(org, skillBaseName)`. */
  ideaId: z.string().min(1),
  /** The skill family this idea is attached to (the idea is invisible to listSkills). */
  skillBaseName: z.string().min(1),
  /** Owning org — every idea row is org-partitioned for isolation. */
  org: z.string().min(1),
  /** The SYNTHESIZED, skill-ready concept — prose foldable into a skill body. */
  text: z.string().default(''),
  /** Distinct-session contributions; corroboration = `|distinct sessionId|`. */
  sources: z.array(ideaSourceSchema).default([]),
  status: ideaStatusSchema.default('open'),
  /** Set when folded: the skill revision the lesson was folded into. */
  foldedIntoRev: z.number().int().positive().optional(),
  /**
   * Post-fold evidence (U20). Once an idea is FOLDED, its lesson lives in the
   * skill body and it has dropped from the live candidate-learnings block. A new
   * session that corroborates the SAME (already-folded) lesson must NOT resurrect
   * it into the live block, reopen it, or spawn a fresh duplicate idea that would
   * re-surface a lesson already in the body. Instead the session attaches HERE —
   * post-fold evidence, visible only in the Command HQ history view (U13). These
   * sessions are NOT counted toward `corroborationCount` (which is the live
   * `sources` set); they are a separate, monotonic record that the lesson kept
   * recurring after it was folded. Distinct on `sessionId`, and never overlapping
   * the live `sources`.
   */
  postFoldSources: z.array(ideaSourceSchema).default([]),
  /** The embedding model version the idea's vector was generated with. */
  ideaEmbeddingVersion: z.string().optional(),
  /** Optimistic-concurrency token for conditional corroboration updates. */
  corroborationVersion: z.number().int().nonnegative().default(0),
  createdAt: z.number().int().nonnegative(),
  updatedAt: z.number().int().nonnegative(),
});
export type Idea = z.infer<typeof ideaSchema>;

/** The corroboration count of an idea: distinct sessions, deduped across segments. */
export function corroborationCount(idea: Pick<Idea, 'sources'>): number {
  return new Set(idea.sources.map((s) => s.sessionId)).size;
}

/**
 * An entry in the org's UNASSIGNED bin — a topic the judge rejected from every
 * candidate skill (the new-skill backlog). Lives under `SCOPE#org#<org>` /
 * `IDEABIN#<entryId>` so the whole bin is one partition read, scoped per org.
 */
export const unassignedEntrySchema = z.object({
  /** Stable id within `(org)`. */
  entryId: z.string().min(1),
  org: z.string().min(1),
  /** The topic text / synthesized finding that found no home. */
  text: z.string().default(''),
  /** Provenance of the rejected finding (same shape as an idea source). */
  sources: z.array(ideaSourceSchema).default([]),
  createdAt: z.number().int().nonnegative(),
  updatedAt: z.number().int().nonnegative(),
});
export type UnassignedEntry = z.infer<typeof unassignedEntrySchema>;

// --- Golden-set regression at fold (skill-idea loop, U18) ------------------
//
// Every fold captures the lesson it folded as a before→after EXPECTATION: the
// skill body BEFORE the fold, the AFTER body the fold produced, and the
// synthesized lesson the fold was meant to encode. The cases are co-located
// with the skill (`SCOPE#org#<org>` / `IDEAGOLD#<skillBaseName>#<id>`), mirroring
// the idea side-record shape. Before a later promote, a Bedrock judge REPLAYS
// each prior golden case against the candidate revision body and reports whether
// the candidate still satisfies the prior expectation. v1 is ADVISORY — the
// human is the gate; a regression is surfaced, never hard-blocked.

export const goldenCaseSchema = z.object({
  /** Stable id within `(org, skillBaseName)` — typically the folded `ideaId`. */
  caseId: z.string().min(1),
  /** The skill family this case guards. */
  skillBaseName: z.string().min(1),
  /** Owning org — every case row is org-partitioned for isolation. */
  org: z.string().min(1),
  /** The idea that produced this fold (provenance / dedupe). */
  ideaId: z.string().min(1),
  /** The synthesized lesson the fold was meant to encode (the expectation). */
  lesson: z.string().default(''),
  /** The skill body BEFORE the fold. */
  before: z.string().default(''),
  /** The skill body AFTER the fold (the revision the lesson was folded into). */
  after: z.string().default(''),
  /** The revision the fold produced (the `after` body's rev). */
  foldedIntoRev: z.number().int().positive().optional(),
  createdAt: z.number().int().nonnegative(),
});
export type GoldenCase = z.infer<typeof goldenCaseSchema>;

/**
 * The verdict of replaying ONE golden case against a candidate revision body:
 * does the candidate still satisfy the prior expectation (`lesson`)? `satisfied`
 * false means a REGRESSION — the candidate appears to undo an earlier fold.
 */
export const goldenReplayResultSchema = z.object({
  caseId: z.string().min(1),
  lesson: z.string().default(''),
  /** True when the candidate still honors the prior lesson; false = regression. */
  satisfied: z.boolean(),
  /** The judge's one-line rationale (advisory surface for the human). */
  reason: z.string().default(''),
});
export type GoldenReplayResult = z.infer<typeof goldenReplayResultSchema>;

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
