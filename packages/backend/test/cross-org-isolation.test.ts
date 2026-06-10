import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { QueryVectorsCommand, S3VectorsClient } from '@aws-sdk/client-s3vectors';
import type { Idea } from '@harness/shared';
import {
  ideaKey,
  ideaPrefixForSkill,
  ideaPrefixForOrg,
  unassignedBinKey,
  unassignedBinPrefix,
  ORG_SCOPE_PREFIX,
} from '../src/db/keys.js';
import { memRepoHarness } from './helpers/memtable.js';
import { bodyOf, deviceTokenEvent, httpEvent } from './helpers/httpevent.js';
import {
  FakeIdeaVectors,
  FakeSkillVectors as FakeVectors,
  FakeWriter,
  fakeEmbedder,
  seedSession as seedSessionFor,
} from './helpers/idea-fakes.js';
import { makeBinEntry, makeIdea } from './helpers/factories.js';
import {
  CORROBORATION_K,
  type IdeaWithCorroboration,
  type UnassignedEntryWithFrequency,
  resolveCandidateLearnings,
  resolveSkillIdeas,
  resolveUnassignedBin,
} from '../src/rest/ideas.js';
import {
  associateTopic,
  routeTopic,
  type AssociateDeps,
  type TopicFinding,
} from '../src/ideas/associate.js';
import { corroborateFinding, type Finding } from '../src/ideas/corroborate.js';
import { S3Vectors } from '../src/embeddings/s3vectors.js';
import {
  IDEA_VECTOR_INDEX,
  SKILL_VECTOR_INDEX,
  type S3Vectors as S3VectorsType,
} from '../src/embeddings/s3vectors.js';
import type { IdeaWriter } from '../src/ideas/synth.js';
import type { RerankJudge } from '../src/rerank/judge.js';

/**
 * U22 — CROSS-ORG ISOLATION (F2 data isolation). The verification unit for the
 * skill-idea loop: one org's ideas / skill-vectors / unassigned-bin must NEVER
 * reach another org's sessions or reads. The other units each added an isolation
 * guard; this file is the explicit, end-to-end ASSERTION that they hold across
 * the three surfaces the plan names — association, the REST reads, and the
 * embedding query — plus the one guard added here (corroborate's blank-org
 * refusal).
 *
 * The four invariants asserted, per the plan:
 *  1. Every idea / bin record is `SCOPE#org#<org>` partitioned (key-level).
 *  2. Every S3 Vectors `queryTopK` carries the `org` filter (skill + idea index).
 *  3. The candidate-learnings / all-ideas / unassigned-bin endpoints scope by
 *     `effectiveOrg` (never `principal.org`), and a missing/blank org returns
 *     nothing — never a wrong-org default.
 *  4. Org A's topic never retrieves org B's skills; org A's reads never return
 *     org B's ideas/bin entries; a blank org is REJECTED, not defaulted.
 */

const { repo } = memRepoHarness();
const deps = { repo };

const ORG_A = 'org-a';
const ORG_B = 'org-b';
const SKILL = 'reconcile';

// ───────────────────────────────────────────────────────────────────────────
// 1. KEY-LEVEL PARTITIONING — every idea / bin record is `SCOPE#org#<org>`.
// ───────────────────────────────────────────────────────────────────────────

