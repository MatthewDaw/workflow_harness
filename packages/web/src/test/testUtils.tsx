import { vi } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { Provider } from 'react-redux';
import type { ReactElement, ReactNode } from 'react';
import { render } from '@testing-library/react';
import type {
  Project,
  SessionProjection,
  ObjectiveNode,
  Agent,
  Skill,
  McpServer,
  DefinitionOfDone,
  Envelope,
  LearningRecord,
  Memory,
} from '@harness/shared';
import type { WeekView, ManagerBrief } from '../api/baseApi.js';
import { makeStore, type AppStore } from '../app/store.js';
import { AuthProvider } from '../auth/AuthProvider.js';
import { createMockClient } from '../auth/mockClient.js';
import type { AuthUser } from '../auth/authClient.js';

/** The `GET /me` body the stub serves. `org: null` forces the OrgGate. */
export interface MeResponse {
  userId: string;
  name?: string;
  org: string | null;
  admin?: boolean;
  /** Every org the user belongs to (header switcher). */
  orgs?: string[];
}

/**
 * Seed data the fake REST API serves. The fixture fetch below maps URL paths to
 * these arrays so RTK Query endpoints resolve real DTOs in tests — no network.
 */
export interface SeedData {
  /**
   * The `GET /me` response (org-onboarding). Defaults to a MEMBER so existing
   * gate tests sail through the OrgGate to Objectives. Pass a fixed object to
   * force onboarding (`{ org: null }`), or a function to script a sequence —
   * e.g. return null until createOrg is called, then a member — so a test can
   * watch the gate flip. May return a partial; missing fields are defaulted.
   */
  me?: Partial<MeResponse> | (() => Partial<MeResponse>);
  /**
   * Override responses for arbitrary `METHOD path` keys (e.g. 'POST orgs',
   * 'POST orgs/join'). The value is the JSON body to serve (status 200) or a
   * `{ status, body }` pair to drive error paths (403/409/400). A function is
   * called per request so tests can sequence/branch responses.
   */
  routes?: Record<string, RouteResponse>;
  projects?: Project[];
  sessions?: SessionProjection[];
  /** Stored event history per session, keyed by sessionId, served as backfill. */
  sessionEvents?: Record<string, Envelope[]>;
  objectives?: ObjectiveNode[];
  agents?: Agent[];
  skills?: Skill[];
  /**
   * Per-name skill variant/revision lists (catalog versioning, KTD6), keyed by
   * skill name. Served by `GET /skills/:name/variants` so the variant switcher +
   * promote UI resolve real rows in tests.
   */
  skillVariants?: Record<string, unknown[]>;
  /**
   * Per-name skill idea lists (skill-idea loop, U13/U14), keyed by skill name.
   * Served by `GET /skills/:name/ideas` so the HQ ideas dropdown resolves real
   * rows in tests. Each entry is an `Idea` decorated with `corroborationCount`.
   */
  skillIdeas?: Record<string, unknown[]>;
  /**
   * The org's unassigned bin (skill-idea loop, U15). Served by
   * `GET /ideas/unassigned` so the bin backlog screen resolves real rows in
   * tests. Each entry is an `UnassignedEntry` decorated with `frequency`.
   */
  bin?: unknown[];
  mcpServers?: McpServer[];
  /**
   * Per-project weekly lifecycle data (U3), keyed by projectId — each entry is the
   * `{ plan, commits }` week view the `GET …/weekly` (list) and `…/weekly/:week`
   * (single) endpoints serve. The legacy prose `WeeklyUpdate[]` shape is gone.
   */
  weekly?: Record<string, WeekView[]>;
  /** The reports-scoped manager brief (U8), served by `GET /weekly/manager`. */
  managerBrief?: ManagerBrief;
  /** Detailed-requirements doc tree, keyed by projectId (U11). */
  docs?: Record<string, { path: string; title: string; completion: number }[]>;
  /** Doc markdown bodies, keyed by `${projectId}::${repoRelPath}` (U11). */
  docContent?: Record<string, string>;
  /** HQ-owned high-level requirements markdown, keyed by projectId (U10). */
  requirements?: Record<string, string>;
  /** Example wireframe HTML from docs/wireframe.html, keyed by projectId. */
  wireframe?: Record<string, string>;
  /** Mined topic-focus learnings, keyed by projectId (topic-focus logging). */
  learnings?: Record<string, LearningRecord[]>;
  /** Per-user Claude Code memories synced from claude+, keyed by projectId. */
  memories?: Record<string, Memory[]>;
  /** Org-wide Definition of Done (plan-mapping feature 1). */
  dod?: DefinitionOfDone;
}

