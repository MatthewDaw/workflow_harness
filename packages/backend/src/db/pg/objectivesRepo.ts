import { and, eq } from 'drizzle-orm';
import type { ObjectiveNode } from '@harness/shared';
import { objectives, type ObjectiveRow } from './schema.js';
import type { PgDb } from './migrate.js';

/**
 * Objectives persistence on Postgres (U16) — the same four operations the Dynamo
 * `Repo` exposed (`putObjective`, `getObjective`, `deleteObjective`,
 * `listObjectives`), behind the same `ObjectiveNode` contract so the REST layer
 * (`rest/objectives.ts`) and the roll-up (`projections/rollupRepo.ts`) repoint
 * with no shape change. Functions take a `PgDb` so they run against Neon in
 * Lambda and pglite in tests.
 *
 * `weight` and `pct_cache` are columns the metric/roll-up work owns; the public
 * `ObjectiveNode` carries only `pct` (← `pct_cache`). `weight` defaults to 1 and
 * is surfaced/managed later (WSJF, U6) — it is not part of `ObjectiveNode` yet,
 * so reads drop it and writes leave the DB default in place.
 */

/** Map a row to the public `ObjectiveNode`, omitting absent optional fields (matches the Dynamo shape). */
function toNode(row: ObjectiveRow): ObjectiveNode {
  return {
    id: row.id,
    org: row.org,
    level: row.level as ObjectiveNode['level'],
    title: row.title,
    ...(row.parentId != null ? { parentId: row.parentId } : {}),
    ...(row.pctCache != null ? { pct: row.pctCache } : {}),
  };
}

/** Upsert a node (full overwrite on the composite `(org, id)` key), preserving the stored `weight`. */
export async function putObjective(db: PgDb, o: ObjectiveNode): Promise<void> {
  await db
    .insert(objectives)
    .values({
      org: o.org,
      id: o.id,
      level: o.level,
      title: o.title,
      parentId: o.parentId ?? null,
      pctCache: o.pct ?? null,
    })
    .onConflictDoUpdate({
      target: [objectives.org, objectives.id],
      set: {
        level: o.level,
        title: o.title,
        parentId: o.parentId ?? null,
        pctCache: o.pct ?? null,
      },
    });
}

export async function getObjective(
  db: PgDb,
  org: string,
  id: string,
): Promise<ObjectiveNode | undefined> {
  const rows = await db
    .select()
    .from(objectives)
    .where(and(eq(objectives.org, org), eq(objectives.id, id)))
    .limit(1);
  return rows[0] ? toNode(rows[0]) : undefined;
}

export async function deleteObjective(db: PgDb, org: string, id: string): Promise<void> {
  await db.delete(objectives).where(and(eq(objectives.org, org), eq(objectives.id, id)));
}

export async function listObjectives(db: PgDb, org: string): Promise<ObjectiveNode[]> {
  const rows = await db.select().from(objectives).where(eq(objectives.org, org));
  return rows.map(toNode);
}
