/**
 * learningApi — RTK Query slice for the Python learning read API (A1 / MAT-154).
 *
 * The boundary table (§Language & service boundary) places the learning read API
 * in Python and has the TS web read via that API (JSON-DTO contract), NOT
 * directly from DynamoDB.  This module is the TS side of that contract.
 *
 * Base URL: ``VITE_PYTHON_LEARNING_READ_URL`` (env var).  When the var is absent
 * (local dev / unit tests), the slice falls back to the TS backend's ideas
 * endpoints — so the existing dev-server routes in ``devServer.ts`` still work
 * and tests that stub ``fetch`` need only stub ``/api/...`` paths.
 *
 * JSON-DTO contract (the Python side defines the shapes; this file declares the
 * TS types that must match):
 *
 *   candidate-learnings response:  { learnings: LearningIdeaDto[] }
 *   all-ideas response:            { ideas: AllIdeaDto[] }
 *
 * Contract test: ``packages/web/src/test/learningReadApi.test.ts``
 * Python side:   ``packages/learning-service/tests/test_a1_learning_read_api.py``
 */

import { createApi, fetchBaseQuery } from '@reduxjs/toolkit/query/react';

// ---------------------------------------------------------------------------
// JSON-DTO types (the TS side of the contract)
// These types mirror the Python ``_idea_to_candidate_dto`` and
// ``_idea_to_all_ideas_dto`` serialisers in ``learning_reads.py``.
// ---------------------------------------------------------------------------

/**
 * Candidate-learning DTO — the subset of fields a working session is allowed
 * to see.  Deliberately excludes ``invalidAt`` (a history/HQ-only field).
 *
 * The Python side:  ``learning_service.entrypoints.learning_reads._idea_to_candidate_dto``
 * Contract test:    ``test_candidate_learnings_never_exposes_invalid_at``
 */
export interface LearningIdeaDto {
  ideaId: string;
  skillBaseName: string;
  body: string;
  status: 'open' | 'folded';
  corroborationCount: number;
  foldedIntoRev: number | null;
  authored: boolean | null;
  authorityKind: 'user_directive' | 'authored_import' | 'merged' | null;
}

/**
 * All-ideas DTO — every idea for a skill, including history/supersession fields.
 * Used by the HQ dropdown (the admin surface that shows all proposals).
 *
 * The Python side:  ``learning_service.entrypoints.learning_reads._idea_to_all_ideas_dto``
 */
export interface AllIdeaDto extends LearningIdeaDto {
  /** Epoch-ms of temporal retirement (null = currently active). */
  invalidAt: number | null;
  /** ideaId of the idea that superseded this one, if any. */
  supersededBy: string | null;
  /** ideaIds this idea supersedes. */
  supersedes: string[];
  /** ideaId this idea refines (scoped-nuance coexistence edge). */
  refines: string | null;
}

// ---------------------------------------------------------------------------
// RTK Query slice
// ---------------------------------------------------------------------------

/**
 * Resolve the base URL for the Python learning read API.
 *
 * In production (deployed Lambda): ``VITE_PYTHON_LEARNING_READ_URL`` points at
 * the Python service.  In local dev / tests: falls back to the TS backend (the
 * ``/api`` prefix the dev-server and test-stub both honour).
 */
function pythonLearningReadBaseUrl(): string {
  const envUrl = import.meta.env.VITE_PYTHON_LEARNING_READ_URL;
  if (typeof envUrl === 'string' && envUrl.trim()) {
    return envUrl.trim().replace(/\/$/, '');
  }
  // Fallback: the TS backend serves the same routes in dev/tests.
  return import.meta.env.VITE_API_BASE_URL ?? '/api';
}

/**
 * RTK Query API slice for learning reads.  Kept separate from ``baseApi`` so:
 *  1. The base URL is independently configurable (Python service vs TS backend).
 *  2. Tests that stub ``fetch`` can distinguish learning-API calls from the main
 *     API calls by URL prefix.
 *  3. Future auth (learning-service-specific tokens, org headers) can be added
 *     here without touching the main API slice.
 */
export const learningApi = createApi({
  reducerPath: 'learningApi',
  baseQuery: fetchBaseQuery({
    baseUrl: pythonLearningReadBaseUrl(),
    prepareHeaders: (headers, { getState }) => {
      // Forward the bearer token so the Python endpoint can resolve the org
      // (same pattern as baseApi).
      const token = (getState() as { auth?: { idToken?: string | null } }).auth?.idToken;
      if (token) headers.set('authorization', `Bearer ${token}`);
      return headers;
    },
  }),
  tagTypes: ['LearningIdea'],
  endpoints: (build) => ({
    /**
     * GET /skills/:name/all-ideas (Python API)
     *
     * Returns EVERY idea for a skill — open, folded, and optionally retired
     * (``?history=true``).  This is the HQ dropdown surface: no corroboration
     * gate, no cap, all proposals visible.
     *
     * Replaces the TS backend's ``GET /skills/:name/ideas`` for the learning-data
     * display (the repoint mandated by the boundary table).
     */
    getSkillIdeasFromPython: build.query<AllIdeaDto[], string>({
      query: (skillName) => `skills/${encodeURIComponent(skillName)}/all-ideas`,
      transformResponse: (resp: unknown): AllIdeaDto[] => {
        if (Array.isArray(resp)) return resp as AllIdeaDto[];
        const wrapped = (resp as Record<string, unknown>)?.ideas;
        return Array.isArray(wrapped) ? (wrapped as AllIdeaDto[]) : [];
      },
      providesTags: (_r, _e, name) => [{ type: 'LearningIdea', id: name }],
    }),

    /**
     * GET /skills/:name/candidate-learnings (Python API)
     *
     * Returns the corroborated, capped, current-set-only candidate learnings for
     * a skill.  This is the security-gated surface: only corroborated ideas, no
     * retired ideas, capped at 5 — the set a working session is allowed to see.
     */
    getCandidateLearnings: build.query<LearningIdeaDto[], string>({
      query: (skillName) => `skills/${encodeURIComponent(skillName)}/candidate-learnings`,
      transformResponse: (resp: unknown): LearningIdeaDto[] => {
        if (Array.isArray(resp)) return resp as LearningIdeaDto[];
        const wrapped = (resp as Record<string, unknown>)?.learnings;
        return Array.isArray(wrapped) ? (wrapped as LearningIdeaDto[]) : [];
      },
      providesTags: (_r, _e, name) => [{ type: 'LearningIdea', id: `candidates:${name}` }],
    }),
  }),
});

export const {
  useGetSkillIdeasFromPythonQuery,
  useGetCandidateLearningsQuery,
} = learningApi;

// ---------------------------------------------------------------------------
// Re-export the AllIdeaDto as SkillIdea so existing callers of SkillCard /
// SkillIdeasDropdown can import it from this module without touching baseApi.
// ---------------------------------------------------------------------------
export type { AllIdeaDto as PythonSkillIdea };
