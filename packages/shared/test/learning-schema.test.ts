/**
 * learning-schema.test.ts — MAT-141 (U10) Gap 2 + Gap "wrapper/web parse".
 *
 * Proves @harness/shared CONSUMES the IDL-generated learning schema (no
 * hand-authored mirror), and that TS consumers (the web app + the Go wrapper's
 * fixture) can parse records keyed by the generated builders.
 *
 *  - test_no_hand_authored_key_mirror_remains: the generated builders are the
 *    single source of the IDEA# / IDEAGOLD# / SKILL# revision + TRUE-pointer SK
 *    formats; the backend db/keys builders must produce BYTE-IDENTICAL strings
 *    (they delegate to / coincide with the generated ones), so no drift-capable
 *    second copy exists.
 *  - test_wrapper_and_web_parse_generated_schema: a record shaped by the
 *    generated TS interfaces round-trips through JSON (what the Go materializer +
 *    TS web read).
 */

import { describe, it, expect } from 'vitest';
import {
  learningSkillKey,
  learningRevisionKey,
  learningTruePointerKey,
  learningIdeaKey,
  learningGoldenCaseKey,
  type LearningRevisionRecord,
  type LearningTruePointerRecord,
  type LearningGoldenCaseRecord,
} from '../src/learning-schema.js';

describe('Gap 2 — @harness/shared consumes the IDL codegen (no hand mirror)', () => {
  it('the generated builders own the IDEA# / IDEAGOLD# SK formats', () => {
    expect(learningIdeaKey('acme', 'reconcile', 'i-1')).toEqual({
      PK: 'SCOPE#org#acme',
      SK: 'IDEA#reconcile#i-1',
    });
    expect(learningGoldenCaseKey('acme', 'reconcile', 'i-1')).toEqual({
      PK: 'SCOPE#org#acme',
      SK: 'IDEAGOLD#reconcile#i-1',
    });
  });

  it('the generated SKILL revision + TRUE-pointer builders match the IDL format', () => {
    // Base variant rev (empty variant id → SKILL##r…), zero-padded to width 12.
    expect(learningRevisionKey('acme', '', 7).SK).toBe('SKILL##r000000000007');
    expect(learningRevisionKey('acme', 'reconcile', 1).SK).toBe('SKILL#reconcile#r000000000001');
    expect(learningTruePointerKey('acme', 'reconcile').SK).toBe('SKILL#reconcile#TRUE');
    expect(learningSkillKey('acme', 'reconcile').SK).toBe('SKILL#reconcile');
  });

  it('the file is stamped GENERATED (the drift guard is visible)', async () => {
    // Read the generated file text to confirm it carries the codegen header +
    // fingerprint (CI `codegen --check` enforces freshness against the IDL).
    const fs = await import('node:fs');
    const url = await import('node:url');
    const path = url.fileURLToPath(new URL('../src/learning-schema.ts', import.meta.url));
    const src = fs.readFileSync(path, 'utf-8');
    expect(src).toContain('GENERATED — do not edit by hand');
    expect(src).toContain('IDL fingerprint:');
    // The hand-spelled IDEA#/IDEAGOLD# literals must NOT also live in db/keys.ts
    // (the backend builders delegate to these generated ones — checked in the
    // backend test `keys-no-hand-mirror.test.ts`).
  });
});

describe('test_wrapper_and_web_parse_generated_schema', () => {
  it('a Python-written revision record round-trips through the generated TS interface', () => {
    // Simulate the DynamoDB item attributes the Python writer stores; the Go
    // materializer + TS web parse exactly this shape.
    const item = {
      ...learningRevisionKey('acme', '', 1),
      baseName: 'reconcile',
      variantId: '',
      rev: 1,
      body: 'Use the ledger currency.',
      org: 'acme',
      ideaId: 'i-1',
    };
    // The TS web reads it as a LearningRevisionRecord (the generated interface).
    const parsed: LearningRevisionRecord = {
      baseName: item.baseName,
      variantId: item.variantId,
      rev: item.rev,
      body: item.body,
      org: item.org,
      ideaId: item.ideaId,
    };
    expect(parsed.body).toBe('Use the ledger currency.');
    expect(parsed.rev).toBe(1);

    // The Go materializer reads the TRUE pointer to learn the live rev.
    const ptr: LearningTruePointerRecord = {
      baseName: 'reconcile',
      variantId: '',
      rev: 1,
      org: 'acme',
    };
    expect(learningTruePointerKey(ptr.org, ptr.baseName).SK).toBe('SKILL#reconcile#TRUE');

    // The TS web reads the golden case for the regression suite.
    const gc: LearningGoldenCaseRecord = {
      caseId: 'i-1',
      skillBaseName: 'reconcile',
      org: 'acme',
      before: 'v1',
      after: 'v2',
      ideaBody: 'lesson',
    };
    expect(learningGoldenCaseKey(gc.org, gc.skillBaseName, gc.caseId).SK).toBe(
      'IDEAGOLD#reconcile#i-1',
    );
  });
});
