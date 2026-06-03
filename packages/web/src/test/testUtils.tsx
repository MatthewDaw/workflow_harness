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
  WeeklyUpdate,
} from '@harness/shared';
import { makeStore, type AppStore } from '../app/store.js';
import { AuthProvider } from '../auth/AuthProvider.js';
import { createMockClient } from '../auth/mockClient.js';
import type { AuthUser } from '../auth/authClient.js';

/**
 * Seed data the fake REST API serves. The fixture fetch below maps URL paths to
 * these arrays so RTK Query endpoints resolve real DTOs in tests — no network.
 */
export interface SeedData {
  projects?: Project[];
  sessions?: SessionProjection[];
  objectives?: ObjectiveNode[];
  agents?: Agent[];
  skills?: Skill[];
  weekly?: Record<string, WeeklyUpdate[]>;
  /** Detailed-requirements doc tree, keyed by projectId (U11). */
  docs?: Record<string, { path: string; title: string; completion: number }[]>;
  /** Doc markdown bodies, keyed by `${projectId}::${repoRelPath}` (U11). */
  docContent?: Record<string, string>;
  /** HQ-owned high-level requirements markdown, keyed by projectId (U10). */
  requirements?: Record<string, string>;
}

const MATT: AuthUser = { userId: 'user-matt', username: 'matt', org: 'acme' };

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

  const handler = (input: RequestInfo | URL): Response => {
    const url = typeof input === 'string' ? input : (input as { url: string }).url;
    const afterApi = url.replace(/^.*\/api\/?/, '');
    const [pathPart, queryPart = ''] = afterApi.split('?');
    const path = pathPart ?? '';
    const query = new URLSearchParams(queryPart);
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });

    if (path === 'projects') return json(seed.projects ?? []);
    if (path === 'objectives') return json(seed.objectives ?? []);
    if (path === 'agents') return json(seed.agents ?? []);
    if (path === 'skills') return json(seed.skills ?? []);
    if (path === 'sessions') return json(seed.sessions ?? []);

    const proj = /^projects\/([^/]+)$/.exec(path);
    if (proj) return json((seed.projects ?? []).find((p) => p.id === proj[1]) ?? null);

    if (/^sessions\/[^/]+\/control$/.test(path)) return json({ ok: true });

    const sess = /^sessions\/([^/]+)$/.exec(path);
    if (sess) return json((seed.sessions ?? []).find((s) => s.sessionId === sess[1]) ?? null);

    const weekly = /^projects\/([^/]+)\/weekly$/.exec(path);
    if (weekly) return json(seed.weekly?.[weekly[1]!] ?? []);

    const docContent = /^projects\/([^/]+)\/docs\/content$/.exec(path);
    if (docContent) {
      const docPath = query.get('path') ?? '';
      const markdown = seed.docContent?.[`${docContent[1]}::${docPath}`] ?? '';
      return json({ path: docPath, markdown });
    }

    const docs = /^projects\/([^/]+)\/docs$/.exec(path);
    if (docs) return json(seed.docs?.[docs[1]!] ?? []);

    const requirements = /^projects\/([^/]+)\/requirements$/.exec(path);
    if (requirements) return json({ markdown: seed.requirements?.[requirements[1]!] ?? '' });

    return json([]);
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => handler(input)),
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
