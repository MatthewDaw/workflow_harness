import { createApi, fetchBaseQuery } from '@reduxjs/toolkit/query/react';
import type { Dispatch } from '@reduxjs/toolkit';
import type {
  Project,
  SessionProjection,
  ObjectiveNode,
  Agent,
  Workflow,
  WorkflowRun,
  Skill,
  McpServer,
  WeeklyPlan,
  WeeklyCommit,
  OrphanReason,
  ScopeRef,
  ControlAction,
  DefinitionOfDone,
  Envelope,
  LearningRecord,
  LearningStream,
  Idea,
  UnassignedEntry,
  MeResponse,
  Memory,
} from '@harness/shared';

// Re-exported for consumers that read memories through this module.
export type { Memory } from '@harness/shared';
// Re-exported so the weekly screens (U10–U12) read these straight off this module.
export type { WeeklyPlan, WeeklyCommit, OrphanReason } from '@harness/shared';

/**
 * One week as the weekly endpoints serve it (U3): the lifecycle `plan` plus its
 * itemized `commits`, already priority-sorted server-side. Both the list read
 * (`getWeeks`) and the single-week read (`getWeek`) return this shape.
 */
export interface WeekView {
  plan: WeeklyPlan;
  commits: WeeklyCommit[];
}

/**
 * One exception surfaced on a report's manager brief (U8) — the thing that needs
 * the manager. Mirrors the backend `BriefException`; `kind` is a stable string so
 * the UI can group/icon them.
 */
export interface BriefException {
  kind:
    | 'highest_leverage_not_started'
    | 'oldest_carry'
    | 'longest_starved_outcome'
    | 'lock_failure';
  detail: string;
  commitId?: string;
  supportingOutcomeId?: string;
}

/**
 * A report's strategic-concentration signal (U17), reported as divergence from
 * their declared posture. Mirrors the backend `ConcentrationSignal`.
 */
export interface ConcentrationSignal {
  herfindahl: number;
  nodes: number;
  postureDivergence: number;
}

/** The manager-brief node for one report (U8). */
export interface ReportBrief {
  userId: string;
  name?: string;
  /** The report's most-recent week across their projects, or null when they have none. */
  latestWeek: { projectId: string; isoWeek: string; status: WeeklyPlan['status'] } | null;
  /** The exceptions needing the manager's attention; empty ⇒ "nothing needs you". */
  exceptions: BriefException[];
  concentration: ConcentrationSignal;
}

/**
 * The reports-scoped exception/divergence brief (U8): the current page of report
 * nodes plus the keyset `nextCursor` (absent on the last page).
 */
export interface ManagerBrief {
  reports: ReportBrief[];
  nextCursor?: string;
}

/**
 * RTK Query slice mirroring the backend REST surface. All DTOs come from
 * @harness/shared so the web stays in lockstep with the contract.
 *
 * The base URL comes from VITE_API_BASE_URL; the live-WS middleware
 * (src/ws/liveMiddleware.ts) pushes session events straight into this cache so
 * subscribed components update without refetching.
 *
 * Read endpoints accept either the wrapped backend shape (`{ tickets: [...] }`)
 * or a bare array/object — the test fetch stub serves bare values while the real
 * handlers wrap them. `unwrap*` below normalizes both.
 */

/**
 * Every cache arg `getSessions` is queried with. Any code that patches the
 * sessions lists (the live-WS fold, sendControl's optimistic update) must touch
 * all three or a cached list goes stale.
 */
export const SESSION_LIST_ARGS = [undefined, { live: true }, { live: false }] as const;

/** A node in the project's detailed-requirements doc tree. */
export interface ProjectDoc {
  /** Repo-relative path, e.g. `docs/requirements/auth.md`. */
  path: string;
  /** Human title (heading or filename). */
  title: string;
  /** GitHub-sourced completion 0–100 parsed from the doc's `completion:` front-matter. */
  completion: number;
}

/**
 * One skill idea as the all-ideas endpoint serves it: the shared `Idea` plus
 * its DERIVED corroboration count (distinct sessions), so the dropdown never
 * recomputes the count from `sources`.
 */
export type SkillIdea = Idea & { corroborationCount: number };

/**
 * One unassigned-bin entry as the backlog endpoint serves it: the shared
 * `UnassignedEntry` plus its DERIVED frequency (distinct sessions), so a
 * recurring off-catalog topic sorts to the top without recomputing.
 */
export type UnassignedIdea = UnassignedEntry & { frequency: number };

/** Markdown body of a single detailed-requirements doc. */
export interface ProjectDocContent {
  path: string;
  markdown: string;
}

/** Project requirements markdown, sourced read-only from GitHub `docs/PRD.md`. */
export interface ProjectRequirements {
  markdown: string;
  stale?: boolean;
}

/** Project wireframe HTML, sourced read-only from GitHub `docs/wireframe.html`. */
export interface ProjectWireframe {
  /** Raw HTML of the wireframe; empty string when the file is absent. */
  html: string;
  stale?: boolean;
}

/**
 * Catalog versioning. A catalog item is identified by `(baseName, repoId,
 * userId)`; editing a skill from a project forks/updates that variant. Every
 * variant carries an immutable revision (`version`/`rev`). One ORG-WIDE "true"
 * variant per `baseName` is the default shown in the catalog and added to a
 * project.
 *
 * Version fields may be absent on a given record, so the web reads them
 * defensively via `variantOf()` below — legacy records resolve to their own
 * `name` as the base variant at rev 1.
 */
