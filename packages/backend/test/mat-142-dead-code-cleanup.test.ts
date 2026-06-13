/**
 * mat-142-dead-code-cleanup.test.ts — MAT-142 acceptance checklist.
 *
 * Verifies (statically, via source inspection) that:
 *  1. SessionVector / Forge dead code (dto.ts, keys.ts, repo.ts) is fully removed
 *     with zero non-test callers remaining.
 *  2. The retired 410 scope routes are gone from every dispatch/routing layer
 *     (rest/agents.ts, rest/skills.ts, local/devServer.ts, infra/lib/api-stack.ts).
 *
 * These are source-text assertions — no DynamoDB or runtime required.
 */

import { describe, it, expect } from 'vitest';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '../../..');

function read(rel: string): string {
  return fs.readFileSync(path.join(root, rel), 'utf-8');
}

// ─── SessionVector / Forge dead-code ─────────────────────────────────────────

describe('MAT-142 — SessionVector / Forge dead code removed', () => {
  it('dto.ts no longer exports sessionVectorSchema or SessionVector', () => {
    const src = read('packages/shared/src/dto.ts');
    expect(src).not.toContain('sessionVectorSchema');
    expect(src).not.toContain('SessionVector');
  });

  it('keys.ts no longer exports sessionVectorKey or sessionVectorPrefix', () => {
    const src = read('packages/backend/src/db/keys.ts');
    expect(src).not.toContain('sessionVectorKey');
    expect(src).not.toContain('sessionVectorPrefix');
    expect(src).not.toContain('USERVEC#');
    expect(src).not.toContain("'VEC#'");
  });

  it('repo.ts no longer imports or uses SessionVector', () => {
    const src = read('packages/backend/src/db/repo.ts');
    expect(src).not.toContain('SessionVector');
    expect(src).not.toContain('putSessionVector');
    expect(src).not.toContain('listSessionVectors');
    expect(src).not.toContain('sessionVectorKey');
    expect(src).not.toContain('sessionVectorPrefix');
  });

  it('no non-test source file references the deleted SessionVector symbols', () => {
    // Walk src directories (not test/) for any lingering reference.
    const srcDirs = [
      path.join(root, 'packages/shared/src'),
      path.join(root, 'packages/backend/src'),
      path.join(root, 'packages/web/src'),
    ];
    const forbidden = [
      'sessionVectorSchema',
      'SessionVector',
      'putSessionVector',
      'listSessionVectors',
      'sessionVectorKey',
      'sessionVectorPrefix',
    ];
    const violations: string[] = [];
    function walk(dir: string) {
      if (!fs.existsSync(dir)) return;
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
        } else if (entry.isFile() && /\.(ts|tsx)$/.test(entry.name)) {
          const src = fs.readFileSync(full, 'utf-8');
          for (const sym of forbidden) {
            if (src.includes(sym)) {
              violations.push(`${full}: contains '${sym}'`);
            }
          }
        }
      }
    }
    for (const d of srcDirs) walk(d);
    expect(violations).toEqual([]);
  });
});

// ─── 410 scope-route dispatch removed ────────────────────────────────────────

describe('MAT-142 — retired 410 scope routes removed from all dispatch/routing layers', () => {
  it('rest/agents.ts no longer dispatches /scope to gone()', () => {
    const src = read('packages/backend/src/rest/agents.ts');
    expect(src).not.toContain("path.endsWith('/scope')");
    expect(src).not.toContain("gone('scope changes are retired')");
    // gone import should also be absent
    expect(src).not.toContain("gone,");
  });

  it('rest/skills.ts no longer dispatches /scope to gone()', () => {
    const src = read('packages/backend/src/rest/skills.ts');
    expect(src).not.toContain("path.endsWith('/scope')");
    expect(src).not.toContain("gone('scope changes are retired')");
    expect(src).not.toContain('gone,');
  });

  it('local/devServer.ts no longer routes /agents/{name}/scope or /skills/{name}/scope', () => {
    const src = read('packages/backend/src/local/devServer.ts');
    expect(src).not.toMatch(/\/agents\/.*\/scope/);
    expect(src).not.toMatch(/\/skills\/.*\/scope.*handler: skillsHandler/);
  });

  it('infra/lib/api-stack.ts no longer registers AgentScope or SkillScope routes', () => {
    const src = read('infra/lib/api-stack.ts');
    expect(src).not.toContain("'AgentScope'");
    expect(src).not.toContain("'SkillScope'");
    expect(src).not.toContain("'/agents/{name}/scope'");
    expect(src).not.toContain("'/skills/{name}/scope'");
  });
});
