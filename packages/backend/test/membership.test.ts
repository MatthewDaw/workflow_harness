import { describe, expect, it } from 'vitest';
import { effectiveOrg } from '../src/rest/membership.js';
import { memRepoHarness } from './helpers/memtable.js';
import { httpEvent } from './helpers/httpevent.js';

/**
 * effectiveOrg = profile.org ?? token-claim-org. The token fallback keeps
 * legacy/claim-only callers (and device tokens) scoped; a real onboarded user's
 * profile.org always wins.
 */

const { repo } = memRepoHarness();

describe('effectiveOrg', () => {
  it('falls back to the token claim org when no profile exists (back-compat)', async () => {
    const org = await effectiveOrg(
      httpEvent({ method: 'GET', userId: 'u', org: 'claim-org' }),
      repo,
    );
    expect(org).toBe('claim-org');
  });

  it('prefers the profile org over the token claim once onboarded', async () => {
    await repo.setUserOrg('u', 'real-org');
    const org = await effectiveOrg(
      httpEvent({ method: 'GET', userId: 'u', org: 'claim-org' }),
      repo,
    );
    expect(org).toBe('real-org');
  });

  it('is undefined when unauthenticated (no principal)', async () => {
    const org = await effectiveOrg(httpEvent({ method: 'GET', userId: null }), repo);
    expect(org).toBeUndefined();
  });
});