export interface VariantMeta {
  /** Stable id for the `(baseName, repoId, userId)` triple. */
  variantId: string;
  /** The shared skill/agent/server name this variant belongs to. */
  baseName: string;
  /** Repo that forked this variant (empty/undefined = base variant). */
  repoId?: string;
  /** Author who forked this variant (empty/undefined = base variant). */
  authorUserId?: string;
  /** Immutable revision number, starting at 1. */
  version: number;
  /** Whether this is the org-wide TRUE variant for its baseName. */
  isTrue?: boolean;
  createdAt?: number;
}

/**
 * One variant/revision row as the variants endpoint serves it: the variant
 * provenance plus the human-facing label fields the switcher renders.
 */
export interface SkillVariant extends VariantMeta {
  /** Display name (usually the baseName). */
  name: string;
  description?: string;
  /** Resolved author display name, when the backend joins it. */
  authorName?: string;
}

/**
 * Read a catalog record's variant metadata defensively. A record from the
 * versioning-aware backend carries `variantId`/`baseName`/`version`; a legacy
 * (pre-migration) record carries none, so it resolves to the BASE variant of
 * its own `name` at rev 1 — exactly the migration contract.
 */
export function variantOf(item: {
  name: string;
  variantId?: string;
  baseName?: string;
  repoId?: string;
  authorUserId?: string;
  version?: number;
  rev?: number;
  isTrue?: boolean;
}): VariantMeta {
  const baseName = item.baseName ?? item.name;
  return {
    variantId: item.variantId ?? `${baseName}#base`,
    baseName,
    repoId: item.repoId,
    authorUserId: item.authorUserId,
    version: item.version ?? item.rev ?? 1,
    isTrue: item.isTrue,
  };
}

function unwrapArray<T>(key: string) {
  return (resp: unknown): T[] => {
    if (Array.isArray(resp)) return resp as T[];
    const wrapped = (resp as Record<string, unknown>)?.[key];
    return Array.isArray(wrapped) ? (wrapped as T[]) : [];
  };
}

function unwrapOne<T>(key: string) {
  return (resp: unknown): T => {
    if (resp && typeof resp === 'object' && key in (resp as object)) {
      return (resp as Record<string, T>)[key] as T;
    }
    return resp as T;
  };
}

/** Encode a scope as the query string the REST handlers expect (`tier`/`id`). */
export function scopeQuery(scope: ScopeRef): string {
  return `tier=${encodeURIComponent(scope.tier)}&id=${encodeURIComponent(scope.id)}`;
}

/** Everything that is scoped to the active org and must refetch when membership changes. */
const ORG_SCOPED_TAGS = [
  'Me',
  'Objective',
  'Agent',
  'Skill',
  'Project',
  'Session',
  'Weekly',
  'Dod',
] as const;

/**
 * Shared by createOrg/joinOrg: after a successful write, everything the old
 * (no-)org cached is wrong, so drop every org-scoped dataset and let each query
 * refetch under the new membership. A rejected write (400/403/409) changed no
 * membership, so there is nothing to reset; the rejection is swallowed HERE
 * only because the component's own `.unwrap()` catch surfaces it to the user.
 */
async function resetOrgScopedCacheOnSuccess(
  queryFulfilled: Promise<unknown>,
  dispatch: Dispatch,
): Promise<void> {
  try {
    await queryFulfilled;
  } catch {
    return;
  }
  dispatch(baseApi.util.invalidateTags([...ORG_SCOPED_TAGS]));
}

