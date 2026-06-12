/**
 * keys-no-hand-mirror.test.ts — MAT-141 (U10) Gap 2.
 *
 * The learning record key formats (IDEA#, IDEAGOLD#, and the org-scoped SKILL#
 * revision + TRUE pointer) are defined ONCE in the learning IDL and code-generated
 * into @harness/shared. The backend's db/keys.ts must NOT carry a second,
 * hand-authored copy of those SK formats that could drift from the writer/reader.
 *
 * This test pins that:
 *  - `ideaKey` / `goldenCaseKey` DELEGATE to the generated builders (identical
 *    output, by construction — they call them).
 *  - the generic `revisionKey` / `truePointerKey` / `skillKey` produce
 *    BYTE-IDENTICAL strings to the IDL-generated builders for the org+SKILL case,
 *    so there is no drift-capable mirror.
 *  - db/keys.ts source contains no hand-spelled `IDEA#`/`IDEAGOLD#` SK literals.
 */

import { describe, it, expect } from 'vitest';
import {
  learningIdeaKey,
  learningGoldenCaseKey,
  learningRevisionKey,
  learningTruePointerKey,
  learningSkillKey,
  orgScope,
} from '@harness/shared';
import { ideaKey, goldenCaseKey, revisionKey, truePointerKey, skillKey } from '../src/db/keys.js';

const ORG = 'acme';
const SCOPE = orgScope(ORG);

describe('Gap 2 — db/keys.ts has no hand-authored learning-key mirror', () => {
  it('ideaKey delegates to the generated learningIdeaKey', () => {
    expect(ideaKey(ORG, 'reconcile', 'i-1')).toEqual(learningIdeaKey(ORG, 'reconcile', 'i-1'));
  });

  it('goldenCaseKey delegates to the generated learningGoldenCaseKey', () => {
    expect(goldenCaseKey(ORG, 'reconcile', 'i-1')).toEqual(
      learningGoldenCaseKey(ORG, 'reconcile', 'i-1'),
    );
  });

  it('the generic skill revision SK is byte-identical to the IDL builder (base variant)', () => {
    // db/keys.revisionKey(scope, 'SKILL', baseName, rev) for the BASE variant
    // (no repo/user) → SKILL#<baseName>#r<padded>. The IDL builder keys on the
    // variant id; the base variant id IS the baseName, so they must coincide.
    const fromKeys = revisionKey(SCOPE, 'SKILL', 'reconcile', 1);
    const fromIdl = learningRevisionKey(ORG, 'reconcile', 1);
    expect(fromKeys.SK).toBe(fromIdl.SK);
    expect(fromKeys.PK).toBe(fromIdl.PK);
  });

  it('the TRUE pointer SK is byte-identical to the IDL builder', () => {
    const fromKeys = truePointerKey(SCOPE, 'SKILL', 'reconcile');
    const fromIdl = learningTruePointerKey(ORG, 'reconcile');
    expect(fromKeys.SK).toBe(fromIdl.SK);
    expect(fromKeys.PK).toBe(fromIdl.PK);
  });

  it('the live skill SK is byte-identical to the IDL builder', () => {
    const fromKeys = skillKey(SCOPE, 'reconcile');
    const fromIdl = learningSkillKey(ORG, 'reconcile');
    expect(fromKeys.SK).toBe(fromIdl.SK);
    expect(fromKeys.PK).toBe(fromIdl.PK);
  });

  it('db/keys.ts source no longer hand-spells the IDEA#/IDEAGOLD# SK literals', async () => {
    const fs = await import('node:fs');
    const url = await import('node:url');
    const path = url.fileURLToPath(new URL('../src/db/keys.ts', import.meta.url));
    const src = fs.readFileSync(path, 'utf-8');
    // The builders must reference the generated functions, not template the SKs.
    expect(src).toContain('learningIdeaKey');
    expect(src).toContain('learningGoldenCaseKey');
    // No template-literal SK spellings like `IDEA#${...}` / `IDEAGOLD#${...}`.
    expect(src).not.toMatch(/SK:\s*`IDEA#\$\{/);
    expect(src).not.toMatch(/SK:\s*`IDEAGOLD#\$\{/);
  });
});
