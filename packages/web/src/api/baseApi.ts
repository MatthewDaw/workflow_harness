import { createApi, fetchBaseQuery } from '@reduxjs/toolkit/query/react';
import type {
  Project,
  SessionProjection,
  ObjectiveNode,
  Agent,
  Skill,
  McpServer,
  Priority,
  WeeklyUpdate,
  ScopeRef,
  ControlAction,
  DefinitionOfDone,
  Envelope,
  LearningRecord,
  LearningStream,
} from '@harness/shared';

/**
 * RTK Query base API for Command HQ (U19, KTD12). One slice with tagged
 * endpoints mirroring the backend REST surface (U8–U11). All DTOs come from
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

/** Where the live middleware writes session projections. */
export const SESSIONS_CACHE_ARG = 'all' as const;

/**
 * Two-tier requirements DTOs (U10/U11). The backend serves these from the
 * `/projects/:id/docs*` and `/projects/:id/requirements` endpoints; they are
 * declared here (rather than in @harness/shared) because the requirements UI is
 * the sole consumer.
 */

/**
 * The signed-in principal as the backend resolves it (`GET /me`). `org` is the
 * caller's REAL membership from their profile record — null when they have not
 * joined/created an org yet — so the OrgGate can force onboarding. Declared here
 * (rather than @harness/shared) because the auth gate is the only consumer.
 */
export interface Me {
  userId: string;
  name?: string;
  /** The ACTIVE org. null = no membership yet → OrgGate forces create/join. */
  org: string | null;
  admin?: boolean;
  /** Every org the user belongs to, for the header switcher. */
  orgs?: string[];
}

/** A node in the project's detailed-requirements doc tree (U11). */
export interface ProjectDoc {
  /** Repo-relative path, e.g. `docs/requirements/auth.md`. */
  path: string;
  /** Human title (heading or filename). */
  title: string;
  /** GitHub-sourced completion 0–100 parsed from the doc's `completion:` front-matter. */
  completion: number;
}

/** Markdown body of a single detailed-requirements doc (U11). */
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
 * A single Claude Code "memory" synced up from claude+ (per user). Mirrors the
 * shared `Memory` DTO; declared locally (like ProjectDoc above) because the
 * Memories tab is the only consumer. `type` is the loose string form of the
 * shared MemoryType enum so the UI can render an unknown future type as-is.
 */
export interface Memory {
  projectId: string;
  userId: string;
  userName?: string;
  name: string;
  description?: string;
  type?: string;
  content: string;
  updatedAt: number;
}

