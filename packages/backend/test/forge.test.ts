import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { ScoredSession, SessionVector } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { summarizeAndEmbed, type Embedder, type Summarizer } from '../src/forge/embed.js';
import { cosineSimilarity, rankByCosine, searchSimilarSessions } from '../src/forge/search.js';
import {
  aggregateFrequencies,
  proposeAgent,
  splitConfidence,
  type AgentDrafter,
} from '../src/forge/propose.js';

/**
 * U27 Forge. Embedding-on-done storage, ranking relevance (migration query ranks
 * migration sessions above unrelated ones), skill-frequency aggregation, the
 * full proposal flow, and the not-enough-history edge case. Bedrock is mocked
 * via deterministic fake Embedder/Summarizer/Drafter — no network.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

/**
 * A deterministic keyword embedder: each text becomes a bag-of-words vector over
 * a fixed vocabulary, so semantically-similar texts (sharing keywords) score
 * high under cosine. Good enough to assert ranking relevance without a model.
 */
const VOCAB = ['migration', 'schema', 'database', 'css', 'button', 'layout'];
const keywordEmbedder: Embedder = {
  async embed(text: string): Promise<number[]> {
    const lower = text.toLowerCase();
    return VOCAB.map((w) => (lower.includes(w) ? 1 : 0));
  },
};

const echoSummarizer: Summarizer = {
  async summarize(transcript: string): Promise<string> {
    return transcript.slice(0, 40);
  },
};

describe('cosine + ranking relevance', () => {
  it('cosine is 1 for identical, 0 for orthogonal vectors', () => {
    expect(cosineSimilarity([1, 0, 1], [1, 0, 1])).toBeCloseTo(1);
    expect(cosineSimilarity([1, 0], [0, 1])).toBe(0);
  });

  it('ranks migration sessions above unrelated ones for a migration query', async () => {
    const mk = (sessionId: string, summary: string, skills: string[]): SessionVector => ({
      sessionId,
      userId: 'matt',
      projectId: 'p1',
      summary,
      vector: VOCAB.map((w) => (summary.toLowerCase().includes(w) ? 1 : 0)),
      skills,
      tools: [],
      createdAt: 1,
    });
    await repo.putSessionVector(mk('s-mig1', 'database migration schema work', ['db-migrate']));
    await repo.putSessionVector(mk('s-mig2', 'schema migration for the database', ['db-migrate']));
    await repo.putSessionVector(mk('s-css', 'css button layout polish', ['frontend']));

    const results = await searchSimilarSessions(
      { userId: 'matt', description: 'run a database migration', k: 3 },
      { repo, embedder: keywordEmbedder },
    );
    expect(results[0]?.sessionId).toMatch(/^s-mig/);
    expect(results[1]?.sessionId).toMatch(/^s-mig/);
    // The CSS session ranks last (zero overlap).
    expect(results[2]?.sessionId).toBe('s-css');
    expect(results[2]?.score).toBe(0);
  });

  it('rankByCosine returns top-k by descending score', () => {
    const corpus: SessionVector[] = [
      {
        sessionId: 'a',
        userId: 'u',
        projectId: 'p',
        summary: '',
        vector: [1, 0],
        skills: [],
        tools: [],
        createdAt: 0,
      },
      {
        sessionId: 'b',
        userId: 'u',
        projectId: 'p',
        summary: '',
        vector: [0, 1],
        skills: [],
        tools: [],
        createdAt: 0,
      },
    ];
    const ranked = rankByCosine([1, 0], corpus, 1);
    expect(ranked).toHaveLength(1);
    expect(ranked[0]?.sessionId).toBe('a');
  });
});

describe('embed-on-done', () => {
  it('summarizes, embeds, and stores a session vector', async () => {
    const stored = await summarizeAndEmbed(
      {
        sessionId: 's-1',
        userId: 'matt',
        projectId: 'p1',
        transcript: 'database migration schema rewrite',
        skills: ['db-migrate'],
        tools: ['Bash'],
      },
      { repo, embedder: keywordEmbedder, summarizer: echoSummarizer, now: () => 123 },
    );
    expect(stored.vector[0]).toBe(1); // 'migration' present
    expect(stored.createdAt).toBe(123);
    const reread = await repo.listSessionVectors('matt');
    expect(reread.map((v) => v.sessionId)).toEqual(['s-1']);
  });
});

describe('skill-frequency aggregation', () => {
  const sessions: ScoredSession[] = [
    { sessionId: 's1', score: 0.9, summary: '', skills: ['db-migrate', 'review'], tools: ['Bash'] },
    { sessionId: 's2', score: 0.8, summary: '', skills: ['db-migrate'], tools: ['Bash', 'Edit'] },
    {
      sessionId: 's3',
      score: 0.7,
      summary: '',
      skills: ['db-migrate', 'one-off'],
      tools: ['Edit'],
    },
  ];

  it('counts each skill across sessions, most-frequent first', () => {
    const freqs = aggregateFrequencies(sessions, (s) => s.skills);
    expect(freqs[0]).toEqual({ name: 'db-migrate', count: 3 });
    expect(freqs.find((f) => f.name === 'one-off')?.count).toBe(1);
  });

  it('splits confident (>=2 sessions) from low-confidence', () => {
    const freqs = aggregateFrequencies(sessions, (s) => s.skills);
    const { confident, low } = splitConfidence(freqs, 2);
    expect(confident).toContain('db-migrate');
    expect(low).toContain('one-off');
    expect(confident).not.toContain('one-off');
  });
});

describe('proposeAgent', () => {
  const drafter: AgentDrafter = {
    async draft(input) {
      return {
        name: 'migrator',
        model: 'claude-test',
        prompt: `Handles: ${input.description}`,
      };
    },
  };

  beforeEach(async () => {
    const mk = (id: string, summary: string, skills: string[], tools: string[]): SessionVector => ({
      sessionId: id,
      userId: 'matt',
      projectId: 'p1',
      summary,
      vector: VOCAB.map((w) => (summary.toLowerCase().includes(w) ? 1 : 0)),
      skills,
      tools,
      createdAt: 1,
    });
    await repo.putSessionVector(
      mk('s1', 'database migration schema', ['db-migrate', 'review'], ['Bash']),
    );
    await repo.putSessionVector(
      mk('s2', 'schema migration database', ['db-migrate'], ['Bash', 'rare-tool']),
    );
  });

  it('drafts an agent with confident skills/tools and low-confidence flags', async () => {
    const proposal = await proposeAgent(
      { userId: 'matt', description: 'database migration', k: 5 },
      { repo, embedder: keywordEmbedder, drafter, minConfidentCount: 2 },
    );
    expect(proposal.name).toBe('migrator');
    expect(proposal.skills).toContain('db-migrate'); // in both
    expect(proposal.tools).toContain('Bash'); // in both
    expect(proposal.lowConfidence).toContain('review'); // one session
    expect(proposal.lowConfidence).toContain('rare-tool'); // one session
    expect(proposal.evidence.length).toBeGreaterThan(0);
    expect(proposal.insufficientHistory).toBe(false);
  });

  it('returns a blank editable draft on no similar history', async () => {
    const proposal = await proposeAgent(
      { userId: 'someone-else', description: 'unrelated work', k: 5 },
      { repo, embedder: keywordEmbedder, drafter },
    );
    expect(proposal.insufficientHistory).toBe(true);
    expect(proposal.skills).toEqual([]);
    expect(proposal.evidence).toEqual([]);
  });
});
