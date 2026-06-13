/**
 * memories-authored-bridge.test.ts — Gap 4 TS test (MAT-146 U8)
 *
 * Proves that the Memories tab PUT (reconcileMemories) and machine-sync PUT
 * invoke the Python authored endpoint, bridging each memory write to an authored
 * idea and each delete un-bridging it.
 *
 * Two-store coherence:
 *   - A memory write (PUT) → calls the Python authored endpoint with kind=directive.
 *   - A memory delete (absent from a later PUT) → calls the Python authored endpoint
 *     with kind=delete.
 *   - The flat MEM# DynamoDB write always succeeds regardless of Python bridge result.
 *   - When PYTHON_AUTHORED_URL is not set the bridge is skipped (degradation).
 *
 * Tests:
 *   test_memory_write_bridges_to_authored_idea
 *   test_memory_delete_unbridges_authored_idea
 *   test_bridge_skipped_when_python_authored_url_not_set
 *   test_bridge_failure_does_not_fail_memories_put
 */

import { describe, expect, it, afterEach } from 'vitest';
import type { Memory, MemoryInput } from '@harness/shared';
import { listMemories, reconcileMemories } from '../src/rest/memories.js';
import { memRepoHarness } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------

const { repo } = memRepoHarness();

const MATT = 'matt';
const ORG = 'acme';
const PROJ = 'bridge-test-proj';
const SKILL_NAME = 'no-dashes';

function project(owner: string) {
  return { id: PROJ, name: PROJ, repo: 'gh/acme/bt', ownerUserId: owner, liveSessionCount: 0 };
}

