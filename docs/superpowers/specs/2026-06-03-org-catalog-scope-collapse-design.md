# Design Spec — Org-Catalog Skills+Agents with Per-Project Opt-In

**Date:** 2026-06-03
**Area:** `packages/shared` (types), `packages/backend` (REST + keys + seed), `packages/web` (Skills/Agents/Project screens), `wrapper` (config sync), docs + bundled skills.

## Status

FROZEN CONTRACT. Shared types are committed on branch `refactor/scope-collapse-shared` at commit `b9f4d33e26479e21be63722fd1b563df99de8cec` (off base `bfcd542`). All four slices (backend, web, wrapper, docs) build on this branch and implement against the shapes below WITHOUT seeing each other's code.

## 1. Authoritative model

- Skills AND agents collapse to a single **org catalog**. The 3-tier scope (org/user/project) + `resolveScoped`/`isVisible` are RETIRED for skills+agents. Every skill/agent is org-scoped (`scope.tier === 'org'`, `scope.id === orgId`).
- A Project opts into BOTH `enabledSkills: string[]` and `enabledAgents: string[]` (skill/agent names; default `[]`).
- A skill can be added to a project directly (no agent needed).
- Adding an agent to a project UNIONS that agent's declared `skills` into the project's `enabledSkills` (de-duped). Bundles among those skills expand transitively to leaf member skills before union.
- Every skill and agent records `createdBy: { userId, name }`, stamped from the authenticated principal on create.
- A connected device/repo resolves its linked project, then materializes ONLY that project's `enabledSkills` + `enabledAgents` into `~/.claude+`. Agents' skills are already present because they were union-added into `enabledSkills` when the agent was enabled.

## 2. Shared types (COMMITTED — do not re-edit; consume as-is)

File `packages/shared/src/scope.ts` (additions; existing 3-tier `ScopeRef`/`resolveScoped`/`isVisible`/`SCOPE_TIERS`/`SCOPE_PRECEDENCE` kept intact for non-catalog entities and the wrapper golden fixture):

```ts
export const orgScopeRefSchema = z.object({ tier: z.literal('org'), id: z.string().min(1) });
export type OrgScopeRef = z.infer<typeof orgScopeRefSchema>;
export function orgScope(org: string): OrgScopeRef; // returns { tier: 'org', id: org }
```

File `packages/shared/src/dto.ts`:

```ts
// NEW exported schema/type
export const createdBySchema = z.object({ userId: z.string().min(1), name: z.string().min(1) });
export type CreatedBy = z.infer<typeof createdBySchema>;

// projectSchema GAINS:
enabledSkills: z.array(z.string()).default([]),
enabledAgents: z.array(z.string()).default([]),
// later additions (2026-06-07), same default-[] back-compat:
enabledMcpServers: z.array(z.string()).default([]),
enabledBundles: z.array(z.string()).default([]),  // whole-bundle INTENT; members also in enabledSkills

// agentSchema CHANGED:
scope: orgScopeRefSchema,           // was scopeRefSchema; now org-only
createdBy: createdBySchema.optional(), // optional on read for back-compat, set on create

// skillSchema CHANGED:
scope: orgScopeRefSchema,           // was scopeRefSchema; now org-only
createdBy: createdBySchema.optional(),
```

Lowest-churn decision documented: the `scope` FIELD is KEPT on agent/skill (not removed) to avoid breaking every consumer (keys, repo, wrapper fixture, web). It is merely constrained to `orgScopeRefSchema` (tier literal `'org'`). `scopeChangeSchema` still references the full `scopeRefSchema` and is unchanged at the type level (backend retires the endpoint behaviorally). `Project.enabledSkills`/`enabledAgents` and `createdBy` are additive. Old records lacking these read as `[]` / `undefined` via Zod defaults/optionals.

## 3. REST contract (org-only writes + project opt-in)

