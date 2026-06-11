import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ObjectiveNode, WeeklyCommit, WeeklyPlan } from '@harness/shared';
import type { WeekView } from '../../api/baseApi.js';
import { ProjectWeekly } from './ProjectWeekly.js';
import { lastMatching, makeProject, renderWithProviders } from '../../test/testUtils.js';

const PROJECT = makeProject({ progressPct: 62 });

/** Two Supporting Outcomes the SO-or-orphan picker lists (only this level). */
const OBJECTIVES: ObjectiveNode[] = [
  { id: 'rc1', org: 'acme', level: 'rally_cry', title: 'Win the quarter' },
  {
    id: 'so-1',
    org: 'acme',
    level: 'supporting_outcome',
    title: 'Ship the login gate',
    parentId: 'rc1',
  },
  {
    id: 'so-2',
    org: 'acme',
    level: 'supporting_outcome',
    title: 'Harden the API',
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
      status: 'DRAFT',
      posture: 'focus',
      ...plan,
    },
    commits,
  };
}

function renderWeekly(weekly: WeekView[], objectives: ObjectiveNode[] = OBJECTIVES) {
  return renderWithProviders(<ProjectWeekly />, {
    route: '/projects/weekly-compass/weekly',
    routePath: '/projects/:projectId/weekly',
    seed: { projects: [PROJECT], weekly: { 'weekly-compass': weekly }, objectives },
  });
}

