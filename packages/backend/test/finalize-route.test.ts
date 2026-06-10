import { beforeEach, describe, expect, it } from 'vitest';
import { corroborationCount } from '@harness/shared';
import { memRepoHarness } from './helpers/memtable.js';
import {
  FakeEmbedder,
  FakeIdeaVectors as FakeVectors,
  FakeJudge,
  FakeWriter,
  seedSession as seedSessionFor,
  seedSkill as seedSkillFor,
  vectorFor,
} from './helpers/idea-fakes.js';
import {
  associateAndFinalize,
  finalizeRoute,
  routeTopic,
  type AssociateDeps,
  type RouteResult,
  type TopicFinding,
} from '../src/ideas/associate.js';
import type { OpenRouterEmbedder } from '../src/embeddings/embed.js';
import type { S3Vectors } from '../src/embeddings/s3vectors.js';
import type { RerankJudge } from '../src/rerank/judge.js';

/**
 * U10 — idea creation & re-evaluation MOVE semantics (the FINAL pipeline step).
 *
 * `finalizeRoute` maps a `routed` association into the corroborate `Finding` and
 * calls `corroborateFinding` (U7) — create/merge happens there. `associateAndFinalize`
 * composes route (U9) + finalize. The MOVE (R13): a segment that re-evaluates to a
 * DIFFERENT best skill strips the session off the prior skill's idea and adds it to
 * the new one — no double counting.
 *
 * The embedder / idea-writer / S3 Vectors are the shared FAKES
 * (helpers/idea-fakes.ts) so nothing touches the network: the writer encodes the
 * lesson key into the concept (`concept:<key>`); the embedder maps that to a
 * one-hot vector, so two phrasings of one lesson share a vector (similarity 1.0)
 * and distinct lessons are orthogonal.
 */

const { repo } = memRepoHarness();

const ORG = 'acme';

let embedder: FakeEmbedder;
let writer: FakeWriter;
let vectors: FakeVectors;

function deps(judge?: FakeJudge): AssociateDeps {
  return {
    repo,
    embedder: embedder as unknown as OpenRouterEmbedder,
    vectors: vectors as unknown as S3Vectors,
    writer: writer as unknown as never,
    ...(judge ? { judge: judge as unknown as RerankJudge } : {}),
  };
}

const seedSession = (opts: { sessionId: string; projectId: string; org: string }) =>
  seedSessionFor(repo, opts);
const seedSkill = (org: string, name: string, description: string) =>
  seedSkillFor(repo, org, name, description);

function topic(over: Partial<TopicFinding> = {}): TopicFinding {
  return {
    sessionId: 's-1',
    segmentId: 'seg-1',
    topicLabel: 'decimal money handling',
    description: 'Always use a decimal type for currency, never a float.',
    implLearnings: ['Use BigDecimal for money math.'],
    seq: 3,
    ...over,
  };
}

/** A pre-built `routed` RouteResult onto a chosen skill (drives finalizeRoute directly). */
function routed(over: Partial<RouteResult> & { skillBaseName: string }): RouteResult {
  return {
    outcome: 'routed',
    org: ORG,
    projectId: 'p-1',
    repoId: `gh/${ORG}/p-1`,
    confidence: 0.9,
    finding: topic(),
    ...over,
  };
}

beforeEach(() => {
  embedder = new FakeEmbedder();
  writer = new FakeWriter();
  vectors = new FakeVectors();
});

describe('finalizeRoute — idea creation (U10)', () => {
  it('turns a routed result into a created idea on the chosen skill, with provenance (R12/R15)', async () => {
    const res = await finalizeRoute(routed({ skillBaseName: 'hq-money' }), deps());

    expect(res.corroboration.created).toBe(true);
    expect(res.movedFrom).toEqual([]);

    const ideas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(ideas).toHaveLength(1);
    const idea = ideas[0]!;
    expect(idea.skillBaseName).toBe('hq-money');
    expect(idea.org).toBe(ORG);
    expect(idea.status).toBe('open');
    // The synthesized concept (not the raw transcript) is the idea text.
    expect(idea.text).toBe('concept:money');
    // Provenance carried onto the source (R15): session/segment/seq + project/repo.
    expect(idea.sources).toHaveLength(1);
    const src = idea.sources[0]!;
    expect(src.sessionId).toBe('s-1');
    expect(src.segmentId).toBe('seg-1');
    expect(src.seq).toBe(3);
    expect(src.projectId).toBe('p-1');
    expect(src.repoId).toBe(`gh/${ORG}/p-1`);
    expect(src.snippet).toBe('Always use a decimal type for currency, never a float.');
  });

  it('merges a second routed finding of the same lesson onto the same idea (corroborateFinding invoked)', async () => {
    await finalizeRoute(
      routed({ skillBaseName: 'hq-money', finding: topic({ sessionId: 's-1' }) }),
      deps(),
    );
    const res = await finalizeRoute(
      routed({
        skillBaseName: 'hq-money',
        finding: topic({
          sessionId: 's-2',
          segmentId: 'seg-2',
          description: 'Never represent currency as a float; use a decimal type.',
        }),
      }),
      deps(),
    );

    expect(res.corroboration.created).toBe(false);
    expect(res.corroboration.sessionCounted).toBe(true);
    const ideas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(ideas).toHaveLength(1);
    // Two distinct sessions corroborate the one idea.
    expect(corroborationCount(ideas[0]!)).toBe(2);
    expect(res.corroboration.corroborated).toBe(true);
  });

  it('rejects a non-routed RouteResult', async () => {
    await expect(
      finalizeRoute({ outcome: 'unassigned', org: ORG, finding: topic() }, deps()),
    ).rejects.toThrow(/routed/);
  });
});

