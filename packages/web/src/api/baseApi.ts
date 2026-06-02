import { createApi, fetchBaseQuery } from '@reduxjs/toolkit/query/react';
import type {
  Project,
  SessionProjection,
  ObjectiveNode,
  Agent,
  Skill,
  Ticket,
  WeeklyUpdate,
} from '@harness/shared';

/**
 * RTK Query base API for Command HQ (U19, KTD12). One slice with tagged
 * endpoints mirroring the backend REST surface (U8–U11). All DTOs come from
 * @harness/shared so the web stays in lockstep with the contract.
 *
 * The base URL comes from VITE_API_BASE_URL; the live-WS middleware
 * (src/ws/liveMiddleware.ts) pushes session events straight into this cache so
 * subscribed components update without refetching.
 */

/** Where the live middleware writes session projections. */
export const SESSIONS_CACHE_ARG = 'all' as const;

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
  tagTypes: ['Project', 'Session', 'Objective', 'Agent', 'Skill', 'Ticket', 'Weekly'],
  endpoints: (build) => ({
    getProjects: build.query<Project[], void>({
      query: () => 'projects',
      providesTags: ['Project'],
    }),
    getProject: build.query<Project, string>({
      query: (id) => `projects/${id}`,
      providesTags: (_r, _e, id) => [{ type: 'Project', id }],
    }),

    getSessions: build.query<SessionProjection[], { live?: boolean } | void>({
      query: (arg) => (arg && arg.live ? 'sessions?live=true' : 'sessions'),
      providesTags: ['Session'],
    }),
    getSession: build.query<SessionProjection, string>({
      query: (id) => `sessions/${id}`,
      providesTags: (_r, _e, id) => [{ type: 'Session', id }],
    }),

    getObjectives: build.query<ObjectiveNode[], void>({
      query: () => 'objectives',
      providesTags: ['Objective'],
    }),

    getAgents: build.query<Agent[], { projectId?: string } | void>({
      query: (arg) => (arg && arg.projectId ? `agents?project=${arg.projectId}` : 'agents'),
      providesTags: ['Agent'],
    }),
    getSkills: build.query<Skill[], { projectId?: string } | void>({
      query: (arg) => (arg && arg.projectId ? `skills?project=${arg.projectId}` : 'skills'),
      providesTags: ['Skill'],
    }),

    getTickets: build.query<Ticket[], string>({
      query: (projectId) => `projects/${projectId}/tickets`,
      providesTags: ['Ticket'],
    }),

    getWeekly: build.query<WeeklyUpdate[], string>({
      query: (projectId) => `projects/${projectId}/weekly`,
      providesTags: ['Weekly'],
    }),
  }),
});

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
} = baseApi;
