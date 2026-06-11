import { and, eq, inArray } from 'drizzle-orm';
import type { Posture } from '@harness/shared';
import { objectives, weeklyCommits, weeklyPlans } from '../db/pg/schema.js';
import type { PgDb } from '../db/pg/migrate.js';

/**
 * The three replacement health signals (KTD8, U17) that supersede the deleted
 * conformity score. Under hard-enforced SO linkage "does work ladder up?" is
 * structurally 100% — a dead metric — so it is replaced by three distinct,
 * un-gameable signals computed off the single-source roll-up:
 *
 *  - **Strategic Concentration Index** — Herfindahl over the distinct SO nodes a
 *    subject's reconciled commits touch, priority-weighted, normalized; reported
 *    as signed DIVERGENCE from the declared `posture` so legitimate breadth is
 *    never punished (`focus` expects high concentration, `explore` low).
 *  - **Strategic starvation/coverage** — which Supporting Outcomes received ZERO
 *    commits this window, weighted by `(1 − pct_cache)` so far-behind starved SOs
 *    rank highest.
 *  - **Carry-aging** — the distribution of `carry_depth`; `>= 3` is the
 *    decompose/kill threshold.
 *
 * The compute halves are PURE (fixture-driven, reproducible — the test contract),
 * and a thin `gather*` layer reads the inputs from Postgres. All three accept an
 * aggregation SCOPE (user / team / org) via the same query parameterized by the
 * subject set — here, the set of project ids the subject owns (a project maps to
 * an owner via the `projects` mirror), so a team scope is just the UNION of its
 * members' project sets and folds to a single team number.
 */

/** The `carry_depth` at which a line raises the decompose/kill nudge (KTD3/KTD8). */
export const CARRY_AGING_DECOMPOSE_THRESHOLD = 3;

/* -------------------------------------------------------------------------- */
/* Strategic Concentration Index                                              */
/* -------------------------------------------------------------------------- */

/**
 * One reconciled commit's contribution to a subject's concentration: the primary
 * Supporting Outcome it touches and its derived priority weight. Orphan commits
 * (no SO) never appear here — only primary-SO-linked work shapes concentration,
 * matching the roll-up's credit model (KTD9).
 */
export interface ConcentrationCommit {
  /** The PRIMARY Supporting Outcome this commit touches (KTD9). */
  supportingOutcomeId: string;
  /** The derived WSJF priority — the weight each commit lends its SO (KTD4). */
  priorityNumeric: number;
}

/**
 * The computed concentration signal for a subject (a person / team / org).
 *  - `herfindahl` is the priority-weighted Herfindahl-Hirschman index over the
 *    distinct SO nodes the subject touched: `Σ sᵢ²` where `sᵢ` is SO `i`'s share
 *    of the total priority weight. It is `1` when all weight is on one SO and
 *    approaches `1 / nodes` as effort spreads evenly — higher = more concentrated.
 *  - `nodes` is the count of distinct SO nodes touched.
 *  - `postureDivergence` is the SIGNED divergence from the declared posture
 *    (KTD8): for `focus` it is `1 − herfindahl` (concentrated = aligned ⇒ near 0);
 *    for `explore` it is `herfindahl` itself (spread = aligned ⇒ near 0). A larger
 *    value means MORE divergent from the declared intent — it never punishes
 *    legitimate breadth because the "good" direction flips with the posture.
 */
export interface ConcentrationSignal {
  herfindahl: number;
  nodes: number;
  postureDivergence: number;
}

/**
 * Compute the priority-weighted Herfindahl concentration over the SO nodes a
 * subject's reconciled commits touch (KTD8), and its signed divergence from the
 * declared `posture`. Pure — the caller gathers the commits + posture.
 *
 * With no commits the Herfindahl is `0` and `nodes` is `0` (no concentration to
 * speak of). A commit with non-positive priority contributes a floor weight of
 * `0` to the share math but still counts its SO toward `nodes` only if some
 * positive weight lands on it; to keep the index meaningful every commit lends at
 * least an epsilon so a subject that worked exclusively on a single
 * zero-priority SO still reads as maximally concentrated.
 */