export const baseApi = createApi({
  reducerPath: 'api',
  baseQuery: fetchBaseQuery({
    baseUrl: import.meta.env.VITE_API_BASE_URL ?? '/api',
    prepareHeaders: (headers, { getState }) => {
      const token = (getState() as { auth?: { idToken?: string | null } }).auth?.idToken;
      if (token) headers.set('authorization', `Bearer ${token}`);
      return headers;
    },
  }),
  tagTypes: [
    'Me',
    'Project',
    'Session',
    'Objective',
    'Agent',
    'Workflow',
    'WorkflowRun',
    'Skill',
    'McpServer',
    'Variant',
    'Weekly',
    'WeeklyCommit',
    'Docs',
    'Requirements',
    'Dod',
    'Learnings',
    'Memory',
    'Idea',
    'Bin',
  ],
  endpoints: (build) => {
    /**
     * One enable/disable mutation pair for a project enabled-set:
     * POST/DELETE `projects/:projectId/<segment>/:<argKey>`. The server owns the
     * union/strip semantics (see each endpoint's contract line below); every
     * pair invalidates the project so its detail screens refetch. `enableBody`
     * lets the enable side carry an optional request body (the skill variant pin).
     */
    const projectToggle = <Arg extends { projectId: string }>(
      segment: string,
      argKey: keyof Arg & string,
      enableBody?: (arg: Arg) => Record<string, unknown> | undefined,
    ) => {
      const mutation = (method: 'POST' | 'DELETE') =>
        build.mutation<Project, Arg>({
          query: (arg) => {
            const body = method === 'POST' ? enableBody?.(arg) : undefined;
            return {
              url: `projects/${arg.projectId}/${segment}/${encodeURIComponent(String(arg[argKey]))}`,
              method,
              ...(body !== undefined ? { body } : {}),
            };
          },
          transformResponse: unwrapOne<Project>('project'),
          invalidatesTags: (_r, _e, { projectId }) => [
            'Project',
            { type: 'Project', id: projectId },
          ],
        });
      return { enable: mutation('POST'), disable: mutation('DELETE') };
    };

    // Enable carries the optional variant pin `{ variantId }` in the body; the
    // name stays in the URL. The pin defaults server-side to the current TRUE variant.
    const skillToggle = projectToggle<{
      projectId: string;
      skillName: string;
      variantId?: string;
    }>('skills', 'skillName', ({ variantId }) =>
      variantId !== undefined ? { variantId } : undefined,
    );
    const mcpServerToggle = projectToggle<{ projectId: string; name: string }>(
      'mcp-servers',
      'name',
    );
    const bundleToggle = projectToggle<{ projectId: string; bundleName: string }>(
      'bundles',
      'bundleName',
    );
    const agentToggle = projectToggle<{ projectId: string; agentName: string }>(
      'agents',
      'agentName',
    );
    const workflowToggle = projectToggle<{ projectId: string; workflowName: string }>(
      'workflows',
      'workflowName',
    );
    const agentBundleToggle = projectToggle<{ projectId: string; bundleName: string }>(
      'agent-bundles',
      'bundleName',
    );

    return {
      /**
       * The signed-in principal and their REAL org membership. Unlike the data
       * handlers, /me never falls back to the token claim, so a user with no
       * profile.org reads back `org: null` and the OrgGate forces them to create
       * or join an org before the app mounts.
       */
      getMe: build.query<MeResponse, void>({
        query: () => 'me',
        // Bare object today; unwrapOne tolerates a future wrapped { me: {...} }.
        transformResponse: unwrapOne<MeResponse>('me'),
        providesTags: ['Me'],
      }),

      /** Create a new org; the creator becomes its admin and profile.org is set server-side. */
      createOrg: build.mutation<
        { org: string; admin: boolean },
        { name: string; password: string }
      >({
        query: (body) => ({ url: 'orgs', method: 'POST', body }),
        invalidatesTags: ['Me'],
        async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
          await resetOrgScopedCacheOnSuccess(queryFulfilled, dispatch);
        },
      }),

      /** Join an EXISTING org by exact name + password; sets the caller's profile.org. */
      joinOrg: build.mutation<{ org: string; admin: boolean }, { name: string; password: string }>(
        {
          query: (body) => ({ url: 'orgs/join', method: 'POST', body }),
          invalidatesTags: ['Me'],
          async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
            await resetOrgScopedCacheOnSuccess(queryFulfilled, dispatch);
          },
        },
      ),

      /**
       * Switch the ACTIVE org to another one the user has already joined (no
       * password). Switching swaps EVERY org-scoped dataset, so on success the
       * whole API cache is reset — a clean wipe guarantees no stale tenant data
       * lingers — and /me + all screens re-read under the new org.
       */
      switchOrg: build.mutation<{ org: string; admin: boolean }, { org: string }>({
        query: (body) => ({ url: 'me/org', method: 'POST', body }),
        async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
          try {
            await queryFulfilled;
          } catch {
            return; // not a member / bad request → active org unchanged
          }
          dispatch(baseApi.util.resetApiState());
        },
      }),

      getProjects: build.query<Project[], void>({
        query: () => 'projects',
        transformResponse: unwrapArray<Project>('projects'),
        providesTags: ['Project'],
      }),
      getProject: build.query<Project, string>({
        query: (id) => `projects/${id}`,
        transformResponse: unwrapOne<Project>('project'),
        providesTags: (_r, _e, id) => [{ type: 'Project', id }],
      }),

      /**
       * Connect a GitHub repo as a new project. The caller supplies
       * `{ id, name, repo }`; the backend stamps the owner from the auth
       * principal — a client-supplied owner is never trusted.
       */
      createProject: build.mutation<Project, { id: string; name: string; repo: string }>({
        query: (body) => ({ url: 'projects', method: 'POST', body }),
        transformResponse: unwrapOne<Project>('project'),
        invalidatesTags: ['Project'],
      }),

      /**
       * Re-read a project's framing from GitHub (completion %, PRD goal, owned
       * Supporting Outcomes). GitHub being unreachable degrades to stale, never errors.
       */
      refreshProject: build.mutation<Project, string>({
        query: (id) => ({ url: `projects/${id}/refresh`, method: 'POST' }),
        transformResponse: unwrapOne<Project>('project'),
        invalidatesTags: (_r, _e, id) => ['Project', { type: 'Project', id }],
      }),

      /**
       * Forget a project (and everything under it) plus its repo pointer; the
       * GitHub repo is untouched, so it can be reconnected. Owner-or-admin gated
       * server-side.
       */
      deleteProject: build.mutation<{ deleted: boolean }, string>({
        query: (id) => ({ url: `projects/${id}`, method: 'DELETE' }),
        invalidatesTags: (_r, _e, id) => ['Project', { type: 'Project', id }],
      }),

      getSessions: build.query<SessionProjection[], { live?: boolean } | void>({
        query: (arg) => (arg && arg.live ? 'sessions?live=true' : 'sessions'),
        transformResponse: unwrapArray<SessionProjection>('sessions'),
        providesTags: ['Session'],
      }),
      getSession: build.query<SessionProjection, string>({
        query: (id) => `sessions/${id}`,
        transformResponse: unwrapOne<SessionProjection>('session'),
        providesTags: (_r, _e, id) => [{ type: 'Session', id }],
      }),

      /**
       * Backfill a session's stored event history (the last N events) from the
       * `{ session, events }` body of GET /sessions/:id, so the Watch & steer
       * feed shows history immediately; the live-WS stream carries everything
       * thereafter and the LiveWatch view merges the two by `seq`.
       */
      getSessionEvents: build.query<Envelope[], { id: string; limit?: number }>({
        query: ({ id, limit }) => `sessions/${id}${limit ? `?limit=${limit}` : ''}`,
        transformResponse: unwrapArray<Envelope>('events'),
        providesTags: (_r, _e, { id }) => [{ type: 'Session', id }],
      }),

      getObjectives: build.query<ObjectiveNode[], void>({
        query: () => 'objectives',
        transformResponse: unwrapArray<ObjectiveNode>('nodes'),
        providesTags: ['Objective'],
      }),

      /**
       * Create or update a Company Objective node (admin-gated server-side); the
       * backend stamps the caller's org. Title edits reuse this with the node's
       * existing id.
       */
      createObjective: build.mutation<
        ObjectiveNode,
        { id?: string; level: ObjectiveNode['level']; title: string; parentId?: string }
      >({
        query: (body) => ({ url: 'objectives', method: 'POST', body }),
        transformResponse: unwrapOne<ObjectiveNode>('node'),
        invalidatesTags: ['Objective'],
      }),

      /** Delete a Company Objective node by id (admin-gated server-side). */
      deleteObjective: build.mutation<{ deleted: boolean }, string>({
        query: (id) => ({ url: `objectives/${encodeURIComponent(id)}`, method: 'DELETE' }),
        invalidatesTags: ['Objective'],
      }),

      /**
       * The org-wide Definition of Done — advisory config declaring what
       * `/update-progress` verifies. The backend serves the floor default when
       * unset, so this never errors empty.
       */
      getDod: build.query<DefinitionOfDone, void>({
        query: () => 'dod',
        transformResponse: unwrapOne<DefinitionOfDone>('dod'),
        providesTags: ['Dod'],
      }),

      /** Set the org-wide Definition of Done (admin-gated server-side). */
      putDod: build.mutation<DefinitionOfDone, DefinitionOfDone>({
        query: (dod) => ({ url: 'dod', method: 'PUT', body: dod }),
        transformResponse: unwrapOne<DefinitionOfDone>('dod'),
        invalidatesTags: ['Dod'],
      }),

      /** Org agent catalog (collapsed model): no projectId, org scope only. */
      getAgents: build.query<Agent[], void>({
        query: () => 'agents',
        transformResponse: unwrapArray<Agent>('agents'),
        providesTags: ['Agent'],
      }),
      /** Org skill catalog (collapsed model): no projectId, org scope only. */
      getSkills: build.query<Skill[], void>({
        query: () => 'skills',
        transformResponse: unwrapArray<Skill>('skills'),
        providesTags: ['Skill'],
      }),

      /** Org workflow catalog (collapsed model): no projectId, org scope only. */
      getWorkflows: build.query<Workflow[], void>({
        query: () => 'workflows',
        transformResponse: unwrapArray<Workflow>('workflows'),
        providesTags: ['Workflow'],
      }),
      /** A single workflow by name (the editor's load-on-edit). */
      getWorkflow: build.query<Workflow, string>({
        query: (name) => `workflows/${encodeURIComponent(name)}`,
        transformResponse: unwrapOne<Workflow>('workflow'),
        providesTags: (_r, _e, name) => [{ type: 'Workflow', id: name }],
      }),

      /**
       * Every variant/revision of one skill name, for the catalog's variant
       * switcher; the current TRUE variant is flagged so the UI defaults to it.
       * Tagged by name so a promote/edit refetches just its list.
       */
      getSkillVariants: build.query<SkillVariant[], string>({
        query: (name) => `skills/${encodeURIComponent(name)}/variants`,
        transformResponse: unwrapArray<SkillVariant>('variants'),
        providesTags: (_r, _e, name) => [{ type: 'Variant', id: name }],
      }),

      /**
       * Every idea proposed for one skill — corroborated, uncorroborated, AND
       * folded history; no gate, no cap (the corroborated-only gate lives on the
       * separate candidate-learnings path). Per-skill `Idea` tag so a fold of
       * that skill's idea invalidates just this list.
       */
      getSkillIdeas: build.query<SkillIdea[], string>({
        query: (name) => `skills/${encodeURIComponent(name)}/ideas`,
        transformResponse: unwrapArray<SkillIdea>('ideas'),
        providesTags: (_r, _e, name) => [{ type: 'Idea', id: name }],
      }),

      /**
       * The org's unassigned bin: topics the judge rejected from every candidate
       * skill (the new-skill backlog), each with its distinct-session frequency.
       * READ is open to any member; acting on an entry is admin-gated server-side.
       */
      getUnassignedIdeas: build.query<UnassignedIdea[], void>({
        query: () => 'ideas/unassigned',
        transformResponse: unwrapArray<UnassignedIdea>('entries'),
        providesTags: ['Bin'],
      }),

      /**
       * Promote a variant to the org-wide TRUE version for its name. NOT
       * admin-gated; promotion only repoints the TRUE pointer, never edits or
       * deletes a variant. Optional `rev` pins a specific revision (defaults to
       * the variant's current revision).
       */
      promoteSkill: build.mutation<
        { name: string; variantId: string; rev: number },
        { name: string; variantId: string; rev?: number }
      >({
        query: ({ name, variantId, rev }) => ({
          url: `skills/${encodeURIComponent(name)}/promote`,
          method: 'POST',
          body: rev !== undefined ? { variantId, rev } : { variantId },
        }),
        invalidatesTags: (_r, _e, { name }) => ['Skill', { type: 'Variant', id: name }],
      }),

      /** Org MCP-server catalog (collapsed model): no projectId, org scope only. */
      getMcpServers: build.query<McpServer[], void>({
        query: () => 'mcp-servers',
        transformResponse: unwrapArray<McpServer>('mcpServers'),
        providesTags: ['McpServer'],
      }),
      /** A single MCP server by name (the editor's load-on-edit). */
      getMcpServer: build.query<McpServer, string>({
        query: (name) => `mcp-servers/${encodeURIComponent(name)}`,
        transformResponse: unwrapOne<McpServer>('mcpServer'),
        providesTags: (_r, _e, name) => [{ type: 'McpServer', id: name }],
      }),

      /**
       * Every week for a project (U3): each is a `{ plan, commits }` view, the
       * commits priority-sorted server-side. The legacy prose `getWeekly` (a flat
       * `WeeklyUpdate[]`) is gone — LOCK replaces publish and the report is now
       * itemized (KTD1/KTD4). Tagged `Weekly`+`WeeklyCommit` so a commit write or a
       * lifecycle transition refetches it.
       */
      getWeeks: build.query<WeekView[], string>({
        query: (projectId) => `projects/${projectId}/weekly`,
        transformResponse: unwrapArray<WeekView>('weeks'),
        providesTags: ['Weekly', 'WeeklyCommit'],
      }),

      /**
       * One week (U3): the lifecycle `plan` plus its commits. A week never written
       * to is a 404 server-side; RTK surfaces that as an error to the caller.
       */
      getWeek: build.query<WeekView, { projectId: string; isoWeek: string }>({
        query: ({ projectId, isoWeek }) => `projects/${projectId}/weekly/${isoWeek}`,
        transformResponse: unwrapOne<WeekView>('week'),
        providesTags: (_r, _e, { isoWeek }) => [
          'Weekly',
          'WeeklyCommit',
          { type: 'Weekly', id: isoWeek },
        ],
      }),

      // ---- Two-tier requirements ----

      /** Detailed-requirements doc tree for a project (repo-sourced). */
      getProjectDocs: build.query<ProjectDoc[], string>({
        query: (projectId) => `projects/${projectId}/docs`,
        transformResponse: unwrapArray<ProjectDoc>('docs'),
        providesTags: (_r, _e, id) => [{ type: 'Docs', id }],
      }),

      /** Markdown body of a single detailed-requirements doc. */
      getProjectDocContent: build.query<ProjectDocContent, { projectId: string; path: string }>({
        query: ({ projectId, path }) =>
          `projects/${projectId}/docs/content?path=${encodeURIComponent(path)}`,
        transformResponse: unwrapOne<ProjectDocContent>('content'),
        providesTags: (_r, _e, { path }) => [{ type: 'Docs', id: path }],
      }),

      /** Project requirements markdown from GitHub `docs/PRD.md` (read-only). */
      getProjectRequirements: build.query<ProjectRequirements, string>({
        query: (projectId) => `projects/${projectId}/requirements`,
        transformResponse: unwrapOne<ProjectRequirements>('requirements'),
        providesTags: (_r, _e, id) => [{ type: 'Requirements', id }],
      }),

      /** Project wireframe HTML from GitHub `docs/wireframe.html` (read-only). */
      getProjectWireframe: build.query<ProjectWireframe, string>({
        query: (projectId) => `projects/${projectId}/wireframe`,
        transformResponse: unwrapOne<ProjectWireframe>('wireframe'),
        providesTags: (_r, _e, id) => [{ type: 'Requirements', id }],
      }),

      /**
       * The project's mined learnings. Optional `stream` (`impl`|`doc`) filters
       * server-side; omitted returns both streams.
       */
      getProjectLearnings: build.query<
        LearningRecord[],
        { projectId: string; stream?: LearningStream }
      >({
        query: ({ projectId, stream }) =>
          `projects/${projectId}/learnings${stream ? `?stream=${stream}` : ''}`,
        transformResponse: unwrapArray<LearningRecord>('learnings'),
        providesTags: (_r, _e, { projectId }) => [{ type: 'Learnings', id: projectId }],
      }),

      /**
       * The project's per-user Claude Code memories, synced up from claude+. The
       * flat list spans every author; the Memories tab groups it by `userId`.
       */
      getProjectMemories: build.query<Memory[], string>({
        query: (projectId) => `projects/${projectId}/memories`,
        transformResponse: unwrapArray<Memory>('memories'),
        providesTags: (_r, _e, id) => [{ type: 'Memory', id }],
      }),

      // ---- Catalog mutations ----

      /** Create or update an agent (the editor's Save & sync; server forces org scope). */
      saveAgent: build.mutation<Agent, Agent>({
        query: (agent) => ({ url: 'agents', method: 'POST', body: agent }),
        transformResponse: unwrapOne<Agent>('agent'),
        invalidatesTags: ['Agent'],
      }),

      /** Create or update a workflow (the editor's Save & sync; server forces org scope). */
      saveWorkflow: build.mutation<Workflow, Workflow>({
        query: (workflow) => ({ url: 'workflows', method: 'POST', body: workflow }),
        transformResponse: unwrapOne<Workflow>('workflow'),
        invalidatesTags: ['Workflow'],
      }),

      /**
       * Promote a variant to the org-wide TRUE version for its workflow name.
       * Mirrors promoteSkill: NOT admin-gated, only repoints the TRUE pointer,
       * optional `rev` pins a revision.
       */
      promoteWorkflow: build.mutation<
        { name: string; variantId: string; rev: number },
        { name: string; variantId: string; rev?: number }
      >({
        query: ({ name, variantId, rev }) => ({
          url: `workflows/${encodeURIComponent(name)}/promote`,
          method: 'POST',
          body: rev !== undefined ? { variantId, rev } : { variantId },
        }),
        invalidatesTags: (_r, _e, { name }) => ['Workflow', { type: 'Variant', id: name }],
      }),

      /** Create or update an MCP server (a single POST handles both; server forces org scope). */
      saveMcpServer: build.mutation<McpServer, McpServer>({
        query: (server) => ({ url: 'mcp-servers', method: 'POST', body: server }),
        transformResponse: unwrapOne<McpServer>('mcpServer'),
        invalidatesTags: ['McpServer'],
      }),

      /** Delete an MCP server from the org catalog by name (admin-gated server-side). */
      deleteMcpServer: build.mutation<{ deleted: boolean }, string>({
        query: (name) => ({ url: `mcp-servers/${encodeURIComponent(name)}`, method: 'DELETE' }),
        invalidatesTags: ['McpServer'],
      }),

      // ---- Project opt-in (collapsed org-catalog model) ----
      // Server-side semantics per pair:
      //  - skills: enable adds to enabledSkills (idempotent), optionally pinning a
      //    variant via the body; disable removes it.
      //  - mcp-servers: enable/disable edits enabledMcpServers.
      //  - bundles: enable records the bundle in enabledBundles (INTENT annotation)
      //    AND unions its member skills into enabledSkills (the flat set the daemon
      //    materializes); disable strips the members it contributed.
      //  - agents: enable adds to enabledAgents and unions the agent's skills
      //    (bundles flattened) into enabledSkills; disable does NOT prune enabledSkills.
      //  - workflows: enable adds to enabledWorkflows and unions every referenced
      //    node's agent (transitively their skills/MCP servers); disable does NOT prune.
      //  - agent-bundles: like bundles, for enabledAgentBundles/enabledAgents
      //    (members shared with another enabled bundle survive a disable).
      enableProjectSkill: skillToggle.enable,
      disableProjectSkill: skillToggle.disable,
      enableProjectMcpServer: mcpServerToggle.enable,
      disableProjectMcpServer: mcpServerToggle.disable,
      enableProjectBundle: bundleToggle.enable,
      disableProjectBundle: bundleToggle.disable,
      enableProjectAgent: agentToggle.enable,
      disableProjectAgent: agentToggle.disable,
      enableProjectWorkflow: workflowToggle.enable,
      disableProjectWorkflow: workflowToggle.disable,
      enableProjectAgentBundle: agentBundleToggle.enable,
      disableProjectAgentBundle: agentBundleToggle.disable,

      /**
       * Kick off a headless run of a workflow against a project; returns the run
       * record (with `runId`) the live overlay then polls via getWorkflowRun.
       */
      startWorkflowRun: build.mutation<WorkflowRun, { name: string; projectId: string }>({
        query: ({ name, projectId }) => ({
          url: `workflows/${encodeURIComponent(name)}/runs`,
          method: 'POST',
          body: { projectId },
        }),
        transformResponse: unwrapOne<WorkflowRun>('run'),
        invalidatesTags: ['WorkflowRun'],
      }),

      /**
       * One workflow run's live status, polled while `status === 'running'`. The
       * run record lives under the PROJECT partition, so the read handler
       * requires `projectId` as a query param (it 400s without it).
       */
      getWorkflowRun: build.query<WorkflowRun, { name: string; runId: string; projectId: string }>(
        {
          query: ({ name, runId, projectId }) =>
            `workflows/${encodeURIComponent(name)}/runs/${encodeURIComponent(runId)}?projectId=${encodeURIComponent(projectId)}`,
          transformResponse: unwrapOne<WorkflowRun>('run'),
          providesTags: ['WorkflowRun'],
        },
      ),

      /** Add a member skill (or nested bundle) into a bundle. */
      addBundleMember: build.mutation<Skill, { name: string; scope: ScopeRef; member: string }>({
        query: ({ name, scope, member }) => ({
          url: `skills/${encodeURIComponent(name)}/members?${scopeQuery(scope)}`,
          method: 'POST',
          body: { member },
        }),
        transformResponse: unwrapOne<Skill>('skill'),
        invalidatesTags: ['Skill'],
      }),

      /** Remove/eject a member from a bundle (the member stays standalone). */
      removeBundleMember: build.mutation<Skill, { name: string; scope: ScopeRef; member: string }>(
        {
          query: ({ name, scope, member }) => ({
            url: `skills/${encodeURIComponent(name)}/members/${encodeURIComponent(member)}?${scopeQuery(scope)}`,
            method: 'DELETE',
          }),
          transformResponse: unwrapOne<Skill>('skill'),
          invalidatesTags: ['Skill'],
        },
      ),

      /** Dissolve a bundle: every member becomes standalone, the bundle is removed. */
      dissolveBundle: build.mutation<{ dissolved: boolean }, { name: string; scope: ScopeRef }>({
        query: ({ name, scope }) => ({
          url: `skills/${encodeURIComponent(name)}/dissolve?${scopeQuery(scope)}`,
          method: 'POST',
        }),
        invalidatesTags: ['Skill'],
      }),

      /** Add a member agent (or nested bundle) into an agent bundle. */
      addAgentBundleMember: build.mutation<Agent, { name: string; member: string }>({
        query: ({ name, member }) => ({
          url: `agents/${encodeURIComponent(name)}/members`,
          method: 'POST',
          body: { member },
        }),
        transformResponse: unwrapOne<Agent>('agent'),
        invalidatesTags: ['Agent'],
      }),

      /** Remove/eject a member from an agent bundle (the member stays standalone). */
      removeAgentBundleMember: build.mutation<Agent, { name: string; member: string }>({
        query: ({ name, member }) => ({
          url: `agents/${encodeURIComponent(name)}/members/${encodeURIComponent(member)}`,
          method: 'DELETE',
        }),
        transformResponse: unwrapOne<Agent>('agent'),
        invalidatesTags: ['Agent'],
      }),

      /** Dissolve an agent bundle: every member becomes standalone, the bundle is removed. */
      dissolveAgentBundle: build.mutation<{ dissolved: boolean }, { name: string }>({
        query: ({ name }) => ({
          url: `agents/${encodeURIComponent(name)}/dissolve`,
          method: 'POST',
        }),
        invalidatesTags: ['Agent'],
      }),

      // ---- Weekly commit lifecycle (U3/U4) ----
      // The chess-layer fields (`category`/`priorityNumeric`) are DERIVED
      // server-side (KTD4) — the client never authors them, so the create/update
      // bodies below carry only the editable planned/actual fields. Every write
      // invalidates `WeeklyCommit` (and `Weekly`) so the week view refetches.

      /**
       * Add a commit to a week's DRAFT plan (auto-created on first write). The
       * server derives `category`/`priorityNumeric`; the client sends the title,
       * the SO-or-orphan link (KTD10), and any informational `alsoAdvances`.
       */
      createCommit: build.mutation<
        WeeklyCommit,
        {
          projectId: string;
          isoWeek: string;
          title: string;
          supportingOutcomeId?: string;
          orphanReason?: OrphanReason;
          alsoAdvances?: string[];
        }
      >({
        query: ({ projectId, isoWeek, ...body }) => ({
          url: `projects/${projectId}/weekly/${isoWeek}/commits`,
          method: 'POST',
          body,
        }),
        transformResponse: unwrapOne<WeeklyCommit>('commit'),
        invalidatesTags: (_r, _e, { isoWeek }) => [
          'Weekly',
          'WeeklyCommit',
          { type: 'Weekly', id: isoWeek },
        ],
      }),

      /**
       * Edit a commit. During DRAFT this rewrites the planned fields (title, link,
       * `alsoAdvances`); during RECONCILING it records the actual `status` +
       * `actualOutcome`. The SO-or-orphan link is sent explicitly so an SO can be
       * cleared (the server re-derives the chess fields).
       */
      updateCommit: build.mutation<
        WeeklyCommit,
        {
          projectId: string;
          isoWeek: string;
          commitId: string;
          title?: string;
          supportingOutcomeId?: string | null;
          orphanReason?: OrphanReason | null;
          alsoAdvances?: string[];
          status?: WeeklyCommit['status'];
          actualOutcome?: string;
        }
      >({
        query: ({ projectId, isoWeek, commitId, ...body }) => ({
          url: `projects/${projectId}/weekly/${isoWeek}/commits/${commitId}`,
          method: 'PUT',
          body,
        }),
        transformResponse: unwrapOne<WeeklyCommit>('commit'),
        invalidatesTags: (_r, _e, { isoWeek }) => [
          'Weekly',
          'WeeklyCommit',
          { type: 'Weekly', id: isoWeek },
        ],
      }),

      /** Remove a commit from a week's DRAFT plan. */
      deleteCommit: build.mutation<
        { deleted: string },
        { projectId: string; isoWeek: string; commitId: string }
      >({
        query: ({ projectId, isoWeek, commitId }) => ({
          url: `projects/${projectId}/weekly/${isoWeek}/commits/${commitId}`,
          method: 'DELETE',
        }),
        invalidatesTags: (_r, _e, { isoWeek }) => [
          'Weekly',
          'WeeklyCommit',
          { type: 'Weekly', id: isoWeek },
        ],
      }),

      /**
       * DRAFT → LOCKED (the new "publish"). The server re-checks the SO-or-orphan
       * lock guard; a violation is a 409 carrying a structured `blockers[]` the
       * editor reads to self-correct, surfaced to the caller via `.unwrap()`.
       */
      lockWeek: build.mutation<{ plan: WeeklyPlan }, { projectId: string; isoWeek: string }>({
        query: ({ projectId, isoWeek }) => ({
          url: `projects/${projectId}/weekly/${isoWeek}/lock`,
          method: 'POST',
        }),
        invalidatesTags: (_r, _e, { isoWeek }) => [
          'Weekly',
          'WeeklyCommit',
          { type: 'Weekly', id: isoWeek },
        ],
      }),

      /** LOCKED → RECONCILING. Opens the per-commit actual-fields path. */
      startReconcile: build.mutation<{ plan: WeeklyPlan }, { projectId: string; isoWeek: string }>({
        query: ({ projectId, isoWeek }) => ({
          url: `projects/${projectId}/weekly/${isoWeek}/reconcile/start`,
          method: 'POST',
        }),
        invalidatesTags: (_r, _e, { isoWeek }) => [
          'Weekly',
          'WeeklyCommit',
          { type: 'Weekly', id: isoWeek },
        ],
      }),

      /**
       * RECONCILING → RECONCILED. On success the server carries incomplete commits
       * into next week's DRAFT and recomputes the single-source roll-up (KTD3/KTD5),
       * so this ALSO invalidates `Objective` — the Objectives screen's SO `pct`
       * moves to the just-reconciled value. The response carries the carry-forward
       * summary (`carriedTo`/`carriedCount`/`deepCarryNudge`).
       */
      completeReconcile: build.mutation<
        {
          plan: WeeklyPlan;
          carriedTo: string;
          carriedCount: number;
          deepCarryNudge?: { commitId: string; carryDepth: number }[];
        },
        { projectId: string; isoWeek: string }
      >({
        query: ({ projectId, isoWeek }) => ({
          url: `projects/${projectId}/weekly/${isoWeek}/reconcile/complete`,
          method: 'POST',
        }),
        invalidatesTags: (_r, _e, { isoWeek }) => [
          'Weekly',
          'WeeklyCommit',
          'Objective',
          { type: 'Weekly', id: isoWeek },
        ],
      }),

      /**
       * The reports-scoped manager exception/divergence brief (U8), keyset-paginated
       * over the caller's reports. A caller with no reports gets `{ reports: [] }`
       * ("nothing needs you"). `cursor` follows `nextCursor` for the next page.
       */
      getManagerBrief: build.query<ManagerBrief, { limit?: number; cursor?: string } | void>({
        query: (arg) => {
          const params = new URLSearchParams();
          if (arg && arg.limit !== undefined) params.set('limit', String(arg.limit));
          if (arg && arg.cursor !== undefined) params.set('cursor', arg.cursor);
          const qs = params.toString();
          return `weekly/manager${qs ? `?${qs}` : ''}`;
        },
        providesTags: ['Weekly', 'WeeklyCommit'],
      }),

      /**
       * Send a control frame (inject/pause/interrupt/shutdown/kill) down the
       * control gateway to a live session. The backend authorizes ownership then
       * routes the frame to the owning daemon's WebSocket connection. `shutdown`
       * terminates gracefully (force-fallback); `kill` terminates immediately.
       */
      sendControl: build.mutation<
        { ok: boolean },
        { sessionId: string; action: ControlAction; text?: string }
      >({
        query: ({ sessionId, action, text }) => ({
          url: `sessions/${sessionId}/control`,
          method: 'POST',
          body: { action, payload: text !== undefined ? { text } : {} },
        }),
        // Shut down / kill terminates the session, but the Sessions LIST page does
        // not subscribe to that session's live-WS events, so a status→done event
        // never reaches it and the row stayed live. Optimistically flip the row to
        // `done` across every cached sessions view + the single-session cache so
        // the UI reflects the action immediately; undo if the control call fails.
        // Also invalidate `Session` so the list reconciles with server truth once
        // the daemon has actually terminated.
        async onQueryStarted({ sessionId, action }, { dispatch, queryFulfilled }) {
          if (action !== 'shutdown' && action !== 'kill') return;
          const setDone = (s: SessionProjection) => {
            if (s.sessionId === sessionId) s.status = 'done';
          };
          const patches = [
            dispatch(
              baseApi.util.updateQueryData('getSession', sessionId, (draft) => {
                if (draft) draft.status = 'done';
              }),
            ),
            ...SESSION_LIST_ARGS.map((arg) =>
              dispatch(
                baseApi.util.updateQueryData('getSessions', arg, (draft) =>
                  draft.forEach(setDone),
                ),
              ),
            ),
          ];
          try {
            await queryFulfilled;
          } catch {
            patches.forEach((p) => p.undo());
          }
        },
        invalidatesTags: ['Session'],
      }),

      /**
       * Approve a pending claude+ device login: a signed-in HQ user submits the
       * user code shown in their terminal; the backend matches it to a pending
       * device request and marks it approved.
       */
      approveDevice: build.mutation<{ approved: boolean }, { userCode: string }>({
        query: ({ userCode }) => ({
          url: 'device/approve',
          method: 'POST',
          body: { userCode },
        }),
      }),
    };
  },
});