describe('U22 — every idea/bin record is SCOPE#org# partitioned', () => {
  it('idea keys partition on SCOPE#org#<org> and never collide across orgs', () => {
    const a = ideaKey(ORG_A, SKILL, 'i-1');
    const b = ideaKey(ORG_B, SKILL, 'i-1');
    expect(a.PK).toBe(`${ORG_SCOPE_PREFIX}${ORG_A}`);
    expect(b.PK).toBe(`${ORG_SCOPE_PREFIX}${ORG_B}`);
    // Same skill + same ideaId in two orgs are DIFFERENT physical rows.
    expect(a.PK).not.toBe(b.PK);
    expect(a.SK).toBe(b.SK); // SK is org-independent; the PK is the isolation axis.
  });

  it('the per-skill and per-org idea read prefixes are pinned to one org partition', () => {
    expect(ideaPrefixForSkill(ORG_A, SKILL).PK).toBe(`${ORG_SCOPE_PREFIX}${ORG_A}`);
    expect(ideaPrefixForOrg(ORG_A).PK).toBe(`${ORG_SCOPE_PREFIX}${ORG_A}`);
    // A read scoped to org A can never structurally reach org B's partition.
    expect(ideaPrefixForSkill(ORG_A, SKILL).PK).not.toBe(ideaPrefixForSkill(ORG_B, SKILL).PK);
  });

  it('unassigned-bin keys/prefixes partition on SCOPE#org#<org>', () => {
    expect(unassignedBinKey(ORG_A, 'e-1').PK).toBe(`${ORG_SCOPE_PREFIX}${ORG_A}`);
    expect(unassignedBinPrefix(ORG_A).PK).toBe(`${ORG_SCOPE_PREFIX}${ORG_A}`);
    expect(unassignedBinPrefix(ORG_A).PK).not.toBe(unassignedBinPrefix(ORG_B).PK);
  });

  it('a put under org A is invisible to an org-B partition read (round-trip isolation)', async () => {
    await repo.putIdea(makeIdea('a-only', { org: ORG_A, sessions: 2 }));
    await repo.putIdea(makeIdea('b-only', { org: ORG_B, sessions: 2 }));
    await repo.putUnassigned(makeBinEntry('bin-a', { org: ORG_A, sessions: 1 }));
    await repo.putUnassigned(makeBinEntry('bin-b', { org: ORG_B, sessions: 1 }));

    expect((await repo.listIdeasForOrg(ORG_A)).map((i) => i.ideaId)).toEqual(['a-only']);
    expect((await repo.listIdeasForOrg(ORG_B)).map((i) => i.ideaId)).toEqual(['b-only']);
    expect((await repo.listIdeasForSkill(ORG_A, SKILL)).map((i) => i.ideaId)).toEqual(['a-only']);
    expect((await repo.listUnassignedForOrg(ORG_A)).map((e) => e.entryId)).toEqual(['bin-a']);
    expect((await repo.listUnassignedForOrg(ORG_B)).map((e) => e.entryId)).toEqual(['bin-b']);
  });
});

// ───────────────────────────────────────────────────────────────────────────
// 2 + 4 (association). Org A's topic never retrieves org B's skills, and the
//   skill-index query always carries the org filter.
// ───────────────────────────────────────────────────────────────────────────

function assocDeps(vectors: FakeVectors, over: Partial<AssociateDeps> = {}): AssociateDeps {
  return { repo, embedder: fakeEmbedder, vectors: vectors as unknown as S3VectorsType, ...over };
}

const seedSession = (opts: { sessionId: string; projectId: string; org?: string }) =>
  seedSessionFor(repo, opts);

function topic(over: Partial<TopicFinding> = {}): TopicFinding {
  return {
    sessionId: 's-A',
    segmentId: 'seg-1',
    topicLabel: 'decimal money handling',
    description: 'Always use a decimal type for currency, never a float.',
    seq: 3,
    ...over,
  };
}

