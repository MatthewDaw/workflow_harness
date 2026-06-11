import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ManagerBrief as ManagerBriefDto, ReportBrief } from '../../api/baseApi.js';
import { ManagerBrief } from './ManagerBrief.js';
import { AppShell } from '../../components/AppShell.js';
import { renderWithProviders } from '../../test/testUtils.js';

/** A report-brief node with sensible defaults; override what the test exercises. */
function report(overrides: Partial<ReportBrief> = {}): ReportBrief {
  return {
    userId: 'r1',
    name: 'Report One',
    latestWeek: { projectId: 'weekly-compass', isoWeek: '2026-W23', status: 'LOCKED' },
    exceptions: [],
    concentration: { herfindahl: 0.5, nodes: 2, postureDivergence: 0 },
    ...overrides,
  };
}

function renderBrief(brief: ManagerBriefDto) {
  return renderWithProviders(<ManagerBrief />, {
    route: '/weekly/manager',
    routePath: '/weekly/manager',
    seed: { managerBrief: brief },
  });
}

describe('ManagerBrief — reports-scoped exception/divergence brief (U12)', () => {
  afterEach(() => vi.unstubAllGlobals());

  // ---- Happy path: 2 reports → grouped exception rows ----

  it('renders grouped exception rows per report', async () => {
    renderBrief({
      reports: [
        report({
          userId: 'r1',
          name: 'Ada',
          exceptions: [
            {
              kind: 'highest_leverage_not_started',
              detail: 'highest-leverage commit not started: "Ship the gate"',
              commitId: 'c1',
            },
            {
              kind: 'oldest_carry',
              detail: 'oldest carry: "Flaky test" at carry depth 2',
              commitId: 'c2',
            },
          ],
        }),
        report({
          userId: 'r2',
          name: 'Linus',
          exceptions: [
            {
              kind: 'longest_starved_outcome',
              detail: 'longest-starved supporting outcome: so-9',
              supportingOutcomeId: 'so-9',
            },
          ],
        }),
      ],
    });

    // Both reports rendered, each as its own card.
    expect(await screen.findByTestId('brief-report-r1')).toBeInTheDocument();
    expect(screen.getByTestId('brief-report-r2')).toBeInTheDocument();
    expect(screen.getByText('Ada')).toBeInTheDocument();
    expect(screen.getByText('Linus')).toBeInTheDocument();

    // The exceptions group under their report, by detail text.
    const ada = screen.getByTestId('brief-report-r1');
    expect(ada).toHaveTextContent('highest-leverage commit not started: "Ship the gate"');
    expect(ada).toHaveTextContent('oldest carry: "Flaky test" at carry depth 2');

    const linus = screen.getByTestId('brief-report-r2');
    expect(linus).toHaveTextContent('longest-starved supporting outcome: so-9');

    // Concentration is surfaced per report (Herfindahl → percent).
    expect(screen.getByTestId('brief-concentration-r1')).toHaveTextContent('50%');
    expect(screen.getByTestId('brief-concentration-r1')).toHaveTextContent('2 nodes');
  });

  // ---- Edge: all-green → empty-state ----

  it('shows the empty "nothing needs you" state when there are no reports', async () => {
    renderBrief({ reports: [] });
    expect(await screen.findByTestId('brief-empty')).toBeInTheDocument();
    expect(screen.getByTestId('brief-empty')).toHaveTextContent('Nothing needs you');
  });

  it('shows a per-report "Nothing needs you" when a report has no exceptions', async () => {
    renderBrief({ reports: [report({ userId: 'r1', name: 'Ada', exceptions: [] })] });
    expect(await screen.findByTestId('brief-report-clean-r1')).toBeInTheDocument();
    expect(screen.getByTestId('brief-report-clean-r1')).toHaveTextContent('Nothing needs you.');
  });

  it('renders a report with no week as "no weeks yet"', async () => {
    renderBrief({
      reports: [report({ userId: 'r1', name: 'Ada', latestWeek: null, exceptions: [] })],
    });
    expect(await screen.findByTestId('brief-no-week-r1')).toBeInTheDocument();
  });

  // ---- Integration: a row deep-links to /projects/:id/weekly ----

  it("deep-links each report to that report's project weekly tab", async () => {
    renderBrief({
      reports: [
        report({
          userId: 'r1',
          name: 'Ada',
          latestWeek: { projectId: 'proj-ada', isoWeek: '2026-W23', status: 'LOCKED' },
        }),
      ],
    });
    const link = await screen.findByTestId('brief-report-link-r1');
    expect(link).toHaveAttribute('href', '/projects/proj-ada/weekly');
  });

  // ---- Pagination: "Load more" appends the next page ----

  it('paginates: "Load more" follows the cursor and appends the next page', async () => {
    // Script two pages: the first carries `nextCursor`, the second does not.
    const page1: ManagerBriefDto = {
      reports: [report({ userId: 'r1', name: 'Ada' })],
      nextCursor: 'r1',
    };
    const page2: ManagerBriefDto = {
      reports: [report({ userId: 'r2', name: 'Linus' })],
    };
    let calls = 0;
    renderWithProviders(<ManagerBrief />, {
      route: '/weekly/manager',
      routePath: '/weekly/manager',
      seed: {
        routes: {
          'GET weekly/manager': () => (calls++ === 0 ? page1 : page2),
        },
      },
    });

    // First page renders with a "Load more" affordance.
    expect(await screen.findByTestId('brief-report-r1')).toBeInTheDocument();
    expect(screen.queryByTestId('brief-report-r2')).not.toBeInTheDocument();
    const loadMore = screen.getByTestId('brief-load-more');

    // Loading the next page APPENDS — the first page's report stays.
    await userEvent.click(loadMore);
    await waitFor(() => expect(screen.getByTestId('brief-report-r2')).toBeInTheDocument());
    expect(screen.getByTestId('brief-report-r1')).toBeInTheDocument();

    // The second page has no `nextCursor`, so "Load more" is gone.
    expect(screen.queryByTestId('brief-load-more')).not.toBeInTheDocument();
  });

  it('shows no "Load more" when the first page is already the last', async () => {
    renderBrief({ reports: [report({ userId: 'r1', name: 'Ada' })] });
    await screen.findByTestId('brief-report-r1');
    expect(screen.queryByTestId('brief-load-more')).not.toBeInTheDocument();
  });
});

describe('ManagerBrief nav gating (U12)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('shows the Manager Brief nav entry only when the caller has reports', async () => {
    renderWithProviders(<AppShell />, {
      route: '/',
      routePath: '/',
      seed: { managerBrief: { reports: [report({ userId: 'r1' })] } },
    });
    expect(await screen.findByRole('link', { name: 'Manager Brief' })).toBeInTheDocument();
  });

  it('hides the Manager Brief nav entry for a user with no reports', async () => {
    renderWithProviders(<AppShell />, {
      route: '/',
      routePath: '/',
      seed: { managerBrief: { reports: [] } },
    });
    // Wait for the chrome to render, then assert the gated entry is absent.
    await screen.findByRole('button', { name: 'Sign out' });
    expect(screen.queryByRole('link', { name: 'Manager Brief' })).not.toBeInTheDocument();
  });
});
