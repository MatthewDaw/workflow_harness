import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Idea, IdeaSource } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { signDeviceToken } from '../src/auth/verify.js';
import {
  CANDIDATE_CAP,
  CORROBORATION_K,
  type IdeaWithCorroboration,
  resolveCandidateLearnings,
  resolveSkillIdeas,
} from '../src/rest/ideas.js';
import { handler as ideasHandler } from '../src/rest/ideas.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * GET /skills/{name}/candidate-learnings (U11). The endpoint serves ONLY
 * corroborated (`>= K` distinct sessions), open (non-folded) ideas, ranked by
 * corroboration then recency and capped at N. The gate is a server-side security
 * boundary: uncorroborated ideas never leave the backend on this path. Org is the
 * effective (PROFILE-driven) org, so an org-A read never sees org-B ideas.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');
const deps = { repo };

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const ORG = 'acme';
const SKILL = 'reconcile';

let seq = 0;
function source(sessionId: string): IdeaSource {
  return { sessionId, segmentId: `${sessionId}-seg`, seq: seq++, snippet: '' };
}

/**
 * An idea with `sessions` DISTINCT sessions. `segments` extra sources reuse the
 * FIRST session id, so they must NOT raise the corroboration count (the unit is
 * the distinct session, deduped across segments).
 */
function idea(
  ideaId: string,
  opts: {
    sessions: number;
    status?: Idea['status'];
    org?: string;
    skillBaseName?: string;
    updatedAt?: number;
    extraSegmentsOnFirst?: number;
    foldedIntoRev?: number;
  },
): Idea {
  const sources: IdeaSource[] = [];
  for (let i = 0; i < opts.sessions; i++) sources.push(source(`${ideaId}-sess-${i}`));
  for (let i = 0; i < (opts.extraSegmentsOnFirst ?? 0); i++) {
    // Reuse the first session id with a fresh segment — same session, more segments.
    sources.push({ sessionId: `${ideaId}-sess-0`, segmentId: `extra-${i}`, seq: seq++, snippet: '' });
  }
  return {
    ideaId,
    skillBaseName: opts.skillBaseName ?? SKILL,
    org: opts.org ?? ORG,
    text: `lesson ${ideaId}`,
    sources,
    status: opts.status ?? 'open',
    ...(opts.foldedIntoRev !== undefined ? { foldedIntoRev: opts.foldedIntoRev } : {}),
    corroborationVersion: 0,
    createdAt: 1,
    updatedAt: opts.updatedAt ?? 1,
  };
}

function getEvent(name: string, org = ORG, userId: string | null = 'matt') {
  return httpEvent({ method: 'GET', userId, org, path: { name } });
}

describe('GET /skills/{name}/candidate-learnings', () => {
  it('returns only ideas corroborated by >= K distinct sessions', async () => {
    await repo.putIdea(idea('strong', { sessions: CORROBORATION_K })); // exactly K → included
    await repo.putIdea(idea('weak', { sessions: CORROBORATION_K - 1 })); // below K → excluded
    const res = await resolveCandidateLearnings(getEvent(SKILL), deps);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res as { body: string });
    expect(learnings.map((i) => i.ideaId)).toEqual(['strong']);
  });

  it('counts the distinct SESSION, not segments — extra segments do not corroborate', async () => {
    // One session, many segments: still 1 distinct session → below K, excluded.
    await repo.putIdea(idea('multiseg', { sessions: 1, extraSegmentsOnFirst: 5 }));
    const res = await resolveCandidateLearnings(getEvent(SKILL), deps);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res as { body: string });
    expect(learnings).toEqual([]);
  });

  it('excludes folded ideas even when corroborated', async () => {
    await repo.putIdea(idea('open-one', { sessions: CORROBORATION_K }));
    await repo.putIdea(idea('folded-one', { sessions: CORROBORATION_K + 3, status: 'folded' }));
    const res = await resolveCandidateLearnings(getEvent(SKILL), deps);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res as { body: string });
    expect(learnings.map((i) => i.ideaId)).toEqual(['open-one']);
  });

  it('orders by corroboration desc, then recency desc, and respects the cap', async () => {
    // SIX corroborated candidates — one more than the cap — with a corroboration
    // tie at 2 (c2-old vs c2-new) that must break by updatedAt (fresher wins).
    await repo.putIdea(idea('c2-old', { sessions: 2, updatedAt: 100 }));
    await repo.putIdea(idea('c2-new', { sessions: 2, updatedAt: 200 }));
    await repo.putIdea(idea('c6', { sessions: 6, updatedAt: 1 }));
    await repo.putIdea(idea('c5', { sessions: 5, updatedAt: 1 }));
    await repo.putIdea(idea('c4', { sessions: 4, updatedAt: 1 }));
    await repo.putIdea(idea('c3', { sessions: 3, updatedAt: 1 }));
    const res = await resolveCandidateLearnings(getEvent(SKILL), deps);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res as { body: string });
    // Sorted strongest-first; the tie at 2 favors the fresher (c2-new before c2-old);
    // capped at CANDIDATE_CAP (5) so the weakest of the six (c2-old) is dropped.
    expect(CANDIDATE_CAP).toBe(5);
    expect(learnings.map((i) => i.ideaId)).toEqual(['c6', 'c5', 'c4', 'c3', 'c2-new']);
  });

  it('returns empty for a skill whose ideas are all uncorroborated', async () => {
    await repo.putIdea(idea('a', { sessions: 1 }));
    await repo.putIdea(idea('b', { sessions: 1 }));
    const res = await resolveCandidateLearnings(getEvent(SKILL), deps);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res as { body: string });
    expect(learnings).toEqual([]);
  });

  it('is org-scoped — org A never sees org B corroborated ideas', async () => {
    await repo.putIdea(idea('mine', { sessions: CORROBORATION_K, org: ORG }));
    await repo.putIdea(idea('theirs', { sessions: CORROBORATION_K + 2, org: 'other-org' }));
    const res = await resolveCandidateLearnings(getEvent(SKILL, ORG), deps);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res as { body: string });
    expect(learnings.map((i) => i.ideaId)).toEqual(['mine']);
  });

  it('scopes by the EFFECTIVE org (profile.org), not the raw token org', async () => {
    // Token says org A, but the caller's profile membership is org B — the read
    // must follow the profile, exactly like every other org read.
    await repo.putUser({ userId: 'matt', org: 'profile-org' });
    await repo.putIdea(idea('in-profile-org', { sessions: CORROBORATION_K, org: 'profile-org' }));
    await repo.putIdea(idea('in-token-org', { sessions: CORROBORATION_K, org: ORG }));
    const res = await resolveCandidateLearnings(getEvent(SKILL, ORG), deps);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res as { body: string });
    expect(learnings.map((i) => i.ideaId)).toEqual(['in-profile-org']);
  });

  it('401s an unauthenticated request', async () => {
    const res = await resolveCandidateLearnings(getEvent(SKILL, ORG, null), deps);
    expect(res).toMatchObject({ statusCode: 401 });
  });

  it('400s when the skill name path param is missing', async () => {
    const res = await resolveCandidateLearnings(httpEvent({ method: 'GET', userId: 'matt', org: ORG }), deps);
    expect(res).toMatchObject({ statusCode: 400 });
  });
});