describe('U22 — association: org A topic never retrieves org B skills', () => {
  it('only org B has a (strong) skill → org A retrieves nothing; the query is org-A scoped', async () => {
    await seedSession({ sessionId: 's-A', projectId: 'p-A', org: ORG_A });
    const vectors = new FakeVectors();
    // A near-perfect match — but it belongs to org B. It must NEVER reach org A.
    vectors.seed({ org: ORG_B, skillBaseName: 'b-only-skill', score: 0.99 });

    const res = await associateTopic(topic(), assocDeps(vectors));

    expect(res.org).toBe(ORG_A);
    // The skill-index query carried the org filter, scoped to the RESOLVED org.
    expect(vectors.queries[0]!.opts.orgFilter).toBe(ORG_A);
    // org B's strong skill is invisible → no candidates → bin seam.
    expect(res.outcome).toBe('unassigned');
    expect(res.candidates).toEqual([]);
  });

  it('every skill-index queryTopK on the retrieval path carries the org filter', async () => {
    await seedSession({ sessionId: 's-A', projectId: 'p-A', org: ORG_A });
    const vectors = new FakeVectors();
    vectors.seed({ org: ORG_A, skillBaseName: 'a-skill', score: 0.9 });

    await associateTopic(topic(), assocDeps(vectors));

    expect(vectors.queries).toHaveLength(1);
    expect(vectors.queries[0]!.opts.orgFilter).toBe(ORG_A);
    // It was the SKILL index (not the ideas index) that was queried for retrieval.
    expect(vectors.queries[0]!.index).toBe(SKILL_VECTOR_INDEX);
    expect(SKILL_VECTOR_INDEX).toBe('skills');
  });

  it('resolves the org from the project, NOT a caller-supplied org — a foreign org is unreachable', async () => {
    // The session's project is stamped org A. There is NO principal here, and even
    // if a caller tried to inject org B it cannot: associate resolves via the
    // project pointer only.
    await seedSession({ sessionId: 's-A', projectId: 'p-A', org: ORG_A });
    const vectors = new FakeVectors();
    vectors.seed({ org: ORG_A, skillBaseName: 'a-skill', score: 0.9 });
    vectors.seed({ org: ORG_B, skillBaseName: 'b-skill', score: 0.99 });

    const res = await routeTopic(topic(), assocDeps(vectors, { judge: alwaysFirstJudge }));

    expect(res.org).toBe(ORG_A);
    expect(vectors.queries.every((q) => q.opts.orgFilter === ORG_A)).toBe(true);
    // Routed to org A's skill, never org B's stronger one.
    expect(res.outcome).toBe('routed');
    expect(res.skillBaseName).toBe('a-skill');
  });

  it('a session whose project carries NO stamped org is unresolved — never a wrong-org guess', async () => {
    await seedSession({ sessionId: 's-A', projectId: 'p-A' }); // no org stamped
    const vectors = new FakeVectors();
    vectors.seed({ org: ORG_B, skillBaseName: 'b-skill', score: 0.99 });

    const res = await associateTopic(topic(), assocDeps(vectors));

    expect(res.outcome).toBe('unresolved');
    expect(res.org).toBeUndefined();
    // We never queried — there was no org to scope to (so org B is never touched).
    expect(vectors.queries).toHaveLength(0);
  });
});

/** A judge that picks the first candidate (deterministic for the route test). */
const alwaysFirstJudge = {
  async judge(_topic: unknown, candidates: Array<{ skillBaseName: string }>) {
    const first = candidates[0];
    return first
      ? { outcome: 'best' as const, skillBaseName: first.skillBaseName, confidence: 0.99 }
      : { outcome: 'none' as const };
  },
} as unknown as RerankJudge;

// ───────────────────────────────────────────────────────────────────────────
// 2 + 4 (corroboration). The IDEA index query carries the org filter, and a
//   blank org is REJECTED (the guard added in this unit).
// ───────────────────────────────────────────────────────────────────────────

function corroDeps(vectors: FakeIdeaVectors) {
  return {
    repo,
    embedder: fakeEmbedder,
    vectors: vectors as unknown as S3VectorsType,
    writer: new FakeWriter() as unknown as IdeaWriter,
  };
}

function finding(over: Partial<Finding> = {}): Finding {
  return {
    org: ORG_A,
    skillBaseName: SKILL,
    content: { description: 'use decimal money types', implLearnings: ['never float for currency'] },
    sessionId: 's-1',
    segments: [{ segmentId: 'seg-1', seq: 1, snippet: 'raw' }],
    ...over,
  };
}

