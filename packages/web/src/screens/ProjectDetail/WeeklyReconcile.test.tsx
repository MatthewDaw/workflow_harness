import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ObjectiveNode, WeeklyCommit, WeeklyPlan } from '@harness/shared';
import type { WeekView } from '../../api/baseApi.js';
import { WeeklyReconcile } from './WeeklyReconcile.js';
import { lastMatching, makeProject, renderWithProviders } from '../../test/testUtils.js';

const PROJECT = makeProject({ progressPct: 62 });

/** One Supporting Outcome the frozen-plan column resolves a name for. */
const OBJECTIVES: ObjectiveNode[] = [
  { id: 'rc1', org: 'acme', level: 'rally_cry', title: 'Win the quarter' },
  {
    id: 'so-1',
    org: 'acme',
    level: 'supporting_outcome',
    title: 'Ship the login gate',
    parentId: 'rc1',
  },
];

function commit(overrides: Partial<WeeklyCommit> = {}): WeeklyCommit {
  return {
    id: 'c1',
    projectId: 'weekly-compass',
    isoWeek: '2026-W23',
    title: 'Wire the login gate',
    supportingOutcomeId: 'so-1',
    alsoAdvances: [],
    category: 'Delivery',
    priorityNumeric: 1,
    status: 'planned',
    carryDepth: 0,
    ...overrides,
  };
}

function week(plan: Partial<WeeklyPlan>, commits: WeeklyCommit[]): WeekView {
  return {
    plan: {
      projectId: 'weekly-compass',
      isoWeek: '2026-W23',
      status: 'RECONCILING',
      posture: 'focus',
      ...plan,
    },
    commits,
  };
}

function renderReconcile(
  weekly: WeekView[],
  routes?: Record<string, unknown>,
  objectives: ObjectiveNode[] = OBJECTIVES,
) {
  return renderWithProviders(<WeeklyReconcile />, {
    route: '/projects/weekly-compass/weekly/reconcile',
    routePath: '/projects/:projectId/weekly/reconcile',
    seed: {
      projects: [PROJECT],
      objectives,
      weekly: { 'weekly-compass': weekly },
      ...(routes ? { routes } : {}),
    },
  });
}

