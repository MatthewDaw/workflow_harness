import type { Repo } from '../db/repo.js';

/** Anything the catalog stamps with authorship — a skill, agent, or MCP server. */
type WithAuthor = { createdBy?: { userId?: string; name?: string } };

/**
 * Resolve catalog authorship to a human name for display.
 *
 * Catalog writes stamp `createdBy = { userId, name }`, but a write from the
 * claude+ wrapper's device token has no display name to stamp (the device token
 * deliberately drops the `name` claim — see `auth/verify.ts`), so the handler
 * falls back to stamping `name` with the Cognito **sub**. The UI then shows a raw
 * UUID as the author. Here we replace `createdBy.name` with the author's current
 * PROFILE name (their email) looked up by `createdBy.userId`, so the catalog
 * shows who actually created each item.
 *
 * Resolving on READ (rather than backfilling) means every EXISTING record — all
 * the UUID-stamped ones already in the table — displays a real name immediately,
 * and the name stays current if the user later changes it. `userId === 'system'`
 * (the built-in seed) and ids with no profile / no name are left untouched.
 *
 * One batched profile lookup per distinct author; an org has few authors, so this
 * is a handful of point reads per list call.
 */
export async function withAuthorNames<T extends WithAuthor>(repo: Repo, items: T[]): Promise<T[]> {
  const ids = [
    ...new Set(
      items
        .map((i) => i.createdBy?.userId)
        .filter((id): id is string => !!id && id !== 'system'),
    ),
  ];
  if (ids.length === 0) return items;

  const profiles = await Promise.all(ids.map((id) => repo.getUser(id)));
  const nameById = new Map<string, string>();
  ids.forEach((id, i) => {
    const name = profiles[i]?.name;
    if (name) nameById.set(id, name);
  });
  if (nameById.size === 0) return items;

  return items.map((item) => {
    const uid = item.createdBy?.userId;
    const resolved = uid ? nameById.get(uid) : undefined;
    return resolved ? { ...item, createdBy: { ...item.createdBy, name: resolved } } : item;
  });
}