describe('U22 — corroboration: the idea-index query is org-filtered and writes stay in-org', () => {
  it('queries the IDEA index WITH the finding org filter', async () => {
    const vectors = new FakeIdeaVectors();
    await corroborateFinding(finding({ org: ORG_A }), corroDeps(vectors));
    expect(vectors.queries).toHaveLength(1);
    expect(vectors.queries[0]!.orgFilter).toBe(ORG_A);
  });

  it("org B's idea on the same skill never merges into org A's finding (separate orgs, separate ideas)", async () => {
    const vectors = new FakeIdeaVectors();
    // Org B already has an identical-lesson idea vector. An org A finding of the
    // SAME lesson must NOT merge into it — the org filter keeps them apart.
    await corroborateFinding(finding({ org: ORG_B, sessionId: 's-b1' }), corroDeps(vectors));
    const beforeB = await repo.listIdeasForSkill(ORG_B, SKILL);
    expect(beforeB).toHaveLength(1);

    const res = await corroborateFinding(finding({ org: ORG_A, sessionId: 's-a1' }), corroDeps(vectors));

    expect(res.created).toBe(true); // a NEW idea in org A, not a merge into org B's
    const aIdeas = await repo.listIdeasForSkill(ORG_A, SKILL);
    const bIdeas = await repo.listIdeasForSkill(ORG_B, SKILL);
    expect(aIdeas).toHaveLength(1);
    expect(bIdeas).toHaveLength(1);
    expect(aIdeas[0]!.ideaId).not.toBe(bIdeas[0]!.ideaId);
  });

  it('REJECTS a blank/whitespace org rather than querying/writing an unscoped partition (guard)', async () => {
    const vectors = new FakeIdeaVectors();
    await expect(corroborateFinding(finding({ org: '' }), corroDeps(vectors))).rejects.toThrow(
      /non-blank org/,
    );
    await expect(corroborateFinding(finding({ org: '   ' }), corroDeps(vectors))).rejects.toThrow(
      /non-blank org/,
    );
    // It never touched the index or the table.
    expect(vectors.queries).toHaveLength(0);
  });
});

// ───────────────────────────────────────────────────────────────────────────
// 2 (embedding query, real client). queryTopK ALWAYS forwards the org filter
//   verbatim, and omits it only when truly none is given.
// ───────────────────────────────────────────────────────────────────────────

describe('U22 — S3 Vectors queryTopK forwards the org filter to the service', () => {
  const s3vMock = mockClient(S3VectorsClient);
  const store = new S3Vectors(new S3VectorsClient({ region: 'us-east-1' }), 'test-bucket');

  beforeEach(() => s3vMock.reset());

  it('passes { org } as the service-side filter on the skill index', async () => {
    s3vMock.on(QueryVectorsCommand).resolves({ distanceMetric: 'cosine', vectors: [] });
    await store.queryTopK(SKILL_VECTOR_INDEX, Array(1024).fill(0.1), 5, { orgFilter: ORG_A });
    const call = s3vMock.commandCalls(QueryVectorsCommand)[0]!.args[0].input;
    expect(call.filter).toEqual({ org: ORG_A });
  });

  it('passes { org } as the service-side filter on the idea index', async () => {
    s3vMock.on(QueryVectorsCommand).resolves({ distanceMetric: 'cosine', vectors: [] });
    await store.queryTopK(IDEA_VECTOR_INDEX, Array(1024).fill(0.1), 5, { orgFilter: ORG_B });
    const call = s3vMock.commandCalls(QueryVectorsCommand)[0]!.args[0].input;
    expect(call.filter).toEqual({ org: ORG_B });
  });
});

// ───────────────────────────────────────────────────────────────────────────
// 3 + 4 (REST reads). The endpoints scope by effectiveOrg (never principal.org),
//   and a missing/blank org returns nothing — never a wrong-org default.
// ───────────────────────────────────────────────────────────────────────────

function getEvent(name: string, org: string, userId: string | null = 'matt') {
  return httpEvent({ method: 'GET', userId, org, path: { name } });
}
function binEvent(org: string, userId: string | null = 'matt') {
  return httpEvent({ method: 'GET', userId, org, rawPath: '/ideas/unassigned' });
}