const MATT: AuthUser = { userId: 'user-matt', username: 'matt', org: 'acme' };

/** Default `GET /me` member so existing tests reach Objectives past the OrgGate. */
const DEFAULT_ME: MeResponse = { userId: 'dev', name: 'Dev', org: 'gmail.com', admin: true };

/** The org scope catalog fixtures live in — matches MATT's org. */
export const ORG = { tier: 'org', id: 'acme' } as const;

/** A connected project with every opt-in set empty; override what the test exercises. */
export function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'weekly-compass',
    name: 'weekly-compass',
    repo: 'gh/acme/weekly-compass',
    ownerUserId: 'user-matt',
    progressPct: 0,
    liveSessionCount: 0,
    enabledSkills: [],
    enabledBundles: [],
    enabledAgents: [],
    enabledWorkflows: [],
    enabledAgentBundles: [],
    enabledMcpServers: [],
    ...overrides,
  };
}

export function makeSkill(overrides: Partial<Skill> = {}): Skill {
  return {
    name: 'gh',
    scope: ORG,
    kind: 'skill',
    description: '',
    source: 'local',
    members: [],
    body: '',
    ...overrides,
  };
}

export function makeAgent(overrides: Partial<Agent> = {}): Agent {
  return {
    name: 'builder',
    scope: ORG,
    kind: 'agent',
    description: '',
    model: 'claude-sonnet-4',
    prompt: '',
    skills: [],
    tools: [],
    mcpServers: [],
    members: [],
    ...overrides,
  };
}

/** A local stdio server; tests needing http/sse keep explicit literals. */
export function makeMcpServer(
  overrides: Partial<Extract<McpServer, { transport: 'stdio' }>> = {},
): McpServer {
  return {
    name: 'fs',
    scope: ORG,
    transport: 'stdio',
    command: 'npx',
    args: [],
    env: {},
    ...overrides,
  };
}

/**
 * A `routes` override value: either a raw JSON body (served 200), or a
 * `{ status, body }` pair to drive non-200 paths. Wrapping in a function lets a
 * test return different responses on successive calls.
 */
export type RouteResponse =
  | unknown
  | { status: number; body: unknown }
  | (() => unknown | { status: number; body: unknown });

