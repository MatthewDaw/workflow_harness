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
  WeeklyUpdate,
  DefinitionOfDone,
  Envelope,
  LearningRecord,
} from '@harness/shared';
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
  mcpServers?: McpServer[];
  weekly?: Record<string, WeeklyUpdate[]>;
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
  /** Org-wide Definition of Done (plan-mapping feature 1). */
  dod?: DefinitionOfDone;
}

const MATT: AuthUser = { userId: 'user-matt', username: 'matt', org: 'acme' };

/** Default `GET /me` member so existing tests reach Objectives past the OrgGate. */
const DEFAULT_ME: MeResponse = { userId: 'dev', name: 'Dev', org: 'gmail.com', admin: true };

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
    if (path === 'sessions') return json(seed.sessions ?? []);

    // Project skill/agent opt-in: return the (seeded) project so the mutation
    // resolves; cache invalidation drives a refetch in tests.
    const projSkill = /^projects\/([^/]+)\/skills\/([^/]+)$/.exec(path);
    if (projSkill)
      return json({ project: (seed.projects ?? []).find((p) => p.id === projSkill[1]) ?? null });
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

    const weekly = /^projects\/([^/]+)\/weekly$/.exec(path);
    if (weekly) return json(seed.weekly?.[weekly[1]!] ?? []);

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
