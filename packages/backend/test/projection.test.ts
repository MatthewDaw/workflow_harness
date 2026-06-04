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