export const {
  useGetMeQuery,
  useCreateOrgMutation,
  useJoinOrgMutation,
  useSwitchOrgMutation,
  useGetProjectsQuery,
  useGetProjectQuery,
  useCreateProjectMutation,
  useRefreshProjectMutation,
  useDeleteProjectMutation,
  useGetSessionsQuery,
  useGetSessionQuery,
  useGetSessionEventsQuery,
  useGetObjectivesQuery,
  useCreateObjectiveMutation,
  useDeleteObjectiveMutation,
  useGetDodQuery,
  usePutDodMutation,
  useGetAgentsQuery,
  useGetWorkflowsQuery,
  useGetWorkflowQuery,
  useGetSkillsQuery,
  useGetSkillVariantsQuery,
  useGetSkillIdeasQuery,
  useGetUnassignedIdeasQuery,
  usePromoteSkillMutation,
  useGetMcpServersQuery,
  useGetMcpServerQuery,
  useGetWeeksQuery,
  useGetWeekQuery,
  useCreateCommitMutation,
  useUpdateCommitMutation,
  useDeleteCommitMutation,
  useLockWeekMutation,
  useStartReconcileMutation,
  useCompleteReconcileMutation,
  useGetManagerBriefQuery,
  useGetProjectDocsQuery,
  useGetProjectDocContentQuery,
  useGetProjectRequirementsQuery,
  useGetProjectWireframeQuery,
  useGetProjectLearningsQuery,
  useGetProjectMemoriesQuery,
  useSaveAgentMutation,
  useSaveWorkflowMutation,
  usePromoteWorkflowMutation,
  useSaveMcpServerMutation,
  useDeleteMcpServerMutation,
  useEnableProjectSkillMutation,
  useDisableProjectSkillMutation,
  useEnableProjectMcpServerMutation,
  useDisableProjectMcpServerMutation,
  useEnableProjectBundleMutation,
  useDisableProjectBundleMutation,
  useEnableProjectAgentMutation,
  useDisableProjectAgentMutation,
  useEnableProjectWorkflowMutation,
  useDisableProjectWorkflowMutation,
  useStartWorkflowRunMutation,
  useGetWorkflowRunQuery,
  useAddBundleMemberMutation,
  useRemoveBundleMemberMutation,
  useDissolveBundleMutation,
  useEnableProjectAgentBundleMutation,
  useDisableProjectAgentBundleMutation,
  useAddAgentBundleMemberMutation,
  useRemoveAgentBundleMemberMutation,
  useDissolveAgentBundleMutation,
  useSendControlMutation,
  useApproveDeviceMutation,
} = baseApi;
