import { beforeEach, describe, expect, it } from 'vitest';
import type { Project, WeeklyCommit, WeeklyPlan } from '@harness/shared';
import { getManagerBrief, type ManagerBrief } from '../src/rest/weeklyManager.js';
import { putObjective } from '../src/db/pg/objectivesRepo.js';
import { createCommit, upsertPlan } from '../src/db/pg/weeklyRepo.js';
import type { PgDb } from '../src/db/pg/migrate.js';
import { memRepoHarness } from './helpers/memtable.js';
import { makePgliteDb } from './helpers/pgharness.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U8 — the reports-scoped manager exception/divergence brief (KTD6). The brief is
 * scoped strictly by the `managerUserId` edge (`listReports`), keyset-paginated
 * over reports (R10), and distils each report's latest week to its exceptions
 * (highest-leverage commit not started, oldest carry, longest-starved SO, lock
 * failures) plus their strategic concentration (U17). A caller with no reports
 * gets an empty brief; an all-green report surfaces no exceptions
 * ("nothing needs you").
 */

const { repo } = memRepoHarness();

let db: PgDb;
beforeEach(async () => {
  db = await makePgliteDb();
});
const deps = () => ({ repo, db });

const MGR = 'manager';
const ALICE = 'alice';
const BOB = 'bob';
const ORG = 'acme';

function briefEvent(userId: string | null, query?: Record<string, string>) {
  return httpEvent({ method: 'GET', userId, org: ORG, rawPath: '/weekly/manager', ...(query ? { query } : {}) });
}

/** Seed a project owned by `owner`, stamped with the org so metrics resolve. */
async function seedProject(id: string, owner: string): Promise<void> {
  const project: Project = {
    id,
    name: id,
    repo: `gh/acme/${id}`,
    ownerUserId: owner,
    org: ORG,
    liveSessionCount: 0,
    enabledSkills: [],
    enabledBundles: [],
    enabledAgents: [],
    enabledWorkflows: [],
    enabledAgentBundles: [],
    enabledMcpServers: [],
  };
  await repo.putProject(project);
}

async function seedSo(id: string, pct?: number): Promise<void> {
  await putObjective(db, {
    id,
    org: ORG,
    level: 'supporting_outcome',
    title: id,
    ...(pct != null ? { pct } : {}),
  });
}

const plan = (over: Pick<WeeklyPlan, 'projectId' | 'isoWeek'> & Partial<WeeklyPlan>): WeeklyPlan => ({
  status: 'RECONCILED',
  posture: 'focus',
  ...over,
});

let seq = 0;
const commit = (over: Pick<WeeklyCommit, 'projectId' | 'isoWeek'> & Partial<WeeklyCommit>): WeeklyCommit => ({
  id: over.id ?? `c-${++seq}`,
  title: over.title ?? 'commit',
  supportingOutcomeId: over.orphanReason ? undefined : (over.supportingOutcomeId ?? 'so-1'),
  alsoAdvances: [],
  category: 'Delivery',
  priorityNumeric: 1,
  status: 'done',
  carryDepth: 0,
  ...over,
});

