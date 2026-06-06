import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { Repo } from '../src/db/repo.js';
import { effectiveOrg } from '../src/rest/membership.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { httpEvent } from './helpers/httpevent.js';

/**
 * effectiveOrg = profile.org ?? token-claim-org. The token fallback keeps
 * legacy/claim-only callers (and device tokens) scoped; a real onboarded user's
 * profile.org always wins.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

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