describe('candidate-learnings via device token (claude+ wrapper, noAuth route)', () => {
  const SECRET = new TextEncoder().encode('test-device-secret');

  beforeEach(() => {
    process.env.DEVICE_TOKEN_SECRET = 'test-device-secret';
  });

  it('accepts the HS256 device token in-handler and gates server-side', async () => {
    await repo.putIdea(idea('strong', { sessions: CORROBORATION_K, org: ORG }));
    await repo.putIdea(idea('weak', { sessions: 1, org: ORG }));
    const token = await signDeviceToken({ userId: 'matt', org: ORG }, { secret: SECRET });
    const event = httpEvent({
      method: 'GET',
      userId: null, // no Cognito claims — only the bearer device token
      headers: { authorization: `Bearer ${token}` },
      path: { name: SKILL },
    });
    const res = await ideasHandler(event);
    const { learnings } = bodyOf<{ learnings: Idea[] }>(res as { body: string });
    expect(learnings.map((i) => i.ideaId)).toEqual(['strong']);
  });
});

/**
 * GET /skills/{name}/ideas (U13/R18). The Command HQ surface: EVERY idea for the
 * skill regardless of status — corroborated, uncorroborated, and folded history
 * alike — each decorated with its derived corroboration count, ordered
 * strongest-and-freshest-first. No gate, no cap: unlike candidate-learnings, this
 * is a human read, not session-injected. Folded ideas carry `foldedIntoRev`, and
 * the read is org-scoped via the effective (PROFILE-driven) org.
 */
