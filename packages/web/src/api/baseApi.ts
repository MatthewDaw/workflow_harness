import { createApi, fetchBaseQuery } from '@reduxjs/toolkit/query/react';
import type {
  Project,
  SessionProjection,
  ObjectiveNode,
  Agent,
  Skill,
  Ticket,
  TicketStatus,
  Priority,
  WeeklyUpdate,
  WeeklyItem,
  ScopeRef,
  ControlAction,
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

/** HQ-owned high-level requirements markdown for a project (U10). */
export interface ProjectRequirements {
  markdown: string;
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
    'Ticket',
    'Weekly',
    'Docs',
    'Requirements',
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

    getObjectives: build.query<ObjectiveNode[], void>({
      query: () => 'objectives',
      transformResponse: unwrapArray<ObjectiveNode>('nodes'),
      providesTags: ['Objective'],
    }),

    getAgents: build.query<Agent[], { projectId?: string } | void>({
      query: (arg) => (arg && arg.projectId ? `agents?project=${arg.projectId}` : 'agents'),
      transformResponse: unwrapArray<Agent>('agents'),
      providesTags: ['Agent'],
    }),
    getSkills: build.query<Skill[], { projectId?: string } | void>({
      query: (arg) => (arg && arg.projectId ? `skills?project=${arg.projectId}` : 'skills'),
      transformResponse: unwrapArray<Skill>('skills'),
      providesTags: ['Skill'],
    }),

    getTickets: build.query<Ticket[], string>({
      query: (projectId) => `projects/${projectId}/tickets`,
      transformResponse: unwrapArray<Ticket>('tickets'),
      providesTags: ['Ticket'],
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

    /** HQ-owned high-level requirements markdown for a project (U10, read). */
    getProjectRequirements: build.query<ProjectRequirements, string>({
      query: (projectId) => `projects/${projectId}/requirements`,
      transformResponse: unwrapOne<ProjectRequirements>('requirements'),
      providesTags: (_r, _e, id) => [{ type: 'Requirements', id }],
    }),

    /** HQ-owned high-level requirements markdown for a project (U10, write). */
    putProjectRequirements: build.mutation<
      ProjectRequirements,
      { projectId: string; markdown: string }
    >({
      query: ({ projectId, markdown }) => ({
        url: `projects/${projectId}/requirements`,
        method: 'PUT',
        body: { markdown },
      }),
      transformResponse: unwrapOne<ProjectRequirements>('requirements'),
      invalidatesTags: (_r, _e, { projectId }) => [{ type: 'Requirements', id: projectId }],
    }),

    // ---- Mutations (U22/U24/U25) ----

    /** Transition a ticket's lifecycle status (board moves, in-review, etc.). */
    transitionTicket: build.mutation<
      Ticket,
      { projectId: string; ticketId: string; status: TicketStatus; sessionId?: string }
    >({
      query: ({ projectId, ticketId, ...body }) => ({
        url: `projects/${projectId}/tickets/${ticketId}/status`,
        method: 'POST',
        body,
      }),
      transformResponse: unwrapOne<Ticket>('ticket'),
      invalidatesTags: ['Ticket'],
    }),

    /** Update ticket fields (priority, title, agent, …). */
    updateTicket: build.mutation<
      Ticket,
      { projectId: string; ticketId: string } & Partial<Pick<Ticket, 'priority' | 'title' | 'description'>>
    >({
      query: ({ projectId, ticketId, ...body }) => ({
        url: `projects/${projectId}/tickets/${ticketId}`,
        method: 'PUT',
        body,
      }),
      transformResponse: unwrapOne<Ticket>('ticket'),
      invalidatesTags: ['Ticket'],
    }),

    /** Elevate/demote an agent: rewrite its scope key (project ↔ user ↔ org). */
    changeAgentScope: build.mutation<Agent, { name: string; from: ScopeRef; to: ScopeRef }>({
      query: ({ name, from, to }) => ({
        url: `agents/${encodeURIComponent(name)}/scope?${scopeQuery(from)}`,
        method: 'POST',
        body: { scope: to },
      }),
      transformResponse: unwrapOne<Agent>('agent'),
      invalidatesTags: ['Agent'],
    }),

    /** Create or update an agent (the editor's Save & sync). */
    saveAgent: build.mutation<Agent, Agent>({
      query: (agent) => ({ url: 'agents', method: 'POST', body: agent }),
      transformResponse: unwrapOne<Agent>('agent'),
      invalidatesTags: ['Agent'],
    }),

    /** Elevate/demote a skill (or bundle) to a new scope. */
    changeSkillScope: build.mutation<Skill, { name: string; from: ScopeRef; to: ScopeRef }>({
      query: ({ name, from, to }) => ({
        url: `skills/${encodeURIComponent(name)}/scope?${scopeQuery(from)}`,
        method: 'POST',
        body: { scope: to },
      }),
      transformResponse: unwrapOne<Skill>('skill'),
      invalidatesTags: ['Skill'],
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

    /** Store a weekly-update draft (done[]/plan[]) for a given iso week. */
    putWeekly: build.mutation<
      WeeklyUpdate,
      { projectId: string; isoWeek: string; done: WeeklyItem[]; plan: WeeklyItem[]; validated?: boolean }
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
     * Send a control frame (inject/pause/interrupt) down the control gateway
     * (U7) to a live session. The backend authorizes ownership then routes the
     * frame to the owning daemon's WebSocket connection.
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
    }),
  }),
});

export type { Priority };

export const {
  useGetProjectsQuery,
  useGetProjectQuery,
  useGetSessionsQuery,
  useGetSessionQuery,
  useGetObjectivesQuery,
  useGetAgentsQuery,
  useGetSkillsQuery,
  useGetTicketsQuery,
  useGetWeeklyQuery,
  useGetProjectDocsQuery,
  useGetProjectDocContentQuery,
  useGetProjectRequirementsQuery,
  usePutProjectRequirementsMutation,
  useTransitionTicketMutation,
  useUpdateTicketMutation,
  useChangeAgentScopeMutation,
  useSaveAgentMutation,
  useChangeSkillScopeMutation,
  useAddBundleMemberMutation,
  useRemoveBundleMemberMutation,
  useDissolveBundleMutation,
  usePutWeeklyMutation,
  usePublishWeeklyMutation,
  useSendControlMutation,
} = baseApi;