describe('finalizeRoute — re-evaluation MOVE (R13)', () => {
  it('moves a session from a prior skill to a newly-chosen one (prior loses it, new gains it)', async () => {
    // First evaluation: the segment routes to hq-money.
    await finalizeRoute(routed({ skillBaseName: 'hq-money', finding: topic() }), deps());
    let moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(corroborationCount(moneyIdeas[0]!)).toBe(1);

    // Re-evaluation of the SAME (sessionId, segmentId): now best matches hq-retry.
    // (A different lesson key → a distinct idea on the new skill.)
    const reEval = routed({
      skillBaseName: 'hq-retry',
      finding: topic({
        description: 'Retry transient failures with exponential backoff.',
        implLearnings: ['Cap retries; jitter the backoff.'],
      }),
    });
    const res = await finalizeRoute(reEval, deps());

    // The session was MOVED OFF hq-money...
    expect(res.movedFrom.map((i) => i.skillBaseName)).toEqual(['hq-money']);
    moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(corroborationCount(moneyIdeas[0]!)).toBe(0); // decremented — no double count.

    // ...and ADDED to hq-retry.
    const retryIdeas = await repo.listIdeasForSkill(ORG, 'hq-retry');
    expect(retryIdeas).toHaveLength(1);
    expect(corroborationCount(retryIdeas[0]!)).toBe(1);
    const src = retryIdeas[0]!.sources[0]!;
    expect(src.sessionId).toBe('s-1');
    expect(src.segmentId).toBe('seg-1');
  });

  it('does not move when re-evaluating to the SAME skill (idempotent, no self-strip)', async () => {
    await finalizeRoute(routed({ skillBaseName: 'hq-money', finding: topic() }), deps());
    const res = await finalizeRoute(
      routed({ skillBaseName: 'hq-money', finding: topic() }),
      deps(),
    );

    expect(res.movedFrom).toEqual([]);
    const ideas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(ideas).toHaveLength(1);
    // Same session re-emitted → still counted exactly once (AE4 no-op, no self-strip).
    expect(corroborationCount(ideas[0]!)).toBe(1);
  });

  it('leaves OTHER sessions on the prior skill untouched when one drifts away', async () => {
    // Two distinct sessions corroborate the money idea.
    await finalizeRoute(
      routed({ skillBaseName: 'hq-money', finding: topic({ sessionId: 's-1' }) }),
      deps(),
    );
    await finalizeRoute(
      routed({
        skillBaseName: 'hq-money',
        finding: topic({ sessionId: 's-2', segmentId: 'seg-9' }),
      }),
      deps(),
    );
    let moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(corroborationCount(moneyIdeas[0]!)).toBe(2);

    // Only s-1 drifts to hq-retry.
    await finalizeRoute(
      routed({
        skillBaseName: 'hq-retry',
        finding: topic({ sessionId: 's-1', description: 'Retry with backoff and idempotency.' }),
      }),
      deps(),
    );

    moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    // s-1 stripped, s-2 stays — exactly one session left on money.
    expect(corroborationCount(moneyIdeas[0]!)).toBe(1);
    expect(moneyIdeas[0]!.sources[0]!.sessionId).toBe('s-2');
  });

  it('does not strip a session out of a FOLDED prior idea (U20 immutable history)', async () => {
    await finalizeRoute(routed({ skillBaseName: 'hq-money', finding: topic() }), deps());
    const moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    const folded = { ...moneyIdeas[0]!, status: 'folded' as const, foldedIntoRev: 2 };
    await repo.putIdea(folded);

    // Same session re-evaluates to hq-retry — the folded money idea must keep its source.
    const res = await finalizeRoute(
      routed({
        skillBaseName: 'hq-retry',
        finding: topic({ description: 'Retry with backoff.' }),
      }),
      deps(),
    );

    expect(res.movedFrom).toEqual([]); // folded idea was not touched.
    const after = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(after[0]!.status).toBe('folded');
    expect(corroborationCount(after[0]!)).toBe(1); // unchanged.
  });
});

describe('associateAndFinalize — full pipeline (U8→U9→U10)', () => {
  it('routes a clear match and creates the idea on the chosen skill', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: ORG });
    await seedSkill(ORG, 'hq-money', 'Handling money and currency in code.');
    // Pre-seed the skill vector index so retrieval surfaces hq-money above the floor.
    await vectors.putVectors('skills', [
      {
        key: `${ORG}#hq-money`,
        vector: vectorFor('money'),
        metadata: { org: ORG, skillBaseName: 'hq-money', embeddingVersion: 'fake-v1' },
      },
    ]);
    const judge = new FakeJudge({ outcome: 'best', skillBaseName: 'hq-money', confidence: 0.9 });

    const res = await associateAndFinalize(topic(), deps(judge));

    expect(res.route.outcome).toBe('routed');
    expect(res.route.skillBaseName).toBe('hq-money');
    expect(res.finalize?.corroboration.created).toBe(true);
    const ideas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(ideas).toHaveLength(1);
    // Provenance from the resolved project rode through onto the source (R15).
    expect(ideas[0]!.sources[0]!.projectId).toBe('p-1');
    expect(ideas[0]!.sources[0]!.repoId).toBe(`gh/${ORG}/p-1`);
  });

  it('does not finalize when the route is unassigned (bin path, no idea)', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: ORG });
    // No skill vectors seeded → nothing clears the floor → bin.
    const judge = new FakeJudge({ outcome: 'none' });

    const res = await associateAndFinalize(topic(), deps(judge));

    expect(res.route.outcome).toBe('unassigned');
    expect(res.finalize).toBeUndefined();
    expect(await repo.listIdeasForOrg(ORG)).toEqual([]);
  });
});
