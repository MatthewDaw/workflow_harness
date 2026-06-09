import { describe, expect, it } from 'vitest';
import { ROUTES } from '../src/local/devServer.js';
import { handler as skillsHandler } from '../src/rest/skills.js';
import { handler as ideasHandler } from '../src/rest/ideas.js';

/**
 * Local dev-server route parity with the deployed API.
 *
 * `infra/lib/api-stack.ts` is the source of truth for the API Gateway route
 * table; `packages/backend/src/local/devServer.ts` re-implements it so `npm run
 * dev` serves the same paths. When an endpoint exists in the stack but not here,
 * a local request 404s and the SPA shows an empty state instead of data. This
 * test pins the ideas/skills surface so that drift is caught in CI, not in a
 * confused local session.
 *
 * Each case asserts the FIRST matching route (the dispatcher takes the first
 * hit, ordered most-specific-first) carries the handler module the stack routes
 * the path to — `ideasFn` (rest/ideas.ts) or `skillsFn` (rest/skills.ts).
 */

function resolve(path: string): { handler: unknown; groups: Record<string, string> } {
  const hit = ROUTES.find((route) => route.re.test(path));
  if (!hit) throw new Error(`no local route for ${path}`);
  const m = hit.re.exec(path)!;
  return { handler: hit.handler, groups: { ...(m.groups ?? {}) } };
}

describe('devServer ROUTES — ideas/skills parity with api-stack', () => {
  // Path → the handler module api-stack.ts registers it against.
  const cases: Array<{ path: string; handler: unknown; params?: Record<string, string> }> = [
    { path: '/skills/auth-helper/candidate-learnings', handler: ideasHandler, params: { name: 'auth-helper' } },
    { path: '/skills/auth-helper/ideas', handler: ideasHandler, params: { name: 'auth-helper' } },
    { path: '/ideas/unassigned', handler: ideasHandler },
    {
      path: '/ideas/unassigned/entry-123/promote-to-skill',
      handler: ideasHandler,
      params: { entryId: 'entry-123' },
    },
    // U16 — the fold route is the skills Lambda (it reuses putNewVersion there),
    // unlike the candidate-learnings/ideas reads which are the ideas Lambda.
    {
      path: '/skills/auth-helper/ideas/idea-9/fold',
      handler: skillsHandler,
      params: { name: 'auth-helper', ideaId: 'idea-9' },
    },
    { path: '/skills/auth-helper/promote', handler: skillsHandler, params: { name: 'auth-helper' } },
  ];

  for (const c of cases) {
    it(`routes ${c.path} to the right handler`, () => {
      const { handler, groups } = resolve(c.path);
      expect(handler).toBe(c.handler);
      if (c.params) expect(groups).toMatchObject(c.params);
    });
  }

  it('does not let the generic /skills/{name} matcher swallow the skill-idea reads', () => {
    // candidate-learnings + the all-ideas read must resolve to the ideas Lambda,
    // proving they sit AHEAD of the generic /skills/{name} (skillsHandler) route.
    expect(resolve('/skills/x/candidate-learnings').handler).toBe(ideasHandler);
    expect(resolve('/skills/x/ideas').handler).toBe(ideasHandler);
    // While the bare /skills/{name} still reaches the skills Lambda.
    expect(resolve('/skills/x').handler).toBe(skillsHandler);
  });

  it('matches the two-segment unassigned promote before the bare bin read', () => {
    // The promote action carries the {entryId} param; the bare bin read does not.
    expect(resolve('/ideas/unassigned/e1/promote-to-skill').groups.entryId).toBe('e1');
    expect(resolve('/ideas/unassigned').groups.entryId).toBeUndefined();
  });
});