describe('ProjectWeekly — DRAFT editor (U10)', () => {
  afterEach(() => vi.unstubAllGlobals());

  // ---- Read-render behaviors carried over from the U9 scaffold ----

  it('renders the latest week with its lifecycle status and declared posture', async () => {
    renderWeekly([week({ isoWeek: '2026-W23', status: 'LOCKED', posture: 'explore' }, [commit()])]);
    expect(await screen.findByText('Week 2026-W23')).toBeInTheDocument();
    expect(screen.getByText('locked')).toBeInTheDocument();
    expect(screen.getByTestId('week-posture')).toHaveTextContent('explore');
  });

  it('shows the LATEST week even when the list arrives oldest-first', async () => {
    renderWeekly([
      week({ isoWeek: '2026-W20', status: 'RECONCILED' }, [
        commit({ id: 'old', isoWeek: '2026-W20', title: 'Older week commit' }),
      ]),
      week({ isoWeek: '2026-W23', status: 'DRAFT' }, [
        commit({ id: 'new', isoWeek: '2026-W23', title: 'Newer week commit' }),
      ]),
    ]);
    expect(await screen.findByText('Week 2026-W23')).toBeInTheDocument();
    expect(screen.getByText('Newer week commit')).toBeInTheDocument();
    expect(screen.queryByText('Older week commit')).not.toBeInTheDocument();
  });

  it('shows the empty state when there are no weeks', async () => {
    renderWeekly([]);
    expect(await screen.findByText('No weekly update yet')).toBeInTheDocument();
  });

  // ---- U10: add a commit with an SO → derived category/priority render; lock enabled

  it('adds a commit with an SO, renders derived category/priority, and lock is enabled', async () => {
    renderWeekly([
      week({ status: 'DRAFT' }, [commit({ category: 'Delivery', priorityNumeric: 4.5 })]),
    ]);
    await screen.findByText('Wire the login gate');

    // The DERIVED chess layer renders read-only on the existing commit.
    expect(screen.getByTestId('commit-c1')).toHaveTextContent('Delivery');
    expect(screen.getByTestId('commit-c1')).toHaveTextContent('4.50');

    // Add a second commit linked to a Supporting Outcome via the single picker.
    await userEvent.type(screen.getByTestId('commit-title-input'), 'Harden the API endpoints');
    await userEvent.selectOptions(screen.getByTestId('commit-link-select'), 'so:so-2');
    await userEvent.click(screen.getByTestId('commit-add'));

    await waitFor(() => {
      const req = lastMatching(
        (u, m) => m === 'POST' && u.includes('projects/weekly-compass/weekly/2026-W23/commits'),
      );
      expect(req).toBeDefined();
      const body = JSON.parse(String(req!.body));
      expect(body.title).toBe('Harden the API endpoints');
      expect(body.supportingOutcomeId).toBe('so-2');
      // The client never authors the derived chess fields.
      expect(body.category).toBeUndefined();
      expect(body.priorityNumeric).toBeUndefined();
    });

    // ≥1 commit, all linked ⇒ lock enabled.
    expect(screen.getByTestId('lock-week')).not.toBeDisabled();
  });

  it('accepts an orphan commit via the orphan-reason branch of the picker', async () => {
    renderWeekly([week({ status: 'DRAFT' }, [commit()])]);
    await screen.findByText('Wire the login gate');

    await userEvent.type(screen.getByTestId('commit-title-input'), 'Patch the flaky test');
    await userEvent.selectOptions(screen.getByTestId('commit-link-select'), 'orphan:KTLO');
    await userEvent.click(screen.getByTestId('commit-add'));

    await waitFor(() => {
      const req = lastMatching(
        (u, m) => m === 'POST' && u.includes('projects/weekly-compass/weekly/2026-W23/commits'),
      );
      expect(req).toBeDefined();
      const body = JSON.parse(String(req!.body));
      expect(body.orphanReason).toBe('KTLO');
      expect(body.supportingOutcomeId).toBeUndefined();
    });
  });

  // ---- U10 edge: lock disabled with a per-commit reason for an unlinked commit

  it('disables lock with a per-commit reason while a commit has neither SO nor orphan', async () => {
    renderWeekly([
      week({ status: 'DRAFT' }, [
        commit({ id: 'good', title: 'Linked commit', supportingOutcomeId: 'so-1' }),
        commit({
          id: 'bad',
          title: 'Floating commit',
          supportingOutcomeId: undefined,
          orphanReason: undefined,
        }),
      ]),
    ]);
    await screen.findByText('Floating commit');

    expect(screen.getByTestId('lock-week')).toBeDisabled();
    const blockers = screen.getByTestId('lock-blockers');
    expect(blockers).toHaveTextContent(
      '"Floating commit" needs a Supporting Outcome or an orphan reason',
    );
    // The unlinked row flags itself inline.
    expect(screen.getByTestId('commit-unlinked-bad')).toBeInTheDocument();
  });

  it('disables lock on an empty DRAFT week with the "needs at least one commit" reason', async () => {
    renderWeekly([week({ status: 'DRAFT' }, [])]);
    await screen.findByText('Week 2026-W23');
    expect(screen.getByTestId('lock-week')).toBeDisabled();
    expect(screen.getByTestId('lock-blockers')).toHaveTextContent(
      'a week needs at least one commit to lock',
    );
  });

  // ---- U10 edge: overriding the derived category shows the override flag

  it('shows the override flag when the chosen category contradicts the derived one', async () => {
    renderWeekly([week({ status: 'DRAFT' }, [commit({ id: 'c1', category: 'Delivery' })])]);
    await screen.findByText('Wire the login gate');

    // No override yet ⇒ no flag.
    expect(screen.queryByTestId('commit-override-flag-c1')).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getByTestId('commit-override-c1'), 'Strategic');
    expect(screen.getByTestId('commit-override-flag-c1')).toBeInTheDocument();

    // Selecting the derived value back clears the flag (override that AGREES isn't flagged).
    await userEvent.selectOptions(screen.getByTestId('commit-override-c1'), 'Delivery');
    expect(screen.queryByTestId('commit-override-flag-c1')).not.toBeInTheDocument();
  });

  // ---- U10 integration: the SO picker lists only supporting_outcome nodes

  it('lists only supporting_outcome nodes in the SO picker (not upper RCDO levels)', async () => {
    renderWeekly([week({ status: 'DRAFT' }, [commit()])]);
    await screen.findByText('Wire the login gate');

    const select = screen.getByTestId('commit-link-select') as HTMLSelectElement;
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toContain('so:so-1');
    expect(values).toContain('so:so-2');
    // The rally_cry node is NOT linkable.
    expect(values).not.toContain('so:rc1');
  });

  // ---- U10: lock issues the transition; surfaces server blockers on a 409

  it('fires lockWeek when all commits are linked', async () => {
    renderWeekly([week({ status: 'DRAFT' }, [commit({ supportingOutcomeId: 'so-1' })])]);
    await screen.findByText('Wire the login gate');

    await userEvent.click(screen.getByTestId('lock-week'));
    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) => m === 'POST' && u.includes('projects/weekly-compass/weekly/2026-W23/lock'),
        ),
      ).toBeDefined(),
    );
  });

  it('surfaces server blockers when lock returns a 409', async () => {
    renderWithProviders(<ProjectWeekly />, {
      route: '/projects/weekly-compass/weekly',
      routePath: '/projects/:projectId/weekly',
      seed: {
        projects: [PROJECT],
        objectives: OBJECTIVES,
        weekly: {
          'weekly-compass': [week({ status: 'DRAFT' }, [commit({ supportingOutcomeId: 'so-1' })])],
        },
        routes: {
          'POST projects/weekly-compass/weekly/2026-W23/lock': {
            status: 409,
            body: { error: 'cannot lock the week', blockers: [{ reason: 'server says no' }] },
          },
        },
      },
    });
    await screen.findByText('Wire the login gate');

    await userEvent.click(screen.getByTestId('lock-week'));
    await waitFor(() =>
      expect(screen.getByTestId('lock-blockers')).toHaveTextContent('server says no'),
    );
  });

  // ---- U10: a non-DRAFT week is read-only

  it('renders a LOCKED week read-only with no add form and no lock button', async () => {
    renderWeekly([week({ status: 'LOCKED' }, [commit()])]);
    await screen.findByText('Wire the login gate');
    expect(screen.queryByTestId('add-commit-form')).not.toBeInTheDocument();
    expect(screen.queryByTestId('lock-week')).not.toBeInTheDocument();
    expect(screen.getByTestId('readonly-note')).toHaveTextContent('locked');
  });

  it('offers a reconcile link on a RECONCILING week', async () => {
    renderWeekly([week({ status: 'RECONCILING' }, [commit()])]);
    await screen.findByText('Wire the login gate');
    expect(screen.getByTestId('goto-reconcile')).toBeInTheDocument();
    expect(screen.queryByTestId('add-commit-form')).not.toBeInTheDocument();
  });
});