describe('WeeklyReconcile — planned-vs-actual (U11)', () => {
  afterEach(() => vi.unstubAllGlobals());

  // ---- Happy path: all terminal → complete enabled → carry-forward + depth nudge

  it('enables complete when all commits are terminal and shows the carry-forward summary + depth nudge', async () => {
    renderReconcile(
      [
        week({ status: 'RECONCILING' }, [
          commit({ id: 'c1', title: 'Shipped item', status: 'done', priorityNumeric: 5 }),
          commit({ id: 'c2', title: 'Half item', status: 'partial', priorityNumeric: 2 }),
        ]),
      ],
      {
        'POST projects/weekly-compass/weekly/2026-W23/reconcile/complete': {
          plan: { status: 'RECONCILED' },
          carriedTo: '2026-W24',
          carriedCount: 1,
          deepCarryNudge: [{ commitId: 'c2', carryDepth: 3 }],
        },
      },
    );

    await screen.findByText('Shipped item');
    const complete = screen.getByTestId('complete-reconcile');
    expect(complete).not.toBeDisabled();

    await userEvent.click(complete);

    await waitFor(() => expect(screen.getByTestId('carry-summary')).toBeInTheDocument());
    expect(screen.getByTestId('carry-count')).toHaveTextContent('1 item carried to 2026-W24');
    expect(screen.getByTestId('deep-carry-nudge')).toHaveTextContent('carry depth ≥ 3');

    // The transition actually fired.
    expect(
      lastMatching(
        (u, m) =>
          m === 'POST' &&
          u.includes('projects/weekly-compass/weekly/2026-W23/reconcile/complete'),
      ),
    ).toBeDefined();
  });

  // ---- Edge: complete disabled while any commit is still planned

  it('disables complete while any commit is still planned, with an inline reason', async () => {
    renderReconcile([
      week({ status: 'RECONCILING' }, [
        commit({ id: 'c1', title: 'Done item', status: 'done' }),
        commit({ id: 'c2', title: 'Untouched item', status: 'planned' }),
      ]),
    ]);

    await screen.findByText('Untouched item');
    expect(screen.getByTestId('complete-reconcile')).toBeDisabled();
    expect(screen.getByTestId('complete-blocked')).toHaveTextContent('1 item still planned');
  });

  // ---- Edge: a done commit shows no carry; a partial is flagged carrying

  it('settles a done commit and flags a partial commit as carrying', async () => {
    renderReconcile([
      week({ status: 'RECONCILING' }, [
        commit({ id: 'c1', title: 'Done item', status: 'done' }),
        commit({ id: 'c2', title: 'Partial item', status: 'partial' }),
      ]),
    ]);

    await screen.findByText('Done item');
    expect(screen.getByTestId('reconcile-settled-c1')).toBeInTheDocument();
    expect(screen.queryByTestId('reconcile-carry-c1')).not.toBeInTheDocument();

    expect(screen.getByTestId('reconcile-carry-c2')).toHaveTextContent('carries to next week');
    expect(screen.queryByTestId('reconcile-settled-c2')).not.toBeInTheDocument();
  });

  // ---- Actual-fields path: setting an outcome status saves via updateCommit

  it('saves the actual status via the updateCommit actual-fields path', async () => {
    renderReconcile([week({ status: 'RECONCILING' }, [commit({ id: 'c1', status: 'planned' })])]);

    await screen.findByText('Wire the login gate');
    await userEvent.selectOptions(screen.getByTestId('reconcile-status-c1'), 'done');

    await waitFor(() => {
      const req = lastMatching(
        (u, m) =>
          m === 'PUT' &&
          u.includes('projects/weekly-compass/weekly/2026-W23/commits/c1'),
      );
      expect(req).toBeDefined();
      expect(JSON.parse(String(req!.body)).status).toBe('done');
    });
  });

  it('saves the actual outcome prose on blur via updateCommit', async () => {
    renderReconcile([week({ status: 'RECONCILING' }, [commit({ id: 'c1', status: 'done' })])]);

    await screen.findByText('Wire the login gate');
    const box = screen.getByTestId('reconcile-outcome-c1');
    await userEvent.type(box, 'Landed behind a flag');
    await userEvent.tab();

    await waitFor(() => {
      const req = lastMatching(
        (u, m) =>
          m === 'PUT' &&
          u.includes('projects/weekly-compass/weekly/2026-W23/commits/c1'),
      );
      expect(req).toBeDefined();
      expect(JSON.parse(String(req!.body)).actualOutcome).toBe('Landed behind a flag');
    });
  });

  // ---- Start path: a LOCKED week offers to start reconciliation

  it('starts reconciliation on a LOCKED week', async () => {
    renderReconcile([week({ status: 'LOCKED' }, [commit()])]);

    await screen.findByTestId('start-reconcile');
    expect(screen.queryByTestId('complete-reconcile')).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId('start-reconcile'));
    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) =>
            m === 'POST' &&
            u.includes('projects/weekly-compass/weekly/2026-W23/reconcile/start'),
        ),
      ).toBeDefined(),
    );
  });

  // ---- Integration: complete invalidates Objective (RTK tag) so the SO % refetches

  it('refetches objectives after a successful complete (Objective tag invalidation)', async () => {
    renderReconcile(
      [week({ status: 'RECONCILING' }, [commit({ id: 'c1', status: 'done' })])],
      {
        'POST projects/weekly-compass/weekly/2026-W23/reconcile/complete': {
          plan: { status: 'RECONCILED' },
          carriedTo: '2026-W24',
          carriedCount: 0,
        },
      },
    );

    await screen.findByText('Wire the login gate');
    await userEvent.click(screen.getByTestId('complete-reconcile'));

    // The completeReconcile mutation invalidates the `Objective` tag; assert a
    // GET /objectives fires after the POST completes (the cache refetch).
    await waitFor(() => {
      const objCalls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => {
        const req = c[0] as { url: string; method: string };
        return req.method === 'GET' && /\/objectives$/.test(req.url);
      });
      // One initial load + one post-invalidation refetch.
      expect(objCalls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it('shows the empty state when there is no week to reconcile', async () => {
    renderReconcile([]);
    expect(await screen.findByText('No week to reconcile')).toBeInTheDocument();
  });
});
