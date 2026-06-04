import { createApi, fetchBaseQuery } from '@reduxjs/toolkit/query/react';
import type {
  Project,
  SessionProjection,
  ObjectiveNode,
  Agent,
  Skill,
  Priority,
  WeeklyUpdate,
  ScopeRef,
  ControlAction,
  DefinitionOfDone,
  Envelope,
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
    'Project',
    'Session',
    'Objective',
    'Agent',
    'Skill',
    'Weekly',
    'Docs',
    'Requirements',
    'Dod',
  ],
  endpoints: (build) => ({
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
      // The repo-sourced doc tree changes server-side (e.g. once GitHub reads
      // start succeeding); always refetch on mount so a stale empty cache from an
      // earlier failed read can't pin the page to "no requirement docs".
      refetchOnMountOrArgChange: true,
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
      refetchOnMountOrArgChange: true,
    }),

    // ---- Mutations (U22/U24/U25) ----

    /** Create or update an agent (the editor's Save & sync; server forces org scope). */
    saveAgent: build.mutation<Agent, Agent>({
      query: (agent) => ({ url: 'agents', method: 'POST', body: agent }),
      transformResponse: unwrapOne<Agent>('agent'),
      invalidatesTags: ['Agent'],
    }),

    // ---- Project opt-in (collapsed org-catalog model) ----

    /** Add a skill to a project's enabledSkills (idempotent). */
    enableProjectSkill: build.mutation<Project, { projectId: string; skillName: string }>({
      query: ({ projectId, skillName }) => ({
        url: `projects/${projectId}/skills/${encodeURIComponent(skillName)}`,
        method: 'POST',
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
            dispatch(baseApi.util.updateQueryData('getSessions', arg, (draft) => draft.forEach(setDone))),
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
  useGetWeeklyQuery,
  useGetProjectDocsQuery,
  useGetProjectDocContentQuery,
  useGetProjectRequirementsQuery,
  useSaveAgentMutation,
  useEnableProjectSkillMutation,
  useDisableProjectSkillMutation,
  useEnableProjectAgentMutation,
  useDisableProjectAgentMutation,
  useAddBundleMemberMutation,
  useRemoveBundleMemberMutation,
  useDissolveBundleMutation,
  usePutWeeklyMutation,
  usePublishWeeklyMutation,
  useSendControlMutation,
  useApproveDeviceMutation,
} = baseApi;
