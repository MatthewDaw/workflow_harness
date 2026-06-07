import { describe, it, expect, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import type { LearningRecord, SessionProjection } from '@harness/shared';
import { SessionsTable } from '../screens/Sessions/SessionsTable.js';
import { LiveWatch } from '../screens/LiveWatch/LiveWatch.js';
import { renderWithProviders } from './testUtils.js';

/**
 * U7 — topic-focus Web UI. Covers the Sessions table topic column (label primary,
 * stable slug muted, description tooltip, Untitled fallback) and the session
 * detail surfaces (within-session topic timeline + impl/doc learning sections
 * with their zero/empty states).
 */

const BASE: SessionProjection = {
  sessionId: 'a91f',
  projectId: 'weekly-compass',
  name: 'reconcile-variance',
  host: 'matt@mbp',
  agent: 'builder',
  status: 'active',
  tokens: 48000,
  costUsd: 0.62,
  startedAt: 1000,
  lastEventAt: 5000,
  maxSeq: 5,
};

function learning(over: Partial<LearningRecord>): LearningRecord {
  return {
    projectId: 'weekly-compass',
    sessionId: 'a91f',
    segmentId: 'seg-1',
    topicLabel: 'Variance reconciliation',
    stream: 'impl',
    text: 'Prefer the cached variance over recomputing.',
    turnId: 't-1',
    ts: 2000,
    seq: 2,
    ...over,
  };
}

describe('SessionsTable — topic column', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders the topic label as primary with the stable slug muted and a description tooltip', () => {
    const session: SessionProjection = {
      ...BASE,
      topic: 'Variance reconciliation',
      description: 'Reworking how monthly variance rolls up to the weekly compass view.',
    };
    renderWithProviders(<SessionsTable sessions={[session]} />, {
      route: '/sessions',
      routePath: '/sessions',
    });

    const cell = screen.getByTestId('session-topic-a91f');
    // Topic label is primary; the stable slug stays visible as muted secondary
    // text (so a user who navigated by slug isn't disoriented).
    expect(within(cell).getByTestId('session-topic-label-a91f')).toHaveTextContent(
      'Variance reconciliation',
    );
    expect(within(cell).getByTestId('session-topic-slug-a91f')).toHaveTextContent(
      'reconcile-variance',
    );
    // The first prompt column is gone — the slug shows, but not under a summary cell.
    expect(screen.queryByTestId('session-summary-a91f')).not.toBeInTheDocument();
    // The rolling description carries a hover tooltip (full text in `title`).
    const desc = within(cell).getByTestId('session-topic-desc-a91f');
    expect(desc).toHaveAttribute(
      'title',
      'Reworking how monthly variance rolls up to the weekly compass view.',
    );
  });

  it('falls back to a muted Untitled placeholder before any topic exists', () => {
    renderWithProviders(<SessionsTable sessions={[BASE]} />, {
      route: '/sessions',
      routePath: '/sessions',
    });

    const cell = screen.getByTestId('session-topic-a91f');
    expect(within(cell).getByTestId('session-topic-untitled-a91f')).toHaveTextContent('Untitled');
    expect(within(cell).queryByTestId('session-topic-label-a91f')).not.toBeInTheDocument();
  });
});

describe('LiveWatch — within-session topic timeline + learnings', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders topic segments in order and the impl/doc learning lists from the endpoint', async () => {
    const session: SessionProjection = {
      ...BASE,
      topic: 'Variance reconciliation',
      description: 'Current focus: rolling variance into the compass view.',
      summaryUpdatedAt: 5000,
    };
    const learnings: LearningRecord[] = [
      learning({ segmentId: 'seg-1', topicLabel: 'Scaffolding', ts: 1000, turnId: 't-1' }),
      learning({
        segmentId: 'seg-2',
        topicLabel: 'Variance reconciliation',
        ts: 3000,
        turnId: 't-2',
        stream: 'impl',
        text: 'Use the cached variance; do not recompute per render.',
      }),
      learning({
        segmentId: 'seg-2',
        topicLabel: 'Variance reconciliation',
        ts: 4000,
        turnId: 't-3',
        stream: 'doc',
        text: 'The variance doc says recompute is required — that contradicts the cache.',
        docRef: 'docs/variance.md',
      }),
    ];
    renderWithProviders(<LiveWatch />, {
      route: '/sessions/a91f',
      routePath: '/sessions/:sessionId',
      seed: { sessions: [session], learnings: { 'weekly-compass': learnings } },
    });

    await waitFor(() => expect(screen.getByTestId('live-status')).toHaveTextContent('active'));

    // Timeline: two segments, oldest first; the live current topic relabels the
    // latest row.
    await waitFor(() => expect(screen.getAllByTestId('topic-segment')).toHaveLength(2));
    const labels = screen.getAllByTestId('topic-segment-label').map((el) => el.textContent);
    expect(labels).toEqual(['Scaffolding', 'Variance reconciliation']);

    // Impl learnings list renders the impl-stream correction.
    const impl = screen.getByTestId('impl-learnings');
    expect(within(impl).getByText('Use the cached variance; do not recompute per render.')).toBeInTheDocument();
    expect(within(impl).queryByTestId('impl-learnings-empty')).not.toBeInTheDocument();

    // Doc learnings list renders the doc-stream correction + its docRef.
    const doc = screen.getByTestId('doc-learnings');
    expect(
      within(doc).getByText('The variance doc says recompute is required — that contradicts the cache.'),
    ).toBeInTheDocument();
    expect(within(doc).getByText('docs/variance.md')).toBeInTheDocument();
  });

  it('renders the timeline + both-learning zero/empty states when there is nothing logged', async () => {
    renderWithProviders(<LiveWatch />, {
      route: '/sessions/a91f',
      routePath: '/sessions/:sessionId',
      seed: { sessions: [BASE] }, // no topic, no learnings
    });

    await waitFor(() => expect(screen.getByTestId('live-status')).toHaveTextContent('active'));

    // Timeline empty state (no topic, no learnings).
    await waitFor(() =>
      expect(screen.getByTestId('topic-timeline-empty')).toHaveTextContent('No topic segments yet'),
    );
    // The doc section is NEVER hidden — it shows its zero-state.
    expect(screen.getByTestId('impl-learnings-empty')).toHaveTextContent(
      'No corrections logged this session',
    );
    expect(screen.getByTestId('doc-learnings-empty')).toHaveTextContent(
      'No doc loaded — doc learnings appear when a correction contradicts a loaded doc',
    );
  });
});