export function computeConcentration(
  commits: readonly ConcentrationCommit[],
  posture: Posture,
): ConcentrationSignal {
  // Every commit lends at least a tiny floor so zero/negative derived priorities
  // do not erase a subject's effort from the share math.
  const EPS = 1e-9;
  const weightByNode = new Map<string, number>();
  for (const c of commits) {
    const w = Math.max(EPS, c.priorityNumeric);
    weightByNode.set(c.supportingOutcomeId, (weightByNode.get(c.supportingOutcomeId) ?? 0) + w);
  }

  const nodes = weightByNode.size;
  const total = [...weightByNode.values()].reduce((s, w) => s + w, 0);

  if (nodes === 0 || total <= 0) {
    return { herfindahl: 0, nodes: 0, postureDivergence: posture === 'focus' ? 1 : 0 };
  }

  let herfindahl = 0;
  for (const w of weightByNode.values()) {
    const share = w / total;
    herfindahl += share * share;
  }
  // Guard against floating drift outside [0, 1].
  herfindahl = Math.min(1, Math.max(0, herfindahl));

  // `focus` wants high concentration (divergence = how far BELOW full
  // concentration); `explore` wants low (divergence = the concentration itself).
  const postureDivergence = posture === 'focus' ? 1 - herfindahl : herfindahl;

  return { herfindahl, nodes, postureDivergence };
}

/* -------------------------------------------------------------------------- */
/* Strategic starvation / coverage                                            */
/* -------------------------------------------------------------------------- */

/** A Supporting Outcome considered for starvation: its id and cached roll-up %. */
export interface StarvationCandidate {
  /** The Supporting Outcome id. */
  id: string;
  /** Its cached roll-up completion (0..100); absent ⇒ treated as 0% (fully behind). */
  pctCache?: number;
}

/** A starved Supporting Outcome — zero commits this window — ranked by behind-ness. */
export interface StarvedOutcome {
  id: string;
  /** `(1 − pct/100)` — far-behind starved SOs (low pct) rank highest (closer to 1). */
  weight: number;
}

/**
 * Compute the starvation/coverage signal (KTD8): the org Supporting Outcomes that
 * received ZERO commits this window, each weighted by `(1 − pct_cache/100)` so the
 * far-behind starved SOs rank highest. Returned sorted by descending weight (ties
 * by id for determinism). Pure — the caller gathers the candidate SOs + the set of
 * SO ids that were touched this window.
 */
export function computeStarvation(
  candidates: readonly StarvationCandidate[],
  touchedSoIds: ReadonlySet<string>,
): StarvedOutcome[] {
  return candidates
    .filter((so) => !touchedSoIds.has(so.id))
    .map((so) => {
      const pct = Math.min(100, Math.max(0, so.pctCache ?? 0));
      return { id: so.id, weight: 1 - pct / 100 };
    })
    .sort((a, b) => b.weight - a.weight || a.id.localeCompare(b.id));
}

/* -------------------------------------------------------------------------- */
/* Carry-aging                                                                */
/* -------------------------------------------------------------------------- */

/**
 * The carry-aging signal (KTD8): the distribution of `carry_depth` across a
 * subject's commits and the count at or beyond the decompose/kill threshold.
 *  - `distribution` maps each observed `carry_depth` to how many commits sit at
 *    it (depth `0` = never carried).
 *  - `deepCount` is how many commits are at `carry_depth >= 3` (the decompose/kill
 *    smell — chronic over-commitment or a unit that needs decomposing).
 */
export interface CarryAgingSignal {
  distribution: Record<number, number>;
  deepCount: number;
}

/**
 * Compute the carry-aging distribution + the deep-carry (`>= 3`) count over a set
 * of commit carry depths (KTD8). Pure — the caller gathers the depths.
 */
export function computeCarryAging(depths: readonly number[]): CarryAgingSignal {
  const distribution: Record<number, number> = {};
  let deepCount = 0;
  for (const d of depths) {
    distribution[d] = (distribution[d] ?? 0) + 1;
    if (d >= CARRY_AGING_DECOMPOSE_THRESHOLD) deepCount += 1;
  }
  return { distribution, deepCount };
}

/* -------------------------------------------------------------------------- */
/* Gather: read the metric inputs from Postgres (foldable scope)              */
/* -------------------------------------------------------------------------- */

/**
 * The subject scope for a metric read (R12): the set of project ids the subject
 * owns. A user scope is that user's projects; a team scope is the UNION of its
 * members' projects (so it folds to a single team number); an org scope is every
 * project in the org. The caller resolves the scope → project ids (via the
 * `projects` mirror / `listReports`) and passes the ids here.
 */
export interface MetricSubject {
  /** The project ids whose latest reconciled weeks form this subject's window. */
  projectIds: string[];
}

/**
 * The reconciled-window commits + posture a subject's metrics read. For each of
 * the subject's projects we take ONLY its LATEST reconciled week (the same "latest
 * reconciled window" the roll-up uses — `rollupRepo`), mirroring the single-source
 * model so the metrics and the roll-up never disagree on which window is current.
 */