describe('U22 — REST reads never leak across orgs and reject a blank org', () => {
  beforeEach(async () => {
    await repo.putIdea(makeIdea('a-strong', { org: ORG_A, sessions: CORROBORATION_K }));
    await repo.putIdea(makeIdea('a-weak', { org: ORG_A, sessions: 1 }));
    await repo.putIdea(makeIdea('b-strong', { org: ORG_B, sessions: CORROBORATION_K + 3 }));
    await repo.putUnassigned(makeBinEntry('a-bin', { org: ORG_A, sessions: 1 }));
    await repo.putUnassigned(makeBinEntry('b-bin', { org: ORG_B, sessions: 5 }));
  });

  it('candidate-learnings: org A never returns org B corroborated ideas', async () => {
    const res = await resolveCandidateLearnings(getEvent(SKILL, ORG_A), deps);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res);
    expect(learnings.map((i) => i.ideaId)).toEqual(['a-strong']);
  });

  it('all-ideas: org A never returns org B ideas', async () => {
    const res = await resolveSkillIdeas(getEvent(SKILL, ORG_A), deps);
    const { ideas } = bodyOf<{ ideas: IdeaWithCorroboration[] }>(res);
    expect(ideas.map((i) => i.ideaId).sort()).toEqual(['a-strong', 'a-weak']);
  });

  it('unassigned-bin: org A never returns org B bin entries', async () => {
    const res = await resolveUnassignedBin(binEvent(ORG_A), deps);
    const { entries } = bodyOf<{ entries: UnassignedEntryWithFrequency[] }>(res);
    expect(entries.map((e) => e.entryId)).toEqual(['a-bin']);
  });

  it('scopes by the EFFECTIVE (profile) org, NOT the raw token org — token A, profile B sees only B', async () => {
    // The token claims org A, but the caller's profile membership is org B. Every
    // read must follow the PROFILE (effectiveOrg), so it sees org B's data and
    // never the org named in the token claim.
    await repo.putUser({ userId: 'matt', org: ORG_B });

    const cl = await resolveCandidateLearnings(getEvent(SKILL, ORG_A), deps);
    expect(bodyOf<{ learnings: Idea[] }>(cl).learnings.map((i) => i.ideaId)).toEqual([
      'b-strong',
    ]);

    const all = await resolveSkillIdeas(getEvent(SKILL, ORG_A), deps);
    expect(bodyOf<{ ideas: IdeaWithCorroboration[] }>(all).ideas.map((i) => i.ideaId)).toEqual([
      'b-strong',
    ]);

    const bin = await resolveUnassignedBin(binEvent(ORG_A), deps);
    expect(bodyOf<{ entries: UnassignedEntryWithFrequency[] }>(bin).entries.map((e) => e.entryId)).toEqual([
      'b-bin',
    ]);
  });

  it('a BLANK org (no profile, blank token claim) returns nothing — never another org default', async () => {
    // Authenticated (a principal exists) but org-less: effectiveOrg is blank, so
    // the `!org` guard returns an empty result rather than defaulting into ANY
    // org's data. This is the "no/blank org returns nothing, never cross-org leak"
    // scenario — proven against a table that DOES hold org A + org B records.
    const cl = await resolveCandidateLearnings(getEvent(SKILL, ''), deps);
    expect(bodyOf<{ learnings: Idea[] }>(cl).learnings).toEqual([]);

    const all = await resolveSkillIdeas(getEvent(SKILL, ''), deps);
    expect(bodyOf<{ ideas: IdeaWithCorroboration[] }>(all).ideas).toEqual([]);

    const bin = await resolveUnassignedBin(binEvent(''), deps);
    expect(bodyOf<{ entries: UnassignedEntryWithFrequency[] }>(bin).entries).toEqual([]);
  });

  it('an unauthenticated request (no principal) is rejected 401, not defaulted to an org', async () => {
    expect(await resolveCandidateLearnings(getEvent(SKILL, ORG_A, null), deps)).toMatchObject({ statusCode: 401 });
    expect(await resolveSkillIdeas(getEvent(SKILL, ORG_A, null), deps)).toMatchObject({ statusCode: 401 });
    expect(await resolveUnassignedBin(binEvent(ORG_A, null), deps)).toMatchObject({ statusCode: 401 });
  });

  it('a device token for org A reads ONLY org A (cross-org isolation through the wrapper path)', async () => {
    const event = await deviceTokenEvent({
      method: 'GET',
      userId: 'wrapper-user',
      org: ORG_A,
      path: { name: SKILL },
    });
    const res = await resolveCandidateLearnings(event, deps);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res);
    expect(learnings.map((i) => i.ideaId)).toEqual(['a-strong']);
  });
});
