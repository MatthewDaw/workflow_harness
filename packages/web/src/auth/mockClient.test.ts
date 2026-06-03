import { describe, it, expect } from 'vitest';
import { createMockClient, deriveOrg } from './mockClient.js';

describe('mockClient org derivation (per-user isolation)', () => {
  it('derives distinct orgs from distinct email domains', () => {
    expect(deriveOrg('mattdaw7@gmail.com')).toBe('gmail.com');
    expect(deriveOrg('bill@cow.com')).toBe('cow.com');
    expect(deriveOrg('mattdaw7@gmail.com')).not.toBe(deriveOrg('bill@cow.com'));
  });

  it('falls back to org-<username> for a bare username', () => {
    expect(deriveOrg('matt')).toBe('org-matt');
  });

  it('signs different emails into different orgs', async () => {
    const a = await createMockClient().signIn('mattdaw7@gmail.com', 'pw');
    const b = await createMockClient().signIn('bill@cow.com', 'pw');
    expect(a.org).toBe('gmail.com');
    expect(b.org).toBe('cow.com');
    expect(a.org).not.toBe(b.org);
  });
});