function putEvent(userId: string, memories: MemoryInput[]) {
  return httpEvent({
    method: 'PUT',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/memories`,
    path: { pid: PROJ },
    body: { memories },
  });
}

const mem = (name: string, content: string): MemoryInput => ({ name, content });

afterEach(() => {
  delete process.env.PYTHON_AUTHORED_URL;
});

// ---------------------------------------------------------------------------
// test_memory_write_bridges_to_authored_idea
// ---------------------------------------------------------------------------

describe('test_memory_write_bridges_to_authored_idea', () => {
  it('PUT with PYTHON_AUTHORED_URL set calls the authored endpoint with kind=directive', async () => {
    process.env.PYTHON_AUTHORED_URL = 'https://python-authored.internal';
    await repo.putProject(project(MATT));

    const capturedRequests: { url: string; body: unknown }[] = [];

    const mockFetch = async (url: string, init: RequestInit): Promise<Response> => {
      capturedRequests.push({ url, body: JSON.parse(init.body as string) });
      return new Response(
        JSON.stringify({
          sourceName: SKILL_NAME,
          kind: 'directive',
          nodesWritten: 1,
          nodesDeduped: 0,
          supersessions: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    };

    const deps = { repo, fetchImpl: mockFetch, org: ORG };

    const res = await reconcileMemories(putEvent(MATT, [mem(SKILL_NAME, 'No dashes please.')]), deps);
    expect(res).toMatchObject({ statusCode: 200 });

    // Allow the fire-and-forget promises to settle.
    await new Promise((r) => setTimeout(r, 10));

    // The authored endpoint must have been called with kind=directive.
    const bridgeCalls = capturedRequests.filter((r) =>
      String(r.url).includes('/internal/authored'),
    );
    expect(bridgeCalls.length).toBeGreaterThanOrEqual(1);
    const directiveCall = bridgeCalls.find(
      (r) => (r.body as { kind: string }).kind === 'directive',
    );
    expect(directiveCall).toBeDefined();
    const b = directiveCall!.body as {
      kind: string;
      org: string;
      projectId: string;
      userId: string;
      name: string;
      content: string;
    };
    expect(b.kind).toBe('directive');
    expect(b.org).toBe(ORG);
    expect(b.projectId).toBe(PROJ);
    expect(b.userId).toBe(MATT);
    expect(b.name).toBe(SKILL_NAME);
    expect(b.content).toBe('No dashes please.');
  });
});

// ---------------------------------------------------------------------------
// test_memory_delete_unbridges_authored_idea
// ---------------------------------------------------------------------------

describe('test_memory_delete_unbridges_authored_idea', () => {
  it('a memory absent from a later PUT triggers a kind=delete bridge call', async () => {
    process.env.PYTHON_AUTHORED_URL = 'https://python-authored.internal';
    await repo.putProject(project(MATT));

    const capturedRequests: { url: string; body: unknown }[] = [];

    const mockFetch = async (url: string, init: RequestInit): Promise<Response> => {
      capturedRequests.push({ url, body: JSON.parse(init.body as string) });
      return new Response(
        JSON.stringify({ sourceName: 'ok', kind: 'ok' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    };

    const deps = { repo, fetchImpl: mockFetch, org: ORG };

    // First PUT: write two memories.
    await reconcileMemories(putEvent(MATT, [mem('mem-a', 'A'), mem('mem-b', 'B')]), deps);
    await new Promise((r) => setTimeout(r, 10));
    capturedRequests.length = 0; // reset for the second PUT

    // Second PUT: drop mem-b (simulating on-disk deletion).
    await reconcileMemories(putEvent(MATT, [mem('mem-a', 'A updated')]), deps);
    await new Promise((r) => setTimeout(r, 10));

    const bridgeCalls = capturedRequests.filter((r) =>
      String(r.url).includes('/internal/authored'),
    );

    // Should have a directive call for mem-a (updated) and a delete call for mem-b.
    const directiveCalls = bridgeCalls.filter(
      (r) => (r.body as { kind: string }).kind === 'directive',
    );
    const deleteCalls = bridgeCalls.filter(
      (r) => (r.body as { kind: string }).kind === 'delete',
    );

    expect(directiveCalls.length).toBeGreaterThanOrEqual(1);
    const directive = directiveCalls.find(
      (r) => (r.body as { name: string }).name === 'mem-a',
    );
    expect(directive).toBeDefined();

    expect(deleteCalls.length).toBeGreaterThanOrEqual(1);
    const del = deleteCalls.find((r) => (r.body as { name: string }).name === 'mem-b');
    expect(del).toBeDefined();
    expect((del!.body as { kind: string }).kind).toBe('delete');
    expect((del!.body as { userId: string }).userId).toBe(MATT);
  });
});

// ---------------------------------------------------------------------------
// test_bridge_skipped_when_python_authored_url_not_set
// ---------------------------------------------------------------------------

describe('test_bridge_skipped_when_python_authored_url_not_set', () => {
  it('PUT with no PYTHON_AUTHORED_URL does NOT call the authored endpoint', async () => {
    // PYTHON_AUTHORED_URL not set (already deleted in afterEach, but be explicit).
    delete process.env.PYTHON_AUTHORED_URL;
    await repo.putProject(project(MATT));

    let bridgeCalled = false;
    const mockFetch = async (): Promise<Response> => {
      bridgeCalled = true;
      return new Response('{}', { status: 200 });
    };

    const deps = { repo, fetchImpl: mockFetch };

    const res = await reconcileMemories(putEvent(MATT, [mem('rule', 'No tabs.')]), deps);
    expect(res).toMatchObject({ statusCode: 200 });

    await new Promise((r) => setTimeout(r, 10));

    // The flat MEM# write must have succeeded.
    const { memories } = bodyOf<{ memories: Memory[] }>(res);
    expect(memories.length).toBe(1);

    // The bridge must NOT have been called.
    expect(bridgeCalled).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// test_bridge_failure_does_not_fail_memories_put
// ---------------------------------------------------------------------------

describe('test_bridge_failure_does_not_fail_memories_put', () => {
  it('a Python authored endpoint failure does NOT fail the Memories PUT', async () => {
    process.env.PYTHON_AUTHORED_URL = 'https://python-authored.internal';
    await repo.putProject(project(MATT));

    // Mock fetch that always rejects (simulates network failure).
    const errorFetch = async (): Promise<Response> => {
      throw new Error('Python service unavailable');
    };

    const deps = { repo, fetchImpl: errorFetch, org: ORG };

    const res = await reconcileMemories(
      putEvent(MATT, [mem('style-rule', 'Use snake_case.')]),
      deps,
    );

    // The Memories PUT must succeed (200) even though the bridge errored.
    expect(res).toMatchObject({ statusCode: 200 });

    // The memory must have been stored in the flat MEM# table.
    const all = await repo.listMemories(PROJ);
    const found = all.find((m) => m.name === 'style-rule');
    expect(found).toBeDefined();
    expect(found?.content).toBe('Use snake_case.');
  });
});
