import { describe, expect, it } from 'vitest';
import { buildSeedSkills, STARTER_BUNDLE_NAME } from '../src/seed/skills.js';

/**
 * The seed records one catalog skill per `.claude/skills/<name>` file, and one
 * bundle per entry in the bundle manifest. A skill that IS a seeded-bundle member
 * seeds at ORG scope (the org-wide default); a skill no bundle lists is kept in
 * the catalog but seeded at the grant owner's USER scope, so a fresh org never
 * sees it. These tests lock the record shapes + manifest-driven scoping.
 */
describe('buildSeedSkills', () => {
  const files = [
    { name: 'hq-create-skill', description: 'Scaffold a new HQ skill', body: '# hq-create-skill' },
    { name: 'hq-weekly-update', description: 'Weekly', body: '# hq-weekly-update' },
    { name: 'playwright-cli', description: 'Browser CLI', body: '# playwright-cli' },
  ];
  const manifest = {
    [STARTER_BUNDLE_NAME]: {
      description: 'Core HQ skills',
      members: ['hq-create-skill', 'hq-weekly-update'],
    },
  };

  it('builds one skill per file plus a bundle from the manifest', () => {
    const records = buildSeedSkills('acme', files, manifest);

    const skills = records.filter((r) => r.kind === 'skill');
    expect(skills.map((s) => s.name).sort()).toEqual([
      'hq-create-skill',
      'hq-weekly-update',
      'playwright-cli',
    ]);
    for (const s of skills) {
      expect(s.source).toBe('built-in');
    }
    // Bundle members seed at org scope.
    for (const name of ['hq-create-skill', 'hq-weekly-update']) {
      expect(skills.find((s) => s.name === name)?.scope).toEqual({ tier: 'org', id: 'acme' });
    }

    const bundle = records.find((r) => r.kind === 'bundle');
    expect(bundle?.name).toBe(STARTER_BUNDLE_NAME);
    expect(bundle?.scope).toEqual({ tier: 'org', id: 'acme' });
    expect(bundle?.members).toEqual(['hq-create-skill', 'hq-weekly-update']);
  });

  it('keeps a non-bundle skill in the catalog but at the grant owner user scope', () => {
    const records = buildSeedSkills('acme', files, manifest, 'grant-owner');
    const bundle = records.find((r) => r.kind === 'bundle');
    expect(bundle?.members).not.toContain('playwright-cli');
    // It is still seeded (body preserved) but at the narrower user scope, so a
    // fresh org's org-only catalog never includes it.
    const granted = records.find((r) => r.name === 'playwright-cli');
    expect(granted?.kind).toBe('skill');
    expect(granted?.body).toBe('# playwright-cli');
    expect(granted?.scope).toEqual({ tier: 'user', id: 'grant-owner' });
  });

  it('drops a manifest member that has no matching skill file (no dangling members)', () => {
    const records = buildSeedSkills('acme', files, {
      [STARTER_BUNDLE_NAME]: { description: 'x', members: ['hq-weekly-update', 'ghost-skill'] },
    });
    const bundle = records.find((r) => r.kind === 'bundle');
    expect(bundle?.members).toEqual(['hq-weekly-update']);
  });

  it('with an empty manifest, no bundle and every skill is granted user-narrow', () => {
    const records = buildSeedSkills('acme', files, {}, 'grant-owner');
    expect(records.filter((r) => r.kind === 'bundle')).toHaveLength(0);
    const skills = records.filter((r) => r.kind === 'skill');
    expect(skills).toHaveLength(files.length);
    // No bundle => no org default => everything seeds at the grant owner scope.
    for (const s of skills) {
      expect(s.scope).toEqual({ tier: 'user', id: 'grant-owner' });
    }
  });
});