async function gatherWindow(
  db: PgDb,
  subject: MetricSubject,
): Promise<{
  /** Primary-SO-linked commits in the window (concentration / coverage inputs). */
  linkedCommits: ConcentrationCommit[];
  /** Every commit's carry depth in the window (carry-aging input). */
  carryDepths: number[];
  /** The set of SO ids touched by a primary link this window (starvation input). */
  touchedSoIds: Set<string>;
  /** The window posture: `explore` if ANY project's latest reconciled week is explore, else focus. */
  posture: Posture;
}> {
  const empty = {
    linkedCommits: [] as ConcentrationCommit[],
    carryDepths: [] as number[],
    touchedSoIds: new Set<string>(),
    posture: 'focus' as Posture,
  };
  if (subject.projectIds.length === 0) return empty;

  // Latest reconciled week per project (the window) + its posture.
  const reconciledPlans = await db
    .select({
      projectId: weeklyPlans.projectId,
      isoWeek: weeklyPlans.isoWeek,
      posture: weeklyPlans.posture,
    })
    .from(weeklyPlans)
    .where(
      and(eq(weeklyPlans.status, 'RECONCILED'), inArray(weeklyPlans.projectId, subject.projectIds)),
    );

  const latestByProject = new Map<string, { isoWeek: string; posture: string }>();
  for (const p of reconciledPlans) {
    const cur = latestByProject.get(p.projectId);
    if (!cur || p.isoWeek > cur.isoWeek) {
      latestByProject.set(p.projectId, { isoWeek: p.isoWeek, posture: p.posture });
    }
  }
  if (latestByProject.size === 0) return empty;

  // Posture folds across the subject's windows: `explore` if ANY window declares
  // it (the subject is exploring somewhere), else `focus`.
  const posture: Posture = [...latestByProject.values()].some((w) => w.posture === 'explore')
    ? 'explore'
    : 'focus';

  const windowProjects = [...latestByProject.keys()];
  const rows = await db
    .select({
      projectId: weeklyCommits.projectId,
      isoWeek: weeklyCommits.isoWeek,
      supportingOutcomeId: weeklyCommits.supportingOutcomeId,
      priorityNumeric: weeklyCommits.priorityNumeric,
      carryDepth: weeklyCommits.carryDepth,
    })
    .from(weeklyCommits)
    .where(inArray(weeklyCommits.projectId, windowProjects));

  const inWindow = rows.filter((r) => latestByProject.get(r.projectId)?.isoWeek === r.isoWeek);

  const linkedCommits: ConcentrationCommit[] = [];
  const touchedSoIds = new Set<string>();
  const carryDepths: number[] = [];
  for (const r of inWindow) {
    carryDepths.push(r.carryDepth);
    if (r.supportingOutcomeId != null) {
      linkedCommits.push({
        supportingOutcomeId: r.supportingOutcomeId,
        priorityNumeric: r.priorityNumeric,
      });
      touchedSoIds.add(r.supportingOutcomeId);
    }
  }

  return { linkedCommits, carryDepths, touchedSoIds, posture };
}

/** The full metric bundle for a subject at a given scope (R12). */
export interface WeeklyMetrics {
  concentration: ConcentrationSignal;
  starvation: StarvedOutcome[];
  carryAging: CarryAgingSignal;
}

/**
 * Gather + compute all three KTD8 signals for a subject scope (R12). The same
 * query is parameterized by the subject's project set, so user / team / org scopes
 * differ ONLY in which project ids are passed — a team is the union of its members'
 * projects and folds to a single team number.
 *
 * Starvation is evaluated against the org's Supporting Outcomes (the `org` arg);
 * coverage is an org-wide question (which org SOs got zero attention), while
 * concentration + carry-aging are over the subject's own window.
 */
export async function gatherWeeklyMetrics(
  db: PgDb,
  org: string,
  subject: MetricSubject,
): Promise<WeeklyMetrics> {
  const window = await gatherWindow(db, subject);

  const soRows = await db
    .select({ id: objectives.id, level: objectives.level, pctCache: objectives.pctCache })
    .from(objectives)
    .where(and(eq(objectives.org, org), eq(objectives.level, 'supporting_outcome')));
  const candidates: StarvationCandidate[] = soRows.map((r) => ({
    id: r.id,
    ...(r.pctCache != null ? { pctCache: r.pctCache } : {}),
  }));

  return {
    concentration: computeConcentration(window.linkedCommits, window.posture),
    starvation: computeStarvation(candidates, window.touchedSoIds),
    carryAging: computeCarryAging(window.carryDepths),
  };
}
