import { describe, it, expect, afterEach, vi } from 'vitest';
import { baseApi } from './baseApi.js';
import { makeStore } from '../app/store.js';
import { installFetchStub, lastMatching } from '../test/testUtils.js';

/**
 * RTK Query wiring for the weekly-commit lifecycle (U9). The endpoints
 * themselves are exercised end-to-end by the screen tests (U10–U12); here we pin
 * the two cache-invalidation contracts the plan calls out explicitly:
 *
 *  - a commit write (`createCommit`) invalidates `WeeklyCommit`, so a subscribed
 *    `getWeeks` REFETCHES (the week view must reflect the new commit), and
 *  - `completeReconcile` invalidates BOTH `WeeklyCommit` AND `Objective`, so a
 *    subscribed `getObjectives` refetches (the SO `pct` moved to the reconciled
 *    value — KTD5).
 *
 * "Refetched" is asserted by counting GETs to the relevant URL through the
 * shared fetch stub: a successful invalidation triggers a second GET.
 */

/** Count the stubbed GET calls whose URL matches `pred`. */
function getCount(pred: (url: string) => boolean): number {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  return calls.filter((c) => {
    const req = c[0] as { url: string; method: string };
    return req.method === 'GET' && pred(req.url);
  }).length;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('baseApi — weekly lifecycle cache invalidation (U9)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('createCommit invalidates WeeklyCommit so a subscribed getWeeks refetches', async () => {
    installFetchStub({});
    const store = makeStore();

    // Subscribe to the week list so an invalidation forces a real refetch.
    const sub = store.dispatch(baseApi.endpoints.getWeeks.initiate('p1'));
    await sub;
    const before = getCount((u) => /projects\/p1\/weekly$/.test(u));
    expect(before).toBe(1);

    await store
      .dispatch(
        baseApi.endpoints.createCommit.initiate({
          projectId: 'p1',
          isoWeek: '2026-W23',
          title: 'Wire the gate',
          supportingOutcomeId: 'so-1',
        }),
      )
      .unwrap();
    await flush();

    // The invalidation refetched the subscribed week list.
    expect(getCount((u) => /projects\/p1\/weekly$/.test(u))).toBeGreaterThan(before);
    sub.unsubscribe();
  });

  it('completeReconcile invalidates Objective so a subscribed getObjectives refetches', async () => {
    installFetchStub({});
    const store = makeStore();

    const sub = store.dispatch(baseApi.endpoints.getObjectives.initiate());
    await sub;
    const before = getCount((u) => /objectives$/.test(u));
    expect(before).toBe(1);

    await store
      .dispatch(
        baseApi.endpoints.completeReconcile.initiate({ projectId: 'p1', isoWeek: '2026-W23' }),
      )
      .unwrap();
    await flush();

    // The reconcile recomputed the roll-up, so the objectives list refetched.
    expect(getCount((u) => /objectives$/.test(u))).toBeGreaterThan(before);
    // And it hit the complete-reconcile endpoint.
    expect(
      lastMatching((u, m) => m === 'POST' && /reconcile\/complete$/.test(u)),
    ).toBeDefined();
    sub.unsubscribe();
  });
});
