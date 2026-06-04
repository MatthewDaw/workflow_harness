import { describe, expect, it } from 'vitest';
import { buildSeedSkills, STARTER_BUNDLE_NAME } from '../src/seed/skills.js';

/**
 * The seed groups every `.claude/skills/<name>` file into the org-scope
 * `command-hq-starter` bundle. These tests lock the record shapes + bundle
 * membership so a new bundled skill (e.g. create-hq-skill) ships correctly.
 */
describe('buildSeedSkills', () => {
  const files = [
    { name: 'create-hq-skill', description: 'Scaffold a new HQ skill', body: '# create-hq-skill' },
    { name: 'weekly-update', description: 'Weekly', body: '# weekly' },
  ];

  it('builds one org-scope skill per file plus a bundle listing them', () => {
    const records = buildSeedSkills('acme', files);

    const skills = records.filter((r) => r.kind === 'skill');
    expect(skills.map((s) => s.name).sort()).toEqual(['create-hq-skill', 'weekly-update']);
    for (const s of skills) {
      expect(s.scope).toEqual({ tier: 'org', id: 'acme' });
      expect(s.source).toBe('built-in');
    }

    const bundle = records.find((r) => r.kind === 'bundle');
    expect(bundle?.name).toBe(STARTER_BUNDLE_NAME);
    expect(bundle?.members).toEqual(['create-hq-skill', 'weekly-update']);
  });

  it('includes create-hq-skill as a bundle member and preserves its body', () => {
    const records = buildSeedSkills('acme', files);
    const created = records.find((r) => r.name === 'create-hq-skill');
    expect(created?.body).toBe('# create-hq-skill');
    const bundle = records.find((r) => r.kind === 'bundle');
    expect(bundle?.members).toContain('create-hq-skill');
  });
});