/** Install a fetch stub that answers the base API routes from `seed`. */
export function installFetchStub(seed: SeedData) {
  // fetchBaseQuery constructs `new Request(url, { signal })`; under jsdom the
  // cross-realm AbortSignal is rejected by Node's Request. Replace Request with
  // a permissive shim that just records the url, so the fetch path works and our
  // stub below answers from `seed`.
  class StubRequest {
    url: string;
    method: string;
    body: unknown;
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      this.url =
        typeof input === 'string' ? input : String((input as { url?: string }).url ?? input);
      // Preserve method/body so mutation tests can assert what RTK Query sent
      // (fetchBaseQuery builds `new Request(url, init)` then calls fetch(request)).
      const fromInput = typeof input === 'object' ? (input as Partial<RequestInit>) : {};
      this.method = (init?.method ?? fromInput.method ?? 'GET').toUpperCase();
      this.body = init?.body ?? fromInput.body;
    }
  }
  vi.stubGlobal('Request', StubRequest);

  const handler = (input: RequestInfo | URL, method: string): Response => {
    const url = typeof input === 'string' ? input : (input as { url: string }).url;
    const afterApi = url.replace(/^.*\/api\/?/, '');
    const [pathPart, queryPart = ''] = afterApi.split('?');
    const path = pathPart ?? '';
    const query = new URLSearchParams(queryPart);
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'content-type': 'application/json' },
      });

    // Per-test route overrides (org-onboarding): keyed `METHOD path`. Resolved
    // first so a test can answer POST orgs / POST orgs/join with success or an
    // error body, and can pass a function to sequence responses.
    const override = seed.routes?.[`${method} ${path}`];
    if (override !== undefined) {
      const resolved = typeof override === 'function' ? (override as () => unknown)() : override;
      if (resolved && typeof resolved === 'object' && 'status' in resolved && 'body' in resolved) {
        const { status, body } = resolved as { status: number; body: unknown };
        return json(body, status);
      }
      return json(resolved);
    }

    // The signed-in principal + real org membership (org-onboarding). Defaults to
    // a member so the OrgGate passes through; tests pass `me` to force onboarding
    // or to script null→member as the gate flips after create/join.
    if (path === 'me') {
      const raw = typeof seed.me === 'function' ? seed.me() : seed.me;
      return json({ ...DEFAULT_ME, ...(raw ?? {}) });
    }

    if (path === 'projects') return json(seed.projects ?? []);
    if (path === 'objectives') return json(seed.objectives ?? []);
    if (path === 'dod')
      return json({ dod: seed.dod ?? { requiresUnitTests: true, requiresProdE2E: false } });
    if (path === 'agents') return json(seed.agents ?? []);
    if (path === 'skills') return json(seed.skills ?? []);
    if (path === 'mcp-servers') return json(seed.mcpServers ?? []);

    // Catalog versioning (KTD6): a name's variant/revision list + the (not
    // admin-gated) promote endpoint. Matched before the project opt-in routes
    // below so `skills/<name>/variants` doesn't fall through to a 200 [].
    const skillVariants = /^skills\/([^/]+)\/variants$/.exec(path);
    if (skillVariants) return json({ variants: seed.skillVariants?.[skillVariants[1]!] ?? [] });
    // All-ideas read for the HQ ideas dropdown (skill-idea loop, U13/U14). Matched
    // before the project opt-in routes so it doesn't fall through to a 200 [].
    const skillIdeas = /^skills\/([^/]+)\/ideas$/.exec(path);
    if (skillIdeas) return json({ ideas: seed.skillIdeas?.[skillIdeas[1]!] ?? [] });
    const skillPromote = /^skills\/([^/]+)\/promote$/.exec(path);
    if (skillPromote) return json({ name: skillPromote[1], variantId: '', rev: 1 });
    // The org's unassigned bin (skill-idea loop, U15): the new-skill backlog.
    if (path === 'ideas/unassigned') return json({ entries: seed.bin ?? [] });
    if (path === 'sessions') return json(seed.sessions ?? []);

    // Project skill/agent opt-in: return the (seeded) project so the mutation
    // resolves; cache invalidation drives a refetch in tests.
    const projSkill = /^projects\/([^/]+)\/skills\/([^/]+)$/.exec(path);
    if (projSkill)
      return json({ project: (seed.projects ?? []).find((p) => p.id === projSkill[1]) ?? null });
    const projBundle = /^projects\/([^/]+)\/bundles\/([^/]+)$/.exec(path);
    if (projBundle)
      return json({ project: (seed.projects ?? []).find((p) => p.id === projBundle[1]) ?? null });
    const projAgent = /^projects\/([^/]+)\/agents\/([^/]+)$/.exec(path);
    if (projAgent)
      return json({ project: (seed.projects ?? []).find((p) => p.id === projAgent[1]) ?? null });
    const projMcp = /^projects\/([^/]+)\/mcp-servers\/([^/]+)$/.exec(path);
    if (projMcp)
      return json({ project: (seed.projects ?? []).find((p) => p.id === projMcp[1]) ?? null });

    const proj = /^projects\/([^/]+)$/.exec(path);
    if (proj) return json((seed.projects ?? []).find((p) => p.id === proj[1]) ?? null);

    if (/^sessions\/[^/]+\/control$/.test(path)) return json({ ok: true });

    if (path === 'device/approve') return json({ approved: true });

    const sess = /^sessions\/([^/]+)$/.exec(path);
    if (sess) {
      const id = sess[1]!;
      const session = (seed.sessions ?? []).find((s) => s.sessionId === id) ?? null;
      // Mirror the real GET /sessions/:id, which returns { session, events }.
      // getSession reads `session`; getSessionEvents reads `events` (backfill).
      return json({ session, events: seed.sessionEvents?.[id] ?? [] });
    }

    // The reports-scoped manager brief (U8). Served bare (the endpoint returns it
    // directly), defaulting to the empty "nothing needs you" brief.
    if (path === 'weekly/manager') return json(seed.managerBrief ?? { reports: [] });

    // Weekly commit lifecycle (U3/U4). The list read returns `{ weeks }`; a single
    // week returns `{ week }` (404 when absent). The commit writes + transitions
    // echo back a minimal success body so the mutations resolve in tests (the
    // cache invalidation, not the echoed row, drives a refetch).
    const weekCommit = /^projects\/([^/]+)\/weekly\/([^/]+)\/commits(?:\/([^/]+))?$/.exec(path);
    if (weekCommit) {
      if (method === 'DELETE') return json({ deleted: weekCommit[3] ?? '' });
      return json({ commit: { id: weekCommit[3] ?? 'commit-new' } });
    }
    if (/^projects\/[^/]+\/weekly\/[^/]+\/lock$/.test(path)) return json({ plan: {} });
    if (/^projects\/[^/]+\/weekly\/[^/]+\/reconcile\/start$/.test(path)) return json({ plan: {} });
    if (/^projects\/[^/]+\/weekly\/[^/]+\/reconcile\/complete$/.test(path))
      return json({ plan: {}, carriedTo: '', carriedCount: 0 });

    const week = /^projects\/([^/]+)\/weekly\/([^/]+)$/.exec(path);
    if (week) {
      const found = (seed.weekly?.[week[1]!] ?? []).find((w) => w.plan.isoWeek === week[2]);
      if (!found) return json({ error: 'not found' }, 404);
      return json({ week: found });
    }

    const weekly = /^projects\/([^/]+)\/weekly$/.exec(path);
    if (weekly) return json({ weeks: seed.weekly?.[weekly[1]!] ?? [] });

    const docContent = /^projects\/([^/]+)\/docs\/content$/.exec(path);
    if (docContent) {
      const docPath = query.get('path') ?? '';
      const markdown = seed.docContent?.[`${docContent[1]}::${docPath}`] ?? '';
      return json({ path: docPath, markdown });
    }

    const learnings = /^projects\/([^/]+)\/learnings$/.exec(path);
    if (learnings) {
      const all = seed.learnings?.[learnings[1]!] ?? [];
      const stream = query.get('stream');
      // Mirror the backend's optional `?stream=impl|doc` server-side filter.
      return json(stream ? all.filter((l) => l.stream === stream) : all);
    }

    const docs = /^projects\/([^/]+)\/docs$/.exec(path);
    if (docs) return json(seed.docs?.[docs[1]!] ?? []);

    const memories = /^projects\/([^/]+)\/memories$/.exec(path);
    if (memories) return json(seed.memories?.[memories[1]!] ?? []);

    const requirements = /^projects\/([^/]+)\/requirements$/.exec(path);
    if (requirements) return json({ markdown: seed.requirements?.[requirements[1]!] ?? '' });

    const wireframe = /^projects\/([^/]+)\/wireframe$/.exec(path);
    if (wireframe) return json({ html: seed.wireframe?.[wireframe[1]!] ?? '' });

    return json([]);
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // fetchBaseQuery calls fetch(request); the method lives on the Request
      // (our StubRequest preserves it) — fall back to init.method / GET.
      const method = ((input as { method?: string }).method ?? init?.method ?? 'GET').toUpperCase();
      return handler(input, method);
    }),
  );
}

