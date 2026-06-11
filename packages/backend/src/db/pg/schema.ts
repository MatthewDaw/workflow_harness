import { pgTable, text, real, primaryKey } from 'drizzle-orm/pg-core';

/**
 * Drizzle schema for the strategic-execution domain (KTD7). Objectives — the
 * RCDO tree — move here from DynamoDB (U16) because the enforced Supporting
 * Outcome link is a foreign key and the roll-up joins commits↔objectives. The
 * weekly relations (`weekly_plans`, `weekly_commits`) and the slim `projects`
 * mirror land in U2; this file grows with them.
 *
 * `objectives` mirrors the shared `ObjectiveNode` shape (`id`, `org`, `level`,
 * `title`, `parentId`, `pct`) plus two columns the metric work needs:
 *  - `weight` (default 1) — the branch weight WSJF's Cost-of-Delay reads (U6).
 *  - `pctCache` — the roll-up's cached completion (was `ObjectiveNode.pct`).
 *
 * The primary key is composite `(org, id)`: an objective id is unique only
 * within its org (the Dynamo key was `ORG#<org>` / `RCDO#<id>`), so the org is
 * part of identity. `parentId` is intentionally NOT a foreign key — the roll-up
 * already tolerates a dangling parent link (an orphaned parent contributes to no
 * node and is never fatal), and enforcing it would reject otherwise-valid trees
 * mid-migration.
 */
export const objectives = pgTable(
  'objectives',
  {
    org: text('org').notNull(),
    id: text('id').notNull(),
    level: text('level').notNull(),
    title: text('title').notNull(),
    parentId: text('parent_id'),
    weight: real('weight').notNull().default(1),
    pctCache: real('pct_cache'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.org, t.id] }),
  }),
);

export type ObjectiveRow = typeof objectives.$inferSelect;
