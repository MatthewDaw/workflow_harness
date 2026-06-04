import { describe, expect, it } from 'vitest';
import { buildSeedSkills, STARTER_BUNDLE_NAME } from '../src/seed/skills.js';

/**
 * The seed records one catalog skill per `.claude/skills/<name>` file, and one
 * bundle per entry in the bundle manifest. A skill no bundle lists stays
 * standalone. These tests lock the record shapes + manifest-driven membership.
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

  it('builds one org-scope skill per file plus a bundle from the manifest', () => {
    const records = buildSeedSkills('acme', files, manifest);

    const skills = records.filter((r) => r.kind === 'skill');
    expect(skills.map((s) => s.name).sort()).toEqual([
      'hq-create-skill',
      'hq-weekly-update',
      'playwright-cli',
    ]);
    for (const s of skills) {
      expect(s.scope).toEqual({ tier: 'org', id: 'acme' });
      expect(s.source).toBe('built-in');
    }

    const bundle = records.find((r) => r.kind === 'bundle');
    expect(bundle?.name).toBe(STARTER_BUNDLE_NAME);
    expect(bundle?.members).toEqual(['hq-create-skill', 'hq-weekly-update']);
  });

  it('leaves a skill no bundle lists standalone (not a member of any bundle)', () => {
    const records = buildSeedSkills('acme', files, manifest);
    const bundle = records.find((r) => r.kind === 'bundle');
    expect(bundle?.members).not.toContain('playwright-cli');
    // It is still seeded as its own catalog skill, with its body preserved.
    const standalone = records.find((r) => r.name === 'playwright-cli');
    expect(standalone?.kind).toBe('skill');
    expect(standalone?.body).toBe('# playwright-cli');
  });

  it('drops a manifest member that has no matching skill file (no dangling members)', () => {
    const records = buildSeedSkills('acme', files, {
      [STARTER_BUNDLE_NAME]: { description: 'x', members: ['hq-weekly-update', 'ghost-skill'] },
    });
    const bundle = records.find((r) => r.kind === 'bundle');
    expect(bundle?.members).toEqual(['hq-weekly-update']);
  });

  it('produces no bundle when the manifest is empty', () => {
    const records = buildSeedSkills('acme', files, {});
    expect(records.filter((r) => r.kind === 'bundle')).toHaveLength(0);
    expect(records.filter((r) => r.kind === 'skill')).toHaveLength(files.length);
  });
});
