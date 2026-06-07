import { describe, expect, it } from 'vitest';
import type { Envelope, Event } from '@harness/shared';
import { applyEvent } from '../src/ws/projection.js';

/**
 * Projection folding for the new first-prompt summary: a `session.rename` event
 * that carries `summary` (the raw first prompt, from the UserPromptSubmit hook)
 * sets BOTH `name` and `summary` on the session read model. A rename without a
 * summary (manual rename / older daemon) leaves any prior summary untouched.
 */

const INSTANCE = 'inst-a';
const HOST = 'matt@mbp';
const SESSION = 's-1';

function env(seq: number, event: Event, ts = 1_700_000_000_000 + seq): Envelope {
  return { v: 1, instanceId: INSTANCE, host: HOST, ts, seq, event };
}

const startEvent: Event = {
  kind: 'session.start',
  sessionId: SESSION,
  projectId: 'weekly-compass',
  host: HOST,
  name: 'reconcile-variance',
};

describe('applyEvent — session.rename summary', () => {
  it('folds name + summary from a rename carrying the first prompt', () => {
    const started = applyEvent(undefined, env(1, startEvent));
    const renamed = applyEvent(
      started,
      env(2, {
        kind: 'session.rename',
        sessionId: SESSION,
        name: 'fix-login-cursor',
        summary: 'fix the login bug in the cursor flow',
      }),
    );
    expect(renamed.name).toBe('fix-login-cursor');
    expect(renamed.summary).toBe('fix the login bug in the cursor flow');
  });

  it('leaves a prior summary intact when a later rename omits summary', () => {
    const started = applyEvent(undefined, env(1, startEvent));
    const withSummary = applyEvent(
      started,
      env(2, {
        kind: 'session.rename',
        sessionId: SESSION,
        name: 'fix-login-cursor',
        summary: 'fix the login bug',
      }),
    );
    const manualRename = applyEvent(
      withSummary,
      env(3, { kind: 'session.rename', sessionId: SESSION, name: 'login-work' }),
    );
    expect(manualRename.name).toBe('login-work');
    expect(manualRename.summary).toBe('fix the login bug');
  });
});

/**
 * Topic-focus folding: a `session.topic` event evolves the session's `topic` and
 * rolling `description` (and stamps `summaryUpdatedAt` + `titleSource = 'topic'`)
 * WITHOUT ever altering the stable slug `name` (the title-thrash guard).
 */
describe('applyEvent — session.topic', () => {
  it('folds topic/description/summaryUpdatedAt/titleSource and leaves name unchanged', () => {
    const started = applyEvent(undefined, env(1, startEvent));
    const topicEnv = env(2, {
      kind: 'session.topic',
      sessionId: SESSION,
      segmentId: 'seg-1',
      topicLabel: 'auth refactor',
      description: 'reworking the device-code login flow',
    });
    const topiced = applyEvent(started, topicEnv);
    expect(topiced.topic).toBe('auth refactor');
    expect(topiced.description).toBe('reworking the device-code login flow');
    expect(topiced.summaryUpdatedAt).toBe(topicEnv.ts);
    expect(topiced.titleSource).toBe('topic');
    // The stable slug must never change on a topic fold.
    expect(topiced.name).toBe('reconcile-variance');
  });

  it('does not regress topic/description for an out-of-order (older seq) event', () => {
    const started = applyEvent(undefined, env(1, startEvent));
    const newer = applyEvent(
      started,
      env(5, {
        kind: 'session.topic',
        sessionId: SESSION,
        segmentId: 'seg-2',
        topicLabel: 'shipping the projection',
        description: 'wiring the read model fold',
      }),
    );
    // A late-arriving, older-seq topic must not clobber the current topic.
    const stale = applyEvent(
      newer,
      env(3, {
        kind: 'session.topic',
        sessionId: SESSION,
        segmentId: 'seg-1',
        topicLabel: 'auth refactor',
        description: 'reworking the device-code login flow',
      }),
    );
    expect(stale.topic).toBe('shipping the projection');
    expect(stale.description).toBe('wiring the read model fold');
    expect(stale.titleSource).toBe('topic');
  });

  it('keeps the slug stable while topic updates across renames and several folds', () => {
    let proj = applyEvent(undefined, env(1, startEvent));
    proj = applyEvent(
      proj,
      env(2, {
        kind: 'session.rename',
        sessionId: SESSION,
        name: 'fix-login-cursor',
        summary: 'fix the login bug',
      }),
    );
    proj = applyEvent(
      proj,
      env(3, {
        kind: 'session.topic',
        sessionId: SESSION,
        segmentId: 'seg-1',
        topicLabel: 'auth',
        description: 'first pass',
      }),
    );
    proj = applyEvent(
      proj,
      env(4, {
        kind: 'session.topic',
        sessionId: SESSION,
        segmentId: 'seg-2',
        topicLabel: 'tokens',
        description: 'rolling the token refresh',
      }),
    );
    // The slug tracks the last rename; the topic tracks the last topic fold.
    expect(proj.name).toBe('fix-login-cursor');
    expect(proj.summary).toBe('fix the login bug');
    expect(proj.topic).toBe('tokens');
    expect(proj.description).toBe('rolling the token refresh');
  });
});
