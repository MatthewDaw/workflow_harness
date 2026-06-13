import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { reconcileMemoriesRequestSchema, type Memory } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  defaultRepo,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
  unauthorized,
} from './runtime.js';
import { resolvePrincipal } from './bearerAuth.js';
import { ownedProject } from './ownership.js';
import {
  callPythonAuthored,
  usesPythonAuthored,
  type AuthoredRequest,
} from '../python-authored-client.js';

/**
 * REST: project memories — the per-user "Memories" tab (Project Details) fed by
 * the claude+ daemon syncing a project's Claude Code memory files up to HQ.
 *
 *   GET  /projects/:pid/memories  — every memory in the project, all authors
 *   PUT  /projects/:pid/memories  — full reconcile of the CALLER's memory set
 *
 * Claude Code persists small per-project "memory" markdown files on disk; the
 * daemon watches that directory and, as Claude saves/edits/deletes them, PUTs the
 * caller's WHOLE current set here. The PUT is a full reconcile per author (KTD):
 * adds + updates land and on-disk deletions propagate, scoped to the caller's own
 * userId so authors never clobber each other. Each memory is stamped with its
 * author so the tab can group/filter by user. HQ stores and serves them — it
 * never generates the content.
 *
 * Graph bridge (U8 two-store coherence)
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 * When ``PYTHON_AUTHORED_URL`` is configured, each memory write/delete is
 * additionally bridged to the Python authored-ingestion endpoint.  This makes
 * every Memories-tab write a directive in the learning graph (authored idea,
 * top authority, immediately active, necessity-exempt) and every delete an
 * un-bridge (invalidAt stamped on the authored idea).
 *
 * The bridge is fire-and-forget-on-error: a Python endpoint failure is logged
 * but does NOT fail the Memories tab PUT response — the flat MEM# DynamoDB
 * write (user-visible, machine-syncable) is the primary operation.  The graph
 * bridge is additive.
 *
 * When ``PYTHON_AUTHORED_URL`` is not set (local dev / tests that don't run
 * the Python service), the bridge is skipped and the handler works exactly as
 * before.
 */

export interface MemoriesDeps {
  repo: Repo;
  /** Injectable fetch for tests (overrides the global fetch used by callPythonAuthored). */
  fetchImpl?: (url: string, init: RequestInit) => Promise<Response>;
  /** Org slug for the org-scoped Python authored bridge. */
  org?: string;
}

/**
 * GET — the whole project's memories across ALL authors (one partition read). The
 * read is owner-gated (only the project owner sees the tab); the UI groups the
 * flat list by `userId`.
 */
export async function listMemories(
  event: APIGatewayProxyEventV2,
  deps: MemoriesDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const memories = await deps.repo.listMemories(resolved.project.id);
  return ok({ memories });
}

/**
 * PUT — full reconcile of the CALLER's memory set for the project: every posted
 * memory is stamped with the caller's identity + now and stored, and any of the
 * caller's previously-synced memories absent from the payload are deleted (so a
 * file removed on disk disappears from HQ). An empty array clears the caller's
 * set.
 *
 * NOTE: there is deliberately NO ownedProject gate here. The reconcile is scoped
 * to the principal's OWN userId (the SK is `MEM#<userId>#<name>`, see keys.ts), so
 * a caller can only ever write/delete under their own author key — they cannot
 * clobber another author's memories. Dropping the owner gate is what lets a
 * COLLABORATOR's daemon push memories to a project they do not own; ownership only
 * governs who can READ the aggregated tab (the GET above).
 *
 * Graph bridge: when ``PYTHON_AUTHORED_URL`` is configured, each added/updated
 * memory is bridged to the Python authored endpoint (directive kind), and each
 * deleted memory is un-bridged (delete kind).  Errors from the bridge are logged
 * but do not fail the handler — the MEM# write is the primary operation.
 */
export async function reconcileMemories(
  event: APIGatewayProxyEventV2,
  deps: MemoriesDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  if (!pid) return badRequest('missing project id');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const parsed = reconcileMemoriesRequestSchema.safeParse(body);
  if (!parsed.success) return badRequest(parsed.error.message);

  // Stamp each on-disk input into a full Memory: the path supplies projectId, the
  // principal supplies userId/userName, and we record the sync time. Optional
  // fields (userName/description/type) are spread conditionally so absent values
  // never serialize as explicit `undefined` into DynamoDB.
  const now = Date.now();
  const items: Memory[] = parsed.data.memories.map((m) => ({
    projectId: pid,
    userId: principal.userId,
    ...(principal.name ? { userName: principal.name } : {}),
    name: m.name,
    ...(m.description !== undefined ? { description: m.description } : {}),
    ...(m.type !== undefined ? { type: m.type } : {}),
    content: m.content,
    updatedAt: now,
  }));

  // Compute deletions (items previously stored by this author but absent from
  // the new payload) so we can un-bridge them in the Python graph.
  let deletedNames: string[] = [];
  if (usesPythonAuthored()) {
    const existing = await deps.repo.listMemories(pid);
    const callerExisting = existing.filter((m) => m.userId === principal.userId);
    const incomingNames = new Set(items.map((m) => m.name));
    deletedNames = callerExisting
      .map((m) => m.name)
      .filter((n) => !incomingNames.has(n));
  }

  // Primary write: flat MEM# DynamoDB reconcile (the user-visible, machine-syncable path).
  await deps.repo.replaceUserMemories(pid, principal.userId, items);

  // Graph bridge (additive, fire-and-log-on-error).
  if (usesPythonAuthored()) {
    const org = deps.org ?? principal.org;
    // Bridge each new/updated memory as a directive.
    for (const m of items) {
      const req: AuthoredRequest = {
        kind: 'directive',
        org,
        projectId: pid,
        userId: principal.userId,
        name: m.name,
        content: m.content,
        // Use the memory name as the skill base name so directives are
        // co-located in a predictable skill family.  Production operators can
        // configure a different mapping; this is the safe default.
        skillBaseName: m.name,
        scopeTag: 'project',
      };
      callPythonAuthored(req, deps.fetchImpl).catch((err: unknown) => {
        console.warn(`[memories] bridge write failed for ${m.name}: ${String(err)}`);
      });
    }

    // Un-bridge deleted memories.
    for (const name of deletedNames) {
      const req: AuthoredRequest = {
        kind: 'delete',
        org,
        projectId: pid,
        userId: principal.userId,
        name,
        skillBaseName: name,
      };
      callPythonAuthored(req, deps.fetchImpl).catch((err: unknown) => {
        console.warn(`[memories] bridge delete failed for ${name}: ${String(err)}`);
      });
    }
  }

  return ok({ memories: items });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: MemoriesDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;

  if (method === 'PUT') return reconcileMemories(event, deps);
  return listMemories(event, deps);
}
