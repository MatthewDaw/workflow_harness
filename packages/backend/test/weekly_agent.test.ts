import { describe, expect, it } from 'vitest';
import type { GitCommit, ObjectiveNode, Ticket, WeeklyItem } from '@harness/shared';
import { assembleWeekly, draftWeekly, validatePlan, type Challenger } from '../src/weekly/agent.js';
import { attributeDone, computeDeltas, summarizeAlignment } from '../src/weekly/align.js';

/**
 * U28 Weekly Update agent. Push-back on plan items mapping to no owned outcome
 * (the design-system example), Done attribution from git commits, alignment
 * deltas feeding U10, and the no-commits edge case. Bedrock is mocked by a fake
 * Challenger — no network.
 */

const ownedOutcomes: ObjectiveNode[] = [
  {
    id: 'SO-RECONCILE',
    org: 'acme',
    level: 'supporting_outcome',
    title: 'Reconciliation ships',
    pct: 40,
  },
  { id: 'SO-EXPORT', org: 'acme', level: 'supporting_outcome', title: 'Export available', pct: 0 },
];

const challenger: Challenger = {
  async challenge({ item }) {
    return `"${item.text}" maps to no owned outcome — which does it advance?`;
  },
};

describe('validatePlan: push-back on unaligned items', () => {
  it('accepts items linked to an owned outcome and challenges the rest', async () => {
    const plan: WeeklyItem[] = [
      { text: 'finish reconciliation view', objectiveId: 'SO-RECONCILE' },
      { text: 'redesign the design system', objectiveId: undefined },
    ];
    const result = await validatePlan(plan, ownedOutcomes, { challenger });
    expect(result.accepted.map((i) => i.text)).toEqual(['finish reconciliation view']);
    expect(result.challenges).toHaveLength(1);
    expect(result.challenges[0]?.item.text).toBe('redesign the design system');
    expect(result.challenges[0]?.message).toContain('which does it advance');
  });

  it('challenges an item linked to an outcome the project does NOT own', async () => {
    const plan: WeeklyItem[] = [{ text: 'random work', objectiveId: 'SO-SOMEONE-ELSE' }];
    const result = await validatePlan(plan, ownedOutcomes, { challenger });
    expect(result.accepted).toHaveLength(0);
    expect(result.challenges).toHaveLength(1);
  });

  it('admits a re-justified item even without a link', async () => {
    const plan = [{ text: 'critical infra fix', justified: true }];
    const result = await validatePlan(plan, ownedOutcomes, { challenger });
    expect(result.accepted.map((i) => i.text)).toEqual(['critical infra fix']);
    expect(result.challenges).toHaveLength(0);
  });
});

describe('Done attribution from git commits', () => {
  const tickets: Ticket[] = [
    {
      id: 'WC-37',
      projectId: 'p1',
      title: 'recon',
      status: 'done',
      priority: 'high',
      objectiveId: 'SO-RECONCILE',
    },
    {
      id: 'WC-40',
      projectId: 'p1',
      title: 'export',
      status: 'done',
      priority: 'medium',
      objectiveId: 'SO-EXPORT',
    },
  ];
  const commits: GitCommit[] = [
    { sha: 'c1', message: 'WC-37 recon', author: 'matt', committedAt: 't1', ticketIds: ['WC-37'] },
    {
      sha: 'c2',
      message: 'WC-37 more recon',
      author: 'matt',
      committedAt: 't2',
      ticketIds: ['WC-37'],
    },
    {
      sha: 'c3',
      message: 'WC-40 export',
      author: 'alice',
      committedAt: 't3',
      ticketIds: ['WC-40'],
    },
    { sha: 'c4', message: 'chore', author: 'matt', committedAt: 't4', ticketIds: [] },
  ];

  it('groups commits under the objective their ticket advances', () => {
    const { byObjective, unattributed } = attributeDone(commits, tickets);
    expect(byObjective['SO-RECONCILE']?.map((c) => c.sha)).toEqual(['c1', 'c2']);
    expect(byObjective['SO-EXPORT']?.map((c) => c.sha)).toEqual(['c3']);
    expect(unattributed.map((c) => c.sha)).toEqual(['c4']);
  });
});