Principal source: `principalOf(event)` yields `{ userId, org, name? }`; admin via `isAdmin(event)` (`custom:admin` claim). `createdBy = { userId: principal.userId, name: principal.name ?? principal.userId }`.

### Skill/agent catalog (org-only)

- `GET /skills` -> `Skill[]` for caller's org (org scope only; no `?project`, no resolveScoped). Auth: belongs to org.
- `GET /skills/:name` -> `Skill`.
- `POST /skills` body = `Skill` minus `scope`/`createdBy` (server forces `scope = orgScope(principal.org)`, stamps `createdBy`). Auth gate: admin of own org. Response: created `Skill`.
- `PUT /skills/:name` -> update (scope/createdBy immutable; createdBy preserved). Auth: admin of org.
- `DELETE /skills/:name` -> `204`. Auth: admin of org.
- Bundle subroutes unchanged behaviorally: `POST /skills/:name/members`, `DELETE /skills/:name/members/:member`, `POST /skills/:name/dissolve`. `flattenBundle` transitive + cycle-guarded stays.
- Agents mirror skills exactly: `GET /agents`, `GET /agents/:name`, `POST /agents`, `PUT /agents/:name`, `DELETE /agents/:name`. Keep `agent.skills[]`.
- RETIRE `changeAgentScope`/`changeSkillScope` (the scope-change endpoint): respond `410 Gone` (or remove route). No tier elevation/demotion exists in the org catalog.

### Project opt-in (project owner OR org admin)

Auth gate for all four: `isAdmin(event) || project.ownerUserId === principal.userId`.

- `POST /projects/:projectId/skills/:skillName` -> idempotent add to `enabledSkills`. Response: updated `Project`.
- `DELETE /projects/:projectId/skills/:skillName` -> remove from `enabledSkills`. Response: updated `Project`.
- `POST /projects/:projectId/agents/:agentName` -> add to `enabledAgents` AND union the agent's `skills` (bundles flattened to leaves) into `enabledSkills` (de-duped). Response: updated `Project` (reflects both new arrays).
- `DELETE /projects/:projectId/agents/:agentName` -> remove from `enabledAgents`. Does NOT prune `enabledSkills` (a skill may be enabled directly or brought by another agent). Response: updated `Project`.
  Validation: skillName/agentName must exist in the org catalog (else `404`).

> **Extension (2026-06-07): bundle-as-unit opt-in + MCP servers.** Two later additions follow the same admin-or-owner gate and read-modify-write-the-`Project`-META shape:
>
> - `POST|DELETE /projects/:projectId/mcp-servers/:name` — add/remove a catalog MCP server on `Project.enabledMcpServers` (flat; no bundle concept). See the [MCP Servers plan](../../plans/2026-06-07-001-feat-mcp-servers-tab-plan.md).
> - `POST|DELETE /projects/:projectId/bundles/:bundleName` — enable/disable a whole **bundle as a unit**, recorded on the new `Project.enabledBundles: string[]` field (default `[]`). `enabledBundles` is an **intent annotation** layered over the flat `enabledSkills` materialization set: the bundle's member skills are ALWAYS also unioned into `enabledSkills` (so the daemon materialization is unchanged), while `enabledBundles` only records that the user added the _whole_ bundle — letting the UI distinguish whole-bundle from individually-picked members. `bundleName` must resolve to a catalog entry of `kind: 'bundle'` (a leaf-skill or unknown name is `404`); the REST layer flattens the bundle transitively to leaves (where the catalog is loaded) so the repo stays catalog-agnostic. **On DELETE, member accounting:** clear the bundle intent and strip the leaves it contributed from `enabledSkills`, EXCEPT any leaf still covered by another still-enabled bundle (a member shared between two enabled bundles survives). A bundle that has vanished from the catalog still clears its intent (no leaves to strip), so a project can never be stuck holding a dead bundle reference. Code: `rest/projects.ts` (`enable/disableProjectBundle`), `db/repo.ts` (`add/removeBundleFromProject`).

## 4. Key design (DynamoDB, single table)

