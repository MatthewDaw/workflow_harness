import { z } from 'zod';
/**
 * Scoping model shared by Agents and Skills: an item is registered at one of
 * three tiers and inherits downward. When two items share a name, the narrowest
 * visible scope wins (project > user > org).
 */
export declare const SCOPE_TIERS: readonly ["org", "user", "project"];
export declare const scopeTierSchema: z.ZodEnum<["org", "user", "project"]>;
export type ScopeTier = z.infer<typeof scopeTierSchema>;
/**
 * A scope reference. `id` identifies the owning entity at that tier:
 * - org: the org id
 * - user: the user id
 * - project: the project id
 */
export declare const scopeRefSchema: z.ZodObject<{
    tier: z.ZodEnum<["org", "user", "project"]>;
    id: z.ZodString;
}, "strip", z.ZodTypeAny, {
    tier: "org" | "user" | "project";
    id: string;
}, {
    tier: "org" | "user" | "project";
    id: string;
}>;
export type ScopeRef = z.infer<typeof scopeRefSchema>;
/** The viewer context a resolution is performed against. */
export interface ScopeContext {
    org: string;
    userId: string;
    projectId?: string;
}
/** Higher number = narrower scope = wins on name collision. */
export declare const SCOPE_PRECEDENCE: Record<ScopeTier, number>;
/** Is an item at `scope` visible to a viewer in `ctx`? */
export declare function isVisible(scope: ScopeRef, ctx: ScopeContext): boolean;
export interface Scoped {
    name: string;
    scope: ScopeRef;
}
/**
 * Resolve a flat list of scoped items into the effective set for `ctx`.
 * Invisible items are dropped; on a name collision the narrowest scope wins.
 */
export declare function resolveScoped<T extends Scoped>(items: readonly T[], ctx: ScopeContext): T[];