describe('alignment + deltas', () => {
  it('summarizes alignment and computes deltas against prior cached %', () => {
    const items: WeeklyItem[] = [
      { text: 'a', objectiveId: 'SO-RECONCILE', completionPct: 80 },
      { text: 'b', objectiveId: 'SO-EXPORT', completionPct: 20 },
      { text: 'c', objectiveId: undefined },
    ];
    const alignment = summarizeAlignment(items, ['SO-RECONCILE', 'SO-EXPORT']);
    expect(alignment.alignedPct).toBeCloseTo((2 / 3) * 100, 1);
    expect(alignment.unaligned).toHaveLength(1);

    const deltas = computeDeltas(alignment, ownedOutcomes);
    const recon = deltas.find((d) => d.objectiveId === 'SO-RECONCILE');
    expect(recon?.priorPct).toBe(40);
    expect(recon?.reportedPct).toBe(80);
    expect(recon?.deltaPct).toBe(40);
  });
});

describe('assembleWeekly', () => {
  const tickets: Ticket[] = [
    {
      id: 'WC-37',
      projectId: 'p1',
      title: 'recon',
      status: 'done',
      priority: 'high',
      objectiveId: 'SO-RECONCILE',
    },
  ];
  const commits: GitCommit[] = [
    { sha: 'c1', message: 'WC-37', author: 'matt', committedAt: 't1', ticketIds: ['WC-37'] },
  ];

  it('builds Done from attributed commits + validated Plan with breakdowns', () => {
    const assembled = assembleWeekly({
      projectId: 'p1',
      isoWeek: '2026-W22',
      ownedOutcomeIds: ['SO-RECONCILE', 'SO-EXPORT'],
      plan: [{ text: 'ship export', objectiveId: 'SO-EXPORT', completionPct: 30 }],
      commits,
      tickets,
      nodes: ownedOutcomes,
    });
    expect(assembled.update.validated).toBe(true);
    expect(assembled.update.done.some((d) => d.objectiveId === 'SO-RECONCILE')).toBe(true);
    expect(assembled.update.plan).toHaveLength(1);
    expect(assembled.planAlignment.alignedPct).toBe(100);
    expect(assembled.deltas.find((d) => d.objectiveId === 'SO-EXPORT')?.reportedPct).toBe(30);
  });

  it('handles a week with no commits with an empty Done note', () => {
    const assembled = assembleWeekly({
      projectId: 'p1',
      isoWeek: '2026-W23',
      ownedOutcomeIds: ['SO-RECONCILE'],
      plan: [],
      commits: [],
      tickets,
      nodes: ownedOutcomes,
    });
    expect(assembled.update.done.some((d) => /no commits/i.test(d.text))).toBe(true);
  });
});

describe('draftWeekly end-to-end', () => {
  const tickets: Ticket[] = [
    {
      id: 'WC-37',
      projectId: 'p1',
      title: 'recon',
      status: 'done',
      priority: 'high',
      objectiveId: 'SO-RECONCILE',
    },
  ];
  const commits: GitCommit[] = [
    { sha: 'c1', message: 'WC-37', author: 'matt', committedAt: 't1', ticketIds: ['WC-37'] },
  ];

  it('stops at challenges when the plan has unaligned items', async () => {
    const out = await draftWeekly(
      {
        projectId: 'p1',
        isoWeek: '2026-W22',
        plan: [{ text: 'unrelated thing' }],
        ownedOutcomes,
        commits,
        tickets,
        nodes: ownedOutcomes,
      },
      { repo: undefined as never, challenger },
    );
    expect(out.validation.challenges).toHaveLength(1);
    expect(out.assembled).toBeUndefined();
  });

  it('assembles when every plan item is aligned', async () => {
    const out = await draftWeekly(
      {
        projectId: 'p1',
        isoWeek: '2026-W22',
        plan: [{ text: 'ship recon', objectiveId: 'SO-RECONCILE', completionPct: 90 }],
        ownedOutcomes,
        commits,
        tickets,
        nodes: ownedOutcomes,
      },
      { repo: undefined as never, challenger },
    );
    expect(out.validation.challenges).toHaveLength(0);
    expect(out.assembled?.update.validated).toBe(true);
  });
});
