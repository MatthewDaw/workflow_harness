import { describe, expect, it } from 'vitest';
import { orgScope, type Skill } from '@harness/shared';
import * as k from '../src/db/keys.js';
import { memRepoHarness } from './helpers/memtable.js';

/**
 * VERSIONING (KTD6) at the repo layer: every content edit SNAPSHOTS an immutable
 * revision; the "current" record stays under its plain item key; one per-baseName
 * ORG-WIDE TRUE pointer is initialized on first create and only repointed by
 * promote. Variants are `(baseName, repoId, userId)`. The catalog list reads must
 * skip the revision + TRUE side-records.
 */

const { repo } = memRepoHarness();

const ORG = 'acme';
const SCOPE = orgScope(ORG);

function skill(name: string, body = ''): Skill {
  return { name, scope: SCOPE, kind: 'skill', description: '', source: 'local', members: [], body };
}

describe('keys: revision + true-pointer + side-record detection', () => {
  it('builds a base-variant revision SK without repo/user', () => {
    const { SK } = k.revisionKey(SCOPE, 'SKILL', 'reconcile', 1);
    expect(SK).toMatch(/^SKILL#reconcile#r0*1$/);
  });

  it('builds a fork-variant revision SK with repo/user', () => {
    const { SK } = k.revisionKey(SCOPE, 'SKILL', 'reconcile', 2, {
      repoId: 'weekly-compass',
      userId: 'matt',
    });
    expect(SK).toMatch(/^SKILL#reconcile#R#weekly-compass#U#matt#r0*2$/);
  });

  it('detects revision + TRUE rows as side-records but not the live record', () => {
    expect(k.isVersionSideRecord('SKILL#reconcile#r000000000001')).toBe(true);
    expect(k.isVersionSideRecord('SKILL#reconcile#TRUE')).toBe(true);
    expect(k.isVersionSideRecord('SKILL#reconcile')).toBe(false);
    expect(k.isVersionSideRecord(undefined)).toBe(false);
  });
});

describe('putNewVersion (snapshot + current + initial TRUE)', () => {
  it('stamps the base variant at rev 1 and initializes the TRUE pointer', async () => {
    const stamped = await repo.putNewVersion('SKILL', skill('reconcile', 'v1'), { now: 1000 });
    expect(stamped.variantId).toBe('reconcile');
    expect(stamped.baseName).toBe('reconcile');
    expect(stamped.version).toBe(1);
    expect(stamped.createdAt).toBe(1000);

    // The live "current" record reads via the unchanged getSkill path.
    const current = await repo.getSkill(SCOPE, 'reconcile');
    expect(current?.body).toBe('v1');
    expect(current?.version).toBe(1);

    // The TRUE pointer is initialized to this variant + rev.
    const truth = await repo.getTrueVariant(SCOPE, 'SKILL', 'reconcile');
    expect(truth).toEqual({ baseName: 'reconcile', variantId: 'reconcile', rev: 1 });
  });

  it('a second edit snapshots rev 2 of the SAME variant and leaves TRUE untouched', async () => {
    await repo.putNewVersion('SKILL', skill('reconcile', 'v1'));
    const v2 = await repo.putNewVersion('SKILL', skill('reconcile', 'v2'));
    expect(v2.version).toBe(2);

    const revs = await repo.listRevisions(SCOPE, 'SKILL', 'reconcile');
    expect(revs.map((r) => r.version).sort()).toEqual([1, 2]);

    // TRUE still points at rev 1 (promotion is explicit, not on every edit).
    const truth = await repo.getTrueVariant(SCOPE, 'SKILL', 'reconcile');
    expect(truth?.rev).toBe(1);
  });

  it('forks a distinct variant for a different (repo, user) without colliding', async () => {
    await repo.putNewVersion('SKILL', skill('reconcile', 'base'));
    const fork = await repo.putNewVersion(
      'SKILL',
      skill('reconcile', 'forked'),
      { repoId: 'weekly-compass', authorUserId: 'matt' },
    );
    expect(fork.variantId).toBe('reconcile#R#weekly-compass#U#matt');
    expect(fork.version).toBe(1); // a NEW variant starts at rev 1

    const variants = await repo.listVariants(SCOPE, 'SKILL', 'reconcile');
    expect(variants.map((v) => v.variantId).sort()).toEqual([
      'reconcile',
      'reconcile#R#weekly-compass#U#matt',
    ]);
  });
});

describe('getRevision + listRevisions scoping', () => {
  it('reads back one specific revision snapshot', async () => {
    await repo.putNewVersion('SKILL', skill('reconcile', 'v1'));
    await repo.putNewVersion('SKILL', skill('reconcile', 'v2'));
    const rev1 = await repo.getRevision(SCOPE, 'SKILL', 'reconcile', 1);
    expect(rev1?.body).toBe('v1');
  });

  it('scopes listRevisions to one variant when repo/user are given', async () => {
    await repo.putNewVersion('SKILL', skill('reconcile', 'base'));
    await repo.putNewVersion('SKILL', skill('reconcile', 'fork'), {
      repoId: 'r',
      authorUserId: 'u',
    });
    const onlyFork = await repo.listRevisions(SCOPE, 'SKILL', 'reconcile', {
      repoId: 'r',
      userId: 'u',
    });
    expect(onlyFork).toHaveLength(1);
    expect(onlyFork[0]!.variantId).toBe('reconcile#R#r#U#u');
  });
});

describe('setTrueVariant (promotion repoints only)', () => {
  it('repoints TRUE to another variant without touching the variant records', async () => {
    await repo.putNewVersion('SKILL', skill('reconcile', 'base'));
    await repo.putNewVersion('SKILL', skill('reconcile', 'fork'), {
      repoId: 'r',
      authorUserId: 'u',
    });
    await repo.setTrueVariant(SCOPE, 'SKILL', {
      baseName: 'reconcile',
      variantId: 'reconcile#R#r#U#u',
      rev: 1,
    });
    const truth = await repo.getTrueVariant(SCOPE, 'SKILL', 'reconcile');
    expect(truth?.variantId).toBe('reconcile#R#r#U#u');

    // Both variants still exist (promotion never deletes/edits).
    expect(await repo.listVariants(SCOPE, 'SKILL', 'reconcile')).toHaveLength(2);
  });
});

describe('catalog list skips version side-records', () => {
  it('listSkills returns only the live record, not its revisions or TRUE row', async () => {
    await repo.putNewVersion('SKILL', skill('reconcile', 'v1'));
    await repo.putNewVersion('SKILL', skill('reconcile', 'v2'));
    const catalog = await repo.listSkills(ORG);
    expect(catalog.map((s) => s.name)).toEqual(['reconcile']);
    expect(catalog).toHaveLength(1);
  });
});