/**
 * Catalog versioning (SHARED CONTRACTS #1 / plan KTD6). A catalog item is
 * identified by `(baseName, repoId, userId)`; editing a skill from a project
 * forks/updates that variant. Every variant carries an immutable revision
 * (`version`/`rev`). One ORG-WIDE "true" variant per `baseName` is the default
 * shown in the catalog and added to a project.
 *
 * These fields are surfaced on the catalog DTOs (`Skill`/`Agent`/`McpServer`)
 * by the schema area (U-Ver-Schema). Until that lands they may be absent on a
 * given record, so the web reads them defensively via `variantOf()` below —
 * legacy records resolve to their own `name` as the base variant at rev 1.
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
    'Skill',
    'McpServer',
    'Variant',
    'Weekly',
    'Docs',
    'Requirements',
    'Dod',
    'Learnings',
    'Memory',
  ],
  endpoints: (build) => ({
    /**
     * The signed-in principal and their REAL org membership (org-onboarding).
     * Unlike the data handlers, /me never falls back to the token claim, so a
     * user with no profile.org reads back `org: null` and the OrgGate forces
     * them to create or join an org before the app mounts.
     */
    getMe: build.query<Me, void>({
      query: () => 'me',
      // The /me response is a bare object; unwrapOne keeps us tolerant of a
      // future wrapped shape ({ me: {...} }) without a contract break.
      transformResponse: unwrapOne<Me>('me'),
      providesTags: ['Me'],
    }),

    /**
     * Create a brand-new org (org-onboarding). The creator becomes its admin and
     * their profile.org is set server-side. Switching org swaps EVERY org-scoped
     * dataset, so onQueryStarted resets the whole cache (see joinOrg) after the
     * write so the app reloads under the new org.
     */
    createOrg: build.mutation<{ org: string; admin: boolean }, { name: string; password: string }>({
      query: (body) => ({ url: 'orgs', method: 'POST', body }),
      invalidatesTags: ['Me'],
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        // A failed create (409/400) leaves the caller in their prior (no-)org, so
        // there is nothing to reset; swallow the rejection here — the component's
        // own .unwrap() catch surfaces the error to the user.
        try {
          await queryFulfilled;
        } catch {
          return;
        }
        // Everything the old org cached is now wrong; drop it so each query
        // refetches under the membership we just established.
        dispatch(
          baseApi.util.invalidateTags([
            'Me',
            'Objective',
            'Agent',
            'Skill',
            'Project',
            'Session',
            'Weekly',
            'Dod',
          ]),
        );
      },
    }),

    /**
     * Join an EXISTING org by exact name + password (org-onboarding). On success
     * the caller's profile.org is set; like createOrg we reset every org-scoped
     * dataset so the app re-reads under the joined org.
     */
    joinOrg: build.mutation<{ org: string; admin: boolean }, { name: string; password: string }>({
      query: (body) => ({ url: 'orgs/join', method: 'POST', body }),
      invalidatesTags: ['Me'],
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        // A failed join (403/400) means no membership changed; swallow it (the
        // component's .unwrap() catch shows the generic error) and skip the reset.
        try {
          await queryFulfilled;
        } catch {
          return;
        }
        dispatch(
          baseApi.util.invalidateTags([
            'Me',
            'Objective',
            'Agent',
            'Skill',
            'Project',
            'Session',
            'Weekly',
            'Dod',
          ]),
        );
      },
    }),

    /**
     * Switch the ACTIVE org to another one the user has already joined (no
     * password). Switching swaps EVERY org-scoped dataset, so on success we reset
     * the whole API cache — a clean wipe is the most reliable way to guarantee no
     * stale tenant data lingers — and /me + all screens re-read under the new org.
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
     * Connect a GitHub repo as a new project (the Projects "+ Connect repo"
     * button). The caller supplies `{ id, name, repo }`; the backend stamps the
     * owner from the auth principal — a client-supplied owner is never trusted.
     */
    createProject: build.mutation<Project, { id: string; name: string; repo: string }>({
      query: (body) => ({ url: 'projects', method: 'POST', body }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: ['Project'],
    }),

    /**
     * Re-read a project's framing from GitHub (`completion:` %, PRD goal, owned
     * Supporting Outcomes) and store it. Fired right after connect so a new card
     * shows real progress; GitHub being unreachable degrades to stale, never errors.
     */
    refreshProject: build.mutation<Project, string>({
      query: (id) => ({ url: `projects/${id}/refresh`, method: 'POST' }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, id) => ['Project', { type: 'Project', id }],
    }),

    /**
     * Remove a project from HQ (and everything under it: sessions, instances,
     * weekly, framing) plus its repo pointer. The GitHub repo is untouched — this
     * only forgets the project, so it can be reconnected. Owner-or-admin gated
     * server-side. Invalidates the project list so the card disappears.
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
     * Backfill the stored event history for a session (the last N events).
     * The same `GET /sessions/:id` endpoint returns `{ session, events }`; this
     * query reads the `events` array so the Watch & steer feed shows history
     * immediately on open instead of "waiting for activity…". The live-WS stream
     * carries everything thereafter; the LiveWatch view merges the two by `seq`.
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
     * Create or update a Company Objective node (admin-gated on the backend).
     * HQ owns the RCDO tree, so the editor posts `{id?, level, title, parentId?}`;
     * the backend stamps the caller's org. Title edits reuse this with the
     * node's existing id.
     */
    createObjective: build.mutation<
      ObjectiveNode,
      { id?: string; level: ObjectiveNode['level']; title: string; parentId?: string }
    >({
      query: (body) => ({ url: 'objectives', method: 'POST', body }),
      transformResponse: unwrapOne<ObjectiveNode>('node'),
      invalidatesTags: ['Objective'],
    }),

    /** Delete a Company Objective node by id (admin-gated on the backend). */
    deleteObjective: build.mutation<{ deleted: boolean }, string>({
      query: (id) => ({ url: `objectives/${encodeURIComponent(id)}`, method: 'DELETE' }),
      invalidatesTags: ['Objective'],
    }),

    /**
     * The org-wide Definition of Done (plan-mapping feature 1). Advisory config
     * declaring what `/update-progress` verifies before work is "done". The
     * backend serves the floor default when unset, so this never errors empty.
     */
    getDod: build.query<DefinitionOfDone, void>({
      query: () => 'dod',
      transformResponse: unwrapOne<DefinitionOfDone>('dod'),
      providesTags: ['Dod'],
    }),

    /** Set the org-wide Definition of Done (admin-gated on the backend). */
    putDod: build.mutation<DefinitionOfDone, DefinitionOfDone>({
      query: (dod) => ({ url: 'dod', method: 'PUT', body: dod }),
      transformResponse: unwrapOne<DefinitionOfDone>('dod'),
      invalidatesTags: ['Dod'],
    }),

    /** Org skill/agent catalog (collapsed model): no projectId, org scope only. */
    getAgents: build.query<Agent[], void>({
      query: () => 'agents',
      transformResponse: unwrapArray<Agent>('agents'),
      providesTags: ['Agent'],
    }),
    getSkills: build.query<Skill[], void>({
      query: () => 'skills',
      transformResponse: unwrapArray<Skill>('skills'),
      providesTags: ['Skill'],
    }),

    /**
     * Every variant/revision of one skill name (catalog versioning, KTD6). The
     * catalog's per-name variant switcher reads this to let any org member pick
     * another `(repo, person)` fork or an earlier revision; the current TRUE
     * variant is flagged so the UI can default to it. Tagged by name so a
     * promote/edit of that name refetches just its variant list.
     */
    getSkillVariants: build.query<SkillVariant[], string>({
      query: (name) => `skills/${encodeURIComponent(name)}/variants`,
      transformResponse: unwrapArray<SkillVariant>('variants'),
      providesTags: (_r, _e, name) => [{ type: 'Variant', id: name }],
    }),

    /**
     * Promote a variant to the org-wide TRUE version for its name (KTD6). Any
     * authed org member may promote — it is NOT admin-gated — and promotion only
     * repoints the TRUE pointer; it never edits or deletes a variant. Pass an
     * optional `rev` to pin a specific revision (defaults to the variant's
     * current revision). Invalidates the catalog + that name's variant list so
     * the new TRUE shows as the default everywhere.
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

    getWeekly: build.query<WeeklyUpdate[], string>({
      query: (projectId) => `projects/${projectId}/weekly`,
      transformResponse: unwrapArray<WeeklyUpdate>('weeks'),
      providesTags: ['Weekly'],
    }),

    // ---- Two-tier requirements (U10/U11) ----

    /** Detailed-requirements doc tree for a project (repo-sourced, U11). */
    getProjectDocs: build.query<ProjectDoc[], string>({
      query: (projectId) => `projects/${projectId}/docs`,
      transformResponse: unwrapArray<ProjectDoc>('docs'),
      providesTags: (_r, _e, id) => [{ type: 'Docs', id }],
    }),

    /** Markdown body of a single detailed-requirements doc (U11). */
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
     * The project's mined learnings (topic-focus logging), read from the
     * backend's `/projects/:id/learnings` endpoint. An optional `stream`
     * (`impl`|`doc`) filters server-side; omitted returns both streams.
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
     * The project's per-user Claude Code memories, synced up from claude+ as
     * Claude saves them. The flat list spans every author; the Memories tab
     * groups it by `userId` and lets you filter to a single author.
     */
    getProjectMemories: build.query<Memory[], string>({
      query: (projectId) => `projects/${projectId}/memories`,
      transformResponse: unwrapArray<Memory>('memories'),
      providesTags: (_r, _e, id) => [{ type: 'Memory', id }],
    }),

    // ---- Mutations (U22/U24/U25) ----

    /** Create or update an agent (the editor's Save & sync; server forces org scope). */
    saveAgent: build.mutation<Agent, Agent>({
      query: (agent) => ({ url: 'agents', method: 'POST', body: agent }),
      transformResponse: unwrapOne<Agent>('agent'),
      invalidatesTags: ['Agent'],
    }),

    /**
     * Create or update an MCP server (the editor's Save; server forces org scope).
     * Mirrors saveAgent — a single POST handles both create and edit.
     */
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

    /**
     * Add a skill to a project's enabledSkills (idempotent). The enabled-set
     * entry carries the chosen variant `{ name, variantId }` (KTD6): the optional
     * `variantId` defaults server-side to the name's current TRUE variant, and a
     * per-repo dropdown can pin another variant. The name stays in the URL (so
     * the handler + existing opt-in tests are unchanged) and the variant rides in
     * the body when supplied.
     */
    enableProjectSkill: build.mutation<
      Project,
      { projectId: string; skillName: string; variantId?: string }
    >({
      query: ({ projectId, skillName, variantId }) => ({
        url: `projects/${projectId}/skills/${encodeURIComponent(skillName)}`,
        method: 'POST',
        ...(variantId !== undefined ? { body: { variantId } } : {}),
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
    }),

    /** Remove a skill from a project's enabledSkills. */
    disableProjectSkill: build.mutation<Project, { projectId: string; skillName: string }>({
      query: ({ projectId, skillName }) => ({
        url: `projects/${projectId}/skills/${encodeURIComponent(skillName)}`,
        method: 'DELETE',
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
    }),

    /** Add an MCP server to a project's enabledMcpServers (idempotent). */
    enableProjectMcpServer: build.mutation<Project, { projectId: string; name: string }>({
      query: ({ projectId, name }) => ({
        url: `projects/${projectId}/mcp-servers/${encodeURIComponent(name)}`,
        method: 'POST',
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
    }),

    /** Remove an MCP server from a project's enabledMcpServers. */
    disableProjectMcpServer: build.mutation<Project, { projectId: string; name: string }>({
      query: ({ projectId, name }) => ({
        url: `projects/${projectId}/mcp-servers/${encodeURIComponent(name)}`,
        method: 'DELETE',
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
    }),

    /**
     * Add a whole bundle to a project. The server records the bundle name in
     * enabledBundles (the INTENT annotation, so the UI can show whole-bundle vs
     * individually-picked members) AND unions the bundle's member skills into
     * enabledSkills (the flat set the daemon actually materializes).
     */
    enableProjectBundle: build.mutation<Project, { projectId: string; bundleName: string }>({
      query: ({ projectId, bundleName }) => ({
        url: `projects/${projectId}/bundles/${encodeURIComponent(bundleName)}`,
        method: 'POST',
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
    }),

    /**
     * Remove a whole bundle from a project. The server drops the bundle from
     * enabledBundles and strips the member skills it contributed to enabledSkills.
     */
    disableProjectBundle: build.mutation<Project, { projectId: string; bundleName: string }>({
      query: ({ projectId, bundleName }) => ({
        url: `projects/${projectId}/bundles/${encodeURIComponent(bundleName)}`,
        method: 'DELETE',
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
    }),

    /**
     * Add an agent to a project's enabledAgents; the server also unions the
     * agent's skills (bundles flattened) into enabledSkills.
     */
    enableProjectAgent: build.mutation<Project, { projectId: string; agentName: string }>({
      query: ({ projectId, agentName }) => ({
        url: `projects/${projectId}/agents/${encodeURIComponent(agentName)}`,
        method: 'POST',
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
    }),

    /** Remove an agent from enabledAgents (does NOT prune enabledSkills). */
    disableProjectAgent: build.mutation<Project, { projectId: string; agentName: string }>({
      query: ({ projectId, agentName }) => ({
        url: `projects/${projectId}/agents/${encodeURIComponent(agentName)}`,
        method: 'DELETE',
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
    }),

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
    removeBundleMember: build.mutation<Skill, { name: string; scope: ScopeRef; member: string }>({
      query: ({ name, scope, member }) => ({
        url: `skills/${encodeURIComponent(name)}/members/${encodeURIComponent(member)}?${scopeQuery(scope)}`,
        method: 'DELETE',
      }),
      transformResponse: unwrapOne<Skill>('skill'),
      invalidatesTags: ['Skill'],
    }),

    /** Dissolve a bundle: every member becomes standalone, the bundle is removed. */
    dissolveBundle: build.mutation<{ dissolved: boolean }, { name: string; scope: ScopeRef }>({
      query: ({ name, scope }) => ({
        url: `skills/${encodeURIComponent(name)}/dissolve?${scopeQuery(scope)}`,
        method: 'POST',
      }),
      invalidatesTags: ['Skill'],
    }),

    /**
     * Add a whole AGENT bundle to a project. The server records the bundle in
     * enabledAgentBundles (the INTENT annotation) AND unions its member agents
     * into enabledAgents (plus those agents' skills/MCP servers). Mirrors
     * enableProjectBundle for skills.
     */
    enableProjectAgentBundle: build.mutation<Project, { projectId: string; bundleName: string }>({
      query: ({ projectId, bundleName }) => ({
        url: `projects/${projectId}/agent-bundles/${encodeURIComponent(bundleName)}`,
        method: 'POST',
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
    }),

    /**
     * Remove a whole AGENT bundle from a project. The server drops it from
     * enabledAgentBundles and strips the member agents it contributed from
     * enabledAgents (members shared with another enabled bundle survive).
     */
    disableProjectAgentBundle: build.mutation<Project, { projectId: string; bundleName: string }>({
      query: ({ projectId, bundleName }) => ({
        url: `projects/${projectId}/agent-bundles/${encodeURIComponent(bundleName)}`,
        method: 'DELETE',
      }),
      transformResponse: unwrapOne<Project>('project'),
      invalidatesTags: (_r, _e, { projectId }) => ['Project', { type: 'Project', id: projectId }],
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

    /** Store a weekly-update draft (free-form done/plan prose) for an iso week. */
    putWeekly: build.mutation<
      WeeklyUpdate,
      {
        projectId: string;
        isoWeek: string;
        done: string;
        plan: string;
        conformityScore?: number;
        validated?: boolean;
      }
    >({
      query: ({ projectId, isoWeek, ...body }) => ({
        url: `projects/${projectId}/weekly/${isoWeek}`,
        method: 'PUT',
        body,
      }),
      transformResponse: unwrapOne<WeeklyUpdate>('update'),
      invalidatesTags: ['Weekly'],
    }),

    /** Publish a weekly update: mark validated + feed the objective roll-up. */
    publishWeekly: build.mutation<WeeklyUpdate, { projectId: string; isoWeek: string }>({
      query: ({ projectId, isoWeek }) => ({
        url: `projects/${projectId}/weekly/${isoWeek}/publish`,
        method: 'POST',
      }),
      transformResponse: unwrapOne<WeeklyUpdate>('update'),
      invalidatesTags: ['Weekly', 'Objective'],
    }),

    /**
     * Send a control frame (inject/pause/interrupt/shutdown/kill) down the
     * control gateway (U7) to a live session. The backend authorizes ownership
     * then routes the frame to the owning daemon's WebSocket connection.
     * `shutdown` terminates the session gracefully (force-fallback); `kill`
     * terminates immediately.
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
          ...([undefined, { live: true }, { live: false }] as const).map((arg) =>
            dispatch(
              baseApi.util.updateQueryData('getSessions', arg, (draft) => draft.forEach(setDone)),
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
     * Approve a pending claude+ device login (device-auth flow). A signed-in HQ
     * user submits the user code shown in their terminal; the backend matches it
     * to a pending device request and marks it approved.
     */
    approveDevice: build.mutation<{ approved: boolean }, { userCode: string }>({
      query: ({ userCode }) => ({
        url: 'device/approve',
        method: 'POST',
        body: { userCode },
      }),
    }),
  }),
});

export type { Priority };

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
  useGetSkillsQuery,
  useGetSkillVariantsQuery,
  usePromoteSkillMutation,
  useGetMcpServersQuery,
  useGetMcpServerQuery,
  useGetWeeklyQuery,
  useGetProjectDocsQuery,
  useGetProjectDocContentQuery,
  useGetProjectRequirementsQuery,
  useGetProjectWireframeQuery,
  useGetProjectLearningsQuery,
  useGetProjectMemoriesQuery,
  useSaveAgentMutation,
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
  useAddBundleMemberMutation,
  useRemoveBundleMemberMutation,
  useDissolveBundleMutation,
  useEnableProjectAgentBundleMutation,
  useDisableProjectAgentBundleMutation,
  useAddAgentBundleMemberMutation,
  useRemoveAgentBundleMemberMutation,
  useDissolveAgentBundleMutation,
  usePutWeeklyMutation,
  usePublishWeeklyMutation,
  useSendControlMutation,
  useApproveDeviceMutation,
} = baseApi;
