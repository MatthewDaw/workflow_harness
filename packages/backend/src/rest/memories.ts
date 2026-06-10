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
 */

export interface MemoriesDeps {
  repo: Repo;
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

  await deps.repo.replaceUserMemories(pid, principal.userId, items);
  return ok({ memories: items });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: MemoriesDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;

  if (method === 'PUT') return reconcileMemories(event, deps);
  return listMemories(event, deps);
}