describe('GET /weekly/manager — reports-scoped exception brief (U8)', () => {
  it('401 when unauthenticated', async () => {
    const res = await getManagerBrief(briefEvent(null), deps());
    expect(res).toMatchObject({ statusCode: 401 });
  });

  it('a caller with no reports gets an empty brief (nothing needs you)', async () => {
    const res = await getManagerBrief(briefEvent(MGR), deps());
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<ManagerBrief>(res)).toEqual({ reports: [] });
  });

  it('groups exceptions per report (happy path: 2 reports)', async () => {
    await repo.setUserOrg(ALICE, ORG, { name: 'Alice' });
    await repo.setUserOrg(BOB, ORG, { name: 'Bob' });
    await repo.setManager(ALICE, MGR);
    await repo.setManager(BOB, MGR);

    await seedProject('p-alice', ALICE);
    await seedProject('p-bob', BOB);
    await seedSo('so-1', 80);
    await seedSo('so-2', 20); // never touched → starved, far behind

    // Alice's latest week: a still-`planned`, high-leverage commit (not started)
    // and a deep carry.
    await upsertPlan(db, plan({ projectId: 'p-alice', isoWeek: '2026-W23' }));
    await createCommit(
      db,
      commit({ projectId: 'p-alice', isoWeek: '2026-W23', supportingOutcomeId: 'so-1', status: 'planned', priorityNumeric: 9, title: 'big bet' }),
    );
    await createCommit(
      db,
      commit({ projectId: 'p-alice', isoWeek: '2026-W23', supportingOutcomeId: 'so-1', carryDepth: 3, title: 'stale line' }),
    );

    // Bob's latest week: a clean done commit — no exceptions of his own beyond the
    // org-wide starved SO.
    await upsertPlan(db, plan({ projectId: 'p-bob', isoWeek: '2026-W23' }));
    await createCommit(
      db,
      commit({ projectId: 'p-bob', isoWeek: '2026-W23', supportingOutcomeId: 'so-1', status: 'done' }),
    );

    const res = await getManagerBrief(briefEvent(MGR), deps());
    expect(res).toMatchObject({ statusCode: 200 });
    const brief = bodyOf<ManagerBrief>(res);
    expect(brief.reports.map((r) => r.userId)).toEqual([ALICE, BOB]);

    const alice = brief.reports.find((r) => r.userId === ALICE)!;
    expect(alice.name).toBe('Alice');
    expect(alice.latestWeek).toMatchObject({ projectId: 'p-alice', isoWeek: '2026-W23', status: 'RECONCILED' });
    const kinds = alice.exceptions.map((e) => e.kind);
    expect(kinds).toContain('highest_leverage_not_started');
    expect(kinds).toContain('oldest_carry');
    expect(kinds).toContain('longest_starved_outcome');
    // The highest-leverage-not-started points at the priority-9 planned commit.
    const notStarted = alice.exceptions.find((e) => e.kind === 'highest_leverage_not_started')!;
    expect(notStarted.detail).toContain('big bet');
    // Concentration computed over Alice's reconciled window.
    expect(alice.concentration.nodes).toBe(1);
  });

  it('a report with no weeks → null latestWeek, no exceptions', async () => {
    await repo.setUserOrg(ALICE, ORG, { name: 'Alice' });
    await repo.setManager(ALICE, MGR);
    await seedProject('p-alice', ALICE);
    // No weekly plans for Alice's project.

    const res = await getManagerBrief(briefEvent(MGR), deps());
    const brief = bodyOf<ManagerBrief>(res);
    const alice = brief.reports.find((r) => r.userId === ALICE)!;
    expect(alice.latestWeek).toBeNull();
    expect(alice.exceptions).toEqual([]);
  });

  it('an all-green report surfaces no commit-level exceptions', async () => {
    await repo.setUserOrg(ALICE, ORG, { name: 'Alice' });
    await repo.setManager(ALICE, MGR);
    await seedProject('p-alice', ALICE);
    await seedSo('so-1', 80);
    await upsertPlan(db, plan({ projectId: 'p-alice', isoWeek: '2026-W23' }));
    await createCommit(
      db,
      commit({ projectId: 'p-alice', isoWeek: '2026-W23', supportingOutcomeId: 'so-1', status: 'done' }),
    );

    const res = await getManagerBrief(briefEvent(MGR), deps());
    const brief = bodyOf<ManagerBrief>(res);
    const alice = brief.reports.find((r) => r.userId === ALICE)!;
    // No planned, no carry, no lock failure; so-1 was touched (the only org SO) →
    // no starvation exception either. Nothing needs the manager.
    expect(alice.exceptions).toEqual([]);
  });

  it('an orphan commit (a typed non-link) is NOT a lock failure', async () => {
    await repo.setUserOrg(ALICE, ORG, { name: 'Alice' });
    await repo.setManager(ALICE, MGR);
    await seedProject('p-alice', ALICE);
    await upsertPlan(db, plan({ projectId: 'p-alice', isoWeek: '2026-W23', status: 'DRAFT' }));
    // The SO-or-orphan DB CHECK (KTD10) forbids inserting a commit with NEITHER, so
    // the lock_failure exception is a defensive last line. A valid orphan commit
    // (a typed non-link) must NOT trip it.
    await createCommit(
      db,
      commit({ projectId: 'p-alice', isoWeek: '2026-W23', orphanReason: 'Incident', status: 'done' }),
    );

    const res = await getManagerBrief(briefEvent(MGR), deps());
    const brief = bodyOf<ManagerBrief>(res);
    const alice = brief.reports.find((r) => r.userId === ALICE)!;
    expect(alice.exceptions.some((e) => e.kind === 'lock_failure')).toBe(false);
  });

  it('keyset-paginates over reports (limit + cursor round-trip — R10)', async () => {
    // Three reports; page size 2.
    for (const u of [ALICE, BOB, 'carol']) {
      await repo.setUserOrg(u, ORG);
      await repo.setManager(u, MGR);
    }

    const first = bodyOf<ManagerBrief>(await getManagerBrief(briefEvent(MGR, { limit: '2' }), deps()));
    expect(first.reports.map((r) => r.userId)).toEqual([ALICE, BOB]);
    expect(first.nextCursor).toBe(BOB);

    const second = bodyOf<ManagerBrief>(
      await getManagerBrief(briefEvent(MGR, { limit: '2', cursor: first.nextCursor! }), deps()),
    );
    expect(second.reports.map((r) => r.userId)).toEqual(['carol']);
    expect(second.nextCursor).toBeUndefined();
  });

  it('400 on an invalid limit', async () => {
    const res = await getManagerBrief(briefEvent(MGR, { limit: '-1' }), deps());
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('the longest-starved SO ranks by behind-ness (the furthest behind surfaces)', async () => {
    await repo.setUserOrg(ALICE, ORG);
    await repo.setManager(ALICE, MGR);
    await seedProject('p-alice', ALICE);
    await seedSo('touched', 50);
    await seedSo('behind', 10); // never touched, far behind
    await seedSo('ahead', 90); // never touched, nearly done
    await upsertPlan(db, plan({ projectId: 'p-alice', isoWeek: '2026-W23' }));
    await createCommit(
      db,
      commit({ projectId: 'p-alice', isoWeek: '2026-W23', supportingOutcomeId: 'touched', status: 'done' }),
    );

    const res = await getManagerBrief(briefEvent(MGR), deps());
    const brief = bodyOf<ManagerBrief>(res);
    const alice = brief.reports.find((r) => r.userId === ALICE)!;
    const starved = alice.exceptions.find((e) => e.kind === 'longest_starved_outcome')!;
    expect(starved.supportingOutcomeId).toBe('behind');
  });
});