describe('GET /skills/{name}/ideas (all ideas, U13)', () => {
  it('returns ideas of EVERY status: corroborated, uncorroborated, and folded', async () => {
    await repo.putIdea(idea('corroborated', { sessions: CORROBORATION_K }));
    await repo.putIdea(idea('uncorroborated', { sessions: 1 }));
    await repo.putIdea(idea('folded', { sessions: CORROBORATION_K, status: 'folded', foldedIntoRev: 3 }));
    const res = await resolveSkillIdeas(getEvent(SKILL), deps);
    const { ideas } = bodyOf<{ ideas: IdeaWithCorroboration[] }>(res as { body: string });
    expect(ideas.map((i) => i.ideaId).sort()).toEqual(['corroborated', 'folded', 'uncorroborated']);
  });

  it('carries the corroboration count (distinct sessions, deduped across segments) on each idea', async () => {
    await repo.putIdea(idea('three', { sessions: 3, extraSegmentsOnFirst: 4 })); // extra segments do NOT raise the count
    await repo.putIdea(idea('one', { sessions: 1 }));
    const res = await resolveSkillIdeas(getEvent(SKILL), deps);
    const { ideas } = bodyOf<{ ideas: IdeaWithCorroboration[] }>(res as { body: string });
    const byId = Object.fromEntries(ideas.map((i) => [i.ideaId, i.corroborationCount]));
    expect(byId).toEqual({ three: 3, one: 1 });
  });

  it('orders by corroboration desc, then recency desc — no cap (every idea returned)', async () => {
    // SIX ideas, more than CANDIDATE_CAP, to prove there is no cap on this path.
    await repo.putIdea(idea('c2-old', { sessions: 2, updatedAt: 100 }));
    await repo.putIdea(idea('c2-new', { sessions: 2, updatedAt: 200 }));
    await repo.putIdea(idea('c6', { sessions: 6, updatedAt: 1 }));
    await repo.putIdea(idea('c5', { sessions: 5, updatedAt: 1 }));
    await repo.putIdea(idea('c4', { sessions: 4, updatedAt: 1 }));
    await repo.putIdea(idea('c1', { sessions: 1, updatedAt: 1 }));
    const res = await resolveSkillIdeas(getEvent(SKILL), deps);
    const { ideas } = bodyOf<{ ideas: IdeaWithCorroboration[] }>(res as { body: string });
    expect(ideas).toHaveLength(6); // CANDIDATE_CAP is 5; the all-ideas path is uncapped.
    expect(ideas.map((i) => i.ideaId)).toEqual(['c6', 'c5', 'c4', 'c2-new', 'c2-old', 'c1']);
  });

  it('folded ideas carry foldedIntoRev and their status', async () => {
    await repo.putIdea(idea('done', { sessions: CORROBORATION_K, status: 'folded', foldedIntoRev: 7 }));
    const res = await resolveSkillIdeas(getEvent(SKILL), deps);
    const { ideas } = bodyOf<{ ideas: IdeaWithCorroboration[] }>(res as { body: string });
    expect(ideas).toHaveLength(1);
    expect(ideas[0]).toMatchObject({ ideaId: 'done', status: 'folded', foldedIntoRev: 7 });
  });

  it('carries provenance (sources) for the history view', async () => {
    await repo.putIdea(idea('p', { sessions: 2 }));
    const res = await resolveSkillIdeas(getEvent(SKILL), deps);
    const { ideas } = bodyOf<{ ideas: IdeaWithCorroboration[] }>(res as { body: string });
    expect(ideas[0]!.sources).toHaveLength(2);
    expect(ideas[0]!.sources[0]).toMatchObject({ sessionId: 'p-sess-0', segmentId: 'p-sess-0-seg' });
  });

  it('is org-scoped — org A never sees org B ideas (no cross-org leak)', async () => {
    await repo.putIdea(idea('mine-open', { sessions: 1, org: ORG }));
    await repo.putIdea(idea('mine-folded', { sessions: 1, org: ORG, status: 'folded', foldedIntoRev: 2 }));
    await repo.putIdea(idea('theirs', { sessions: 9, org: 'other-org' }));
    const res = await resolveSkillIdeas(getEvent(SKILL, ORG), deps);
    const { ideas } = bodyOf<{ ideas: IdeaWithCorroboration[] }>(res as { body: string });
    expect(ideas.map((i) => i.ideaId).sort()).toEqual(['mine-folded', 'mine-open']);
  });

  it('scopes by the EFFECTIVE org (profile.org), not the raw token org', async () => {
    await repo.putUser({ userId: 'matt', org: 'profile-org' });
    await repo.putIdea(idea('in-profile-org', { sessions: 1, org: 'profile-org' }));
    await repo.putIdea(idea('in-token-org', { sessions: 1, org: ORG }));
    const res = await resolveSkillIdeas(getEvent(SKILL, ORG), deps);
    const { ideas } = bodyOf<{ ideas: IdeaWithCorroboration[] }>(res as { body: string });
    expect(ideas.map((i) => i.ideaId)).toEqual(['in-profile-org']);
  });

  it('401s an unauthenticated request', async () => {
    const res = await resolveSkillIdeas(getEvent(SKILL, ORG, null), deps);
    expect(res).toMatchObject({ statusCode: 401 });
  });

  it('400s when the skill name path param is missing', async () => {
    const res = await resolveSkillIdeas(httpEvent({ method: 'GET', userId: 'matt', org: ORG }), deps);
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('dispatches the /ideas suffix to the all-ideas path via the handler (device token)', async () => {
    process.env.DEVICE_TOKEN_SECRET = 'test-device-secret';
    const SECRET = new TextEncoder().encode('test-device-secret');
    await repo.putIdea(idea('open-one', { sessions: 1, org: ORG }));
    await repo.putIdea(idea('folded-one', { sessions: 1, org: ORG, status: 'folded', foldedIntoRev: 1 }));
    const token = await signDeviceToken({ userId: 'matt', org: ORG }, { secret: SECRET });
    const event = httpEvent({
      method: 'GET',
      userId: null,
      rawPath: `/skills/${SKILL}/ideas`,
      headers: { authorization: `Bearer ${token}` },
      path: { name: SKILL },
    });
    const res = await ideasHandler(event);
    const { ideas } = bodyOf<{ ideas: IdeaWithCorroboration[] }>(res as { body: string });
    // Both statuses returned — proves the handler routed to all-ideas, not the gated path.
    expect(ideas.map((i) => i.ideaId).sort()).toEqual(['folded-one', 'open-one']);
  });
});