- Agent/skill keys become org-fixed. Today `agentKey(scope, name) -> { PK: SCOPE#${scopeId(scope)}, SK: AGENT#${name} }`. Backend should call them with `orgScope(org)` so PK is always `SCOPE#org#${org}`; `scopePartition(orgScope(org))` lists the catalog. (No physical key shape change is required — only org scope is ever passed.)
- Project opt-in stored on the project META record fields `enabledSkills`/`enabledAgents` (single read with the project). No separate items required; if the backend slice prefers separate items it may add `projectEnabledSkillsKey`/`projectEnabledAgentsKey` (`PK: PROJ#${id}`, `SK: ENABLED_SKILLS|ENABLED_AGENTS`) but the REST response must still return a hydrated `Project` with both arrays populated. RECOMMENDED: store on META for atomic single-read.

## 5. Wrapper sync algorithm

1. `HTTPRemoteSource.Fetch()` calls `GET /agents` and `GET /skills` (no `?project`, no scope resolution server-side). HQ returns ALL org items, each `scope:{tier:'org',id:orgId}`.
2. Resolve linked project via existing `projectIDFor(repoRoot)`; fetch the project's `enabledSkills` + `enabledAgents` (via `GET /projects/:id`).
3. Compute the effective set = items whose name is in `enabledSkills` (skills) or `enabledAgents` (agents). Agents' own skills need no extra expansion at sync time (already union-added into `enabledSkills` server-side).
4. Drift = `Diff(local, effectiveSet)`. Reconcile pulls HQ-only effective items into `~/.claude+` only (never `~/.claude`); pushes local-only items to HQ at org scope. Once-per-session + 30s drift poll unchanged. Collision rule (`~/.claude` wins, `~/.claude+` backfills) unchanged.

## 6. Web changes

- Skills.tsx: remove ScopePicker + tier grouping; flat org catalog. Add author-filter facet (`createdBy.name`) + keep free-text name search.
- Agents.tsx: same removal; add author filter + free-text search. Remove promote-to-org affordance.
- AgentEditor.tsx: remove scope selector (server forces org).
- SkillCombobox.tsx: add an author filter facet alongside the existing type-to-filter; hint shows author name.
- ProjectLayout.tsx + router.tsx: add `Skills` subtab at `/projects/:projectId/skills`.
- New ProjectSkills.tsx: lists `project.enabledSkills` (rendered through the shared `SkillCatalog` so bundles drill into members the same as the global Skills screen). Add/remove now flows through a shared **CatalogPicker** modal (`components/CatalogPicker.tsx`), not a bare combobox: a single **"+ Add to project"** button opens a staging modal over the org catalog where the user toggles whole bundles or individual skills, and on **Apply** the staged diff fans out to the per-type opt-in mutations. Because each mutation read-modify-writes the whole `Project` META record, the diff is applied **SEQUENTIALLY** (await each) — enable bundles before skills, disable skills before bundles. The same modal is reused on the Project **MCP Servers** and **Agents** tabs.
- ProjectAgents.tsx: add enable/disable toggles -> `POST|DELETE /projects/:id/agents/:name`; on enable, show which skills the agent brings (its `skills`, bundles flattened) and that they auto-add to enabledSkills.
- baseApi.ts: drop `changeAgentScope`/`changeSkillScope`; `getAgents`/`getSkills` drop `projectId` param (org catalog); add `enableProjectSkill`/`disableProjectSkill`/`enableProjectAgent`/`disableProjectAgent` mutations (invalidate Project + their tags).

## 7. Backward compat / migration

No data migration. Old project records read `enabledSkills/enabledAgents` as `[]`. Old skill/agent records read `createdBy` as `undefined`. Existing scoped (user/project) skill/agent records simply stop being returned by the org-only GETs; they are inert. Seed (`seed/skills.ts`) stamps `createdBy:{userId:'system',name:'system'}` and `scope: orgScope(org)`.
