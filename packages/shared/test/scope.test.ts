import { describe, it, expect } from 'vitest';
import { resolveScoped, isVisible, type Scoped, type ScopeContext } from '../src/scope.js';

const ctx: ScopeContext = { org: 'acme', userId: 'matt', projectId: 'weekly-compass' };

function agent(name: string, tier: 'org' | 'user' | 'project', id: string): Scoped {
  return { name, scope: { tier, id } };
}

describe('scope resolution', () => {
  it('includes an org-only item', () => {
    const out = resolveScoped([agent('reviewer', 'org', 'acme')], ctx);
    expect(out.map((a) => a.name)).toEqual(['reviewer']);
  });

  it('narrowest scope wins on a name collision (project over org)', () => {
    const items = [agent('builder', 'org', 'acme'), agent('builder', 'project', 'weekly-compass')];
    const out = resolveScoped(items, ctx);
    expect(out).toHaveLength(1);
    expect(out[0]!.scope.tier).toBe('project');
  });

  it('user overrides org but project overrides both', () => {
    const items = [
      agent('builder', 'org', 'acme'),
      agent('builder', 'user', 'matt'),
      agent('builder', 'project', 'weekly-compass'),
    ];
    const out = resolveScoped(items, ctx);
    expect(out).toHaveLength(1);
    expect(out[0]!.scope.tier).toBe('project');
  });

  it('drops items scoped to another user or project', () => {
    const items = [
      agent('mine', 'user', 'matt'),
      agent('theirs', 'user', 'someone-else'),
      agent('otherproj', 'project', 'atlas-billing'),
    ];
    const out = resolveScoped(items, ctx)
      .map((a) => a.name)
      .sort();
    expect(out).toEqual(['mine']);
  });

  it('hides project-scoped items when no project is in context', () => {
    const noProject: ScopeContext = { org: 'acme', userId: 'matt' };
    expect(isVisible({ tier: 'project', id: 'weekly-compass' }, noProject)).toBe(false);
    expect(isVisible({ tier: 'org', id: 'acme' }, noProject)).toBe(true);
    expect(isVisible({ tier: 'user', id: 'matt' }, noProject)).toBe(true);
  });

  it('composes the full effective set across tiers', () => {
    const items = [
      agent('reviewer', 'org', 'acme'),
      agent('research-sweeper', 'user', 'matt'),
      agent('rcdo-linker', 'project', 'weekly-compass'),
    ];
    const out = resolveScoped(items, ctx)
      .map((a) => a.name)
      .sort();
    expect(out).toEqual(['rcdo-linker', 'research-sweeper', 'reviewer']);
  });
});