/**
 * The shape `installFetchStub`'s StubRequest records for each fetch call —
 * what mutation tests assert against to see what RTK Query actually sent.
 */
export interface StubReq {
  url: string;
  method: string;
  body?: unknown;
}

/** The most recent stubbed fetch call matching `pred` (URL + method). */
export function lastMatching(pred: (u: string, m: string) => boolean): StubReq | undefined {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const req = calls[i]![0] as StubReq;
    if (pred(req.url, req.method)) return req;
  }
  return undefined;
}

export interface RenderOptions {
  route?: string;
  /** Route pattern to bind params (e.g. '/sessions/:sessionId'). Defaults to `route`. */
  routePath?: string;
  seed?: SeedData;
  /** When false, render unauthenticated (login gate visible). */
  authenticated?: boolean;
  store?: AppStore;
}

/** Render an arbitrary subtree with store + auth + router wired for tests. */
export function renderWithProviders(
  ui: ReactElement,
  { route = '/', routePath, seed = {}, authenticated = true, store }: RenderOptions = {},
) {
  installFetchStub(seed);
  const appStore = store ?? makeStore();
  const client = createMockClient(authenticated ? MATT : null);

  const wrapper = ({ children }: { children: ReactNode }) => (
    <Provider store={appStore}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[route]}>
          <Routes>
            <Route path={routePath ?? route} element={children} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </Provider>
  );

  return { store: appStore, ...render(ui, { wrapper }) };
}
