import { z } from 'zod';
/**
 * Scoping model shared by Agents and Skills: an item is registered at one of
 * three tiers and inherits downward. When two items share a name, the narrowest
 * visible scope wins (project > user > org).
 */
export const SCOPE_TIERS = ['org', 'user', 'project'];
export const scopeTierSchema = z.enum(SCOPE_TIERS);
/**
 * A scope reference. `id` identifies the owning entity at that tier:
 * - org: the org id
 * - user: the user id
 * - project: the project id
 */
export const scopeRefSchema = z.object({
    tier: scopeTierSchema,
    id: z.string().min(1),
});
/** Higher number = narrower scope = wins on name collision. */
export const SCOPE_PRECEDENCE = {
    org: 1,
    user: 2,
    project: 3,
};
/** Is an item at `scope` visible to a viewer in `ctx`? */
export function isVisible(scope, ctx) {
    switch (scope.tier) {
        case 'org':
            return scope.id === ctx.org;
        case 'user':
            return scope.id === ctx.userId;
        case 'project':
            return ctx.projectId !== undefined && scope.id === ctx.projectId;
    }
}
/**
 * Resolve a flat list of scoped items into the effective set for `ctx`.
 * Invisible items are dropped; on a name collision the narrowest scope wins.
 */
export function resolveScoped(items, ctx) {
    const byName = new Map();
    for (const item of items) {
        if (!isVisible(item.scope, ctx))
            continue;
        const existing = byName.get(item.name);
        if (existing === undefined ||
            SCOPE_PRECEDENCE[item.scope.tier] > SCOPE_PRECEDENCE[existing.scope.tier]) {
            byName.set(item.name, item);
        }
    }
    return [...byName.values()];
}
//# sourceMappingURL=scope.js.map