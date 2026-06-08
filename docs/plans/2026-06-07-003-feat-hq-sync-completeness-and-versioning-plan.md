---
title: 'feat: End-to-end HQ sync completeness + catalog versioning'
type: feat
status: active
date: 2026-06-07
depth: deep
---

# feat: End-to-end HQ sync completeness + catalog versioning

## Summary

Two intertwined gaps block "update Command HQ skills, MCP, and agents from claude+ in
any project":

1. **Sync is not end-to-end.** `claude+ sync-skills` writes a *partial* install: a
   skill is materialized as a lone `SKILL.md` (sibling scripts/resources dropped), an
   agent is copied without a client-side guarantee of its dependent skills, MCP servers
   land in `~/.claude+/roots/<slug>/.mcp.json` but are never registered in Claude's
   `enabledMcpjsonServers` approval list and have no auth path, and **plugin
   registration (`installed_plugins.json`) is entirely unimplemented** — so a pulled
   bundle's commands never appear in `/`-autocomplete. There is no verification gate, so
   a half-finished install reports success.

2. **The catalog has no versioning.** Every write is a blind upsert-by-name
   (`putSkill`/`putAgent`/`putMcpServer`), so a project that edits a shared org item and
   re-registers **overwrites it for the entire org** with no history and no fork. This
   makes "a project making changes" unsafe, which is the root reason there is no
   `hq-add-mcp` / `hq-update-agent` authoring skill yet.

This plan (a) finishes the sync so every enabled item is *usable*, not just *present*,
with a fail-loud verification gate; (b) introduces an immutable **version** model where
a change cuts a new version and a project's edit **forks** rather than clobbers; (c) adds
`hq-add-mcp` and `hq-update-agent` authoring skills; and (d) extends the seed so agents
and MCP servers are seeded org-wide (today only skills/bundles are).

**Honesty up front (a hard constraint, not a TODO):** completing an OAuth handshake for
the claude.ai connectors (Gmail, Drive, Calendar, M365) **cannot be automated headlessly**
— OAuth requires interactive, browser-based user consent by design. Sync will *register*
those servers, *report* them as needing auth, and *wire the interactive flow*; it will
**not** claim to auto-complete OAuth. See KTD5 and Non-Goals.

---

## Problem Frame

Command HQ curates skills/agents/MCP in an org catalog; a connected `claude+` daemon
materializes the project's opted-in set into its per-project config root
(`~/.claude+/roots/<projectSlug>/`, the layout from the per-project-root change). Two
layers are incomplete:

- The **wrapper** copies the minimum bytes (a `SKILL.md` body, an agent `.md`, an
  `.mcp.json` entry) and stops — it never finishes the setup that makes the item load and
  run, and it has no concept of plugins at all.
- The **catalog** treats every item as a single mutable name-keyed record, so the moment a
  project customizes a shared item, the safe options are "don't" or "overwrite everyone."

The result the user hit: in `fractions_tutorial`, `/hq-*` commands don't autocomplete
(plugin not registered), bundle skills weren't materialized, and MCP connectors are
registered-but-dead. And there is no safe way for a project to evolve a shared skill.

### Current-state facts (traced, with cites)

- Skill write is `SKILL.md` only: `wrapper/internal/config/claude.go` ApplyPulled
  `KindSkill` → `skills/<name>/SKILL.md`; HQ serves one `body` string per skill
  (`remote.go` `remoteSkill`). No sibling files anywhere.
- Agent deps rely entirely on HQ pre-unioning skills into `project.enabledSkills`
  (`remote.go` Fetch comment); the wrapper parses only `name/description/model/tools`
  frontmatter — no `skills:`/`dependsOn`.
- MCP → `.mcp.json` only (`mcp.go` `mcpFileName = ".mcp.json"`); **zero** handling of
  `.claude.json` mcpServers, `enabledMcpjsonServers`, `mcp-needs-auth-cache.json`, or any
  OAuth. (Note the cross-doc inconsistency: plan 002 says the approvals MCP lives in
  `~/.claude+/.claude.json`.)
- Plugins: **no** `installed_plugins.json`, `enabledPlugins`, marketplace, or plugin-cache
  code in the wrapper at all.
- Reconcile actuates only `needs_pull` / `needs_push`; `differs` (an edited item) is
  silently skipped (`sync.go`).
- Catalog writes are blind upserts keyed by `(scope, name)` with no version
  (`repo.ts` `putSkill`/`putAgent`/`putMcpServer`; `keys.ts` `SKILL#<name>` etc.). No
  `version`/`createdAt`/`history` field on any of the three schemas (`dto.ts`).
- General seed (`seed-skills.mjs`, `seed-all-orgs.mjs`, org-create clone in `starter.ts`)
  seeds **skills + bundles only**. Agents/MCP are seeded only by ad-hoc, single-org
  HumanLayer scripts.
- The `command-hq-starter` bundle is cloned into every **org** catalog on create but is
  **not** auto-enabled on any **project**.

---

## Key Technical Decisions

### KTD1 — Skills materialize as whole directories, end to end

HQ must store and serve a skill as a **file set**, not a single body. Add a `files` map
(relative path → contents) to the skill record (or a manifest + per-file rows for large
assets — see U-Skill-Store). ApplyPulled writes the whole `skills/<name>/` tree; ReadLocal
hashes the tree (not just `SKILL.md`) so sibling edits register as drift. Back-compat: a
record with only `body` still materializes `SKILL.md` (legacy path).

### KTD2 — Agents carry, and sync enforces, their skill dependencies

The agent schema already has `skills: string[]` (`dto.ts`). Make the wrapper **enforce**
it client-side: after writing an agent, ensure every name in its `skills[]` is present in
`<root>/skills/`; pull any missing one. This removes the reliance on HQ having pre-unioned
`enabledSkills` and is the only place that can guarantee an agent is runnable.

### KTD3 — MCP config goes where Claude actually reads it, plus the approval list

Resolve the `.mcp.json` vs `.claude.json` inconsistency definitively (U-MCP-Target spikes
this against a real claude+ machine): write project/file MCP servers into the location
Claude loads, and **add file-based servers to `enabledMcpjsonServers`** in
`<root>/.claude.json` so they aren't left disabled. Merge, never clobber. Surface, but do
not block on, servers that additionally need auth.

### KTD4 — Plugin registration is reconciled into `installed_plugins.json`

The wrapper gains a plugin reconcile step: for every `<plugin>@<marketplace>` that is (a)
enabled in `<root>/settings.json` `enabledPlugins` and (b) present in
`<root>/plugins/cache/<marketplace>/<plugin>/<version>/`, ensure an entry in
`<root>/plugins/installed_plugins.json` with `scope`, `installPath`, `version` (from the
cached `.claude-plugin/plugin.json`), and `gitCommitSha` (the marketplace clone HEAD).
This is the fix for missing `/`-autocomplete. (Open question: are `hq-*` delivered as a
plugin, as catalog skills, or both? U-Plugin-Spike answers this before we build — see
Risks R2.)

### KTD5 — MCP auth is *reported and wired*, never auto-completed

A server in `mcp-needs-auth-cache.json` is registered-but-unauthenticated. Sync will:
ensure the launch dependency resolves, populate required static env/headers, then **for
OAuth connectors, emit an explicit "needs interactive login" result** and surface the
exact command for the user to run (Claude Code's `/mcp` auth flow). The verification gate
counts such servers as `needs-auth`, not `failed` and not `ok`. **We do not implement an
unattended OAuth handshake** — it is infeasible and a security anti-pattern.

### KTD6 — Versioning: a version is `(name, repo, person)`; promote one as "true"; repo picks via a dropdown

Today identity is `(scope, name)` and every write clobbers. The model:

- **Version identity = `(name, repo, person)`.** When person `P` working in repo `R` edits
  an existing skill `S`, that edit **forks a new variant** keyed by `(S, R, P)` — it never
  touches the original or anyone else's. A different person `Q` in the same repo `R` editing
  `S` produces a separate variant `(S, R, Q)`; the same person in a different repo produces
  another. The three dimensions — **skill name, repo, person** — together address a variant.
  (Same for agents and MCP servers.)
- **Stored as separate records.** Each variant is a normal catalog record under the hood —
  there is no special "version table." Proposed key: `SKILL#<name>#R#<repoId>#U#<userId>`
  under the org partition (the seeded/original is the base variant, e.g. `R=_, U=_`, or its
  existing `SKILL#<name>` row). This keeps every layer that reads a skill record unchanged;
  only the *addressing* and a *resolver* are new.
- **One org-wide "true" version per skill name, promoted in the UI.** A `trueVersion`
  pointer per `<name>` (a small canonical/LATEST row, e.g. `SKILL#<name>#TRUE → <variantId>`)
  names the single org-wide variant shown by default in the catalog UI and used as the
  default when a project adds the skill; repos override it per-repo via the dropdown.
  **Any authed org member may promote** any variant to "true" (decided — not admin-gated);
  promotion only repoints `trueVersion`, never editing or deleting a variant.
- **Per-repo selection (the dropdown).** A project's enabled-set entry records *which
  variant* it uses: `{ name, variantId }`. Adding a skill defaults `variantId` to the
  current `trueVersion`; a per-skill dropdown in the project UI lets that repo switch to any
  other variant of the same name. Sync materializes the repo's chosen variant. (This
  supersedes the earlier scope-tier "shadow" sketch with explicit per-repo selection.)
- **Edits within a variant are snapshotted (decided).** Repeated edits by the same
  `(name, repo, person)` append an immutable revision `…#r<N>`; the variant's current content
  is its latest revision, and the full `[r1…rN]` history is kept for rollback/audit. The
  `trueVersion` pointer and a project pin resolve to a specific revision.
- **Seed/base variant.** The org-seeded skill is the base variant and the initial
  `trueVersion`. Re-running the seed updates the base variant only (hash-compare no-op
  otherwise) and never changes `trueVersion` once an admin has promoted a different one.

This makes "a project making changes cuts its own version" the default behavior, keeps the
shared catalog safe (no silent org-wide clobber), and gives the UI a single canonical
default plus a per-repo override.

### KTD7 — Authoring skills + seed cover all three kinds, version-aware

Add `hq-add-mcp` (net-new; thin authoring skill over the existing `/mcp-servers` REST) and
`hq-update-agent` (edit an agent + its `skills`/`mcpServers`), both written to cut a new
version via the KTD6 flow. Extend the general seed (`seed-all-orgs.mjs` / a new
`buildSeedAll`) to seed **agents and MCP servers** org-wide, not just skills — and stamp
versions. Reconcile the scope-model contradiction in the existing docs (hq-add-skill/
hq-update-skills claim "no scope tiers" while the code is firmly 3-tier).

---

## Implementation Units

> Sequencing favors shipping the **completeness** fixes (immediately unblocks the user)
> before the **versioning** model (larger, schema-migrating). Each unit lists whether it
> blocks the user's stated need.

### Phase A — End-to-end sync completeness (unblocks the user)

**U-Plugin-Spike** *(blocks build of U-Plugin-Reg).* Determine empirically how `hq-*` and
the `command-hq-starter` bundle are delivered to a project root today: catalog skills,
a Claude Code plugin (marketplace + cache + `installed_plugins.json`), or both. Inspect a
real `~/.claude+/roots/<slug>/` (plugins/, settings.json `enabledPlugins`,
installed_plugins.json) on an authenticated machine. Output: the exact registration record
shape and the delivery model. *No code until this resolves.*

**U-Skill-Dirs.** Skills materialize as whole directories.
- Backend/shared: add `files?: Record<string,string>` to `skillSchema`; REST serves it;
  seed reads the whole `.claude/skills/<name>/` tree.
- Wrapper: ApplyPulled `KindSkill` writes every file; ReadLocal hashes the tree (stable
  walk) so sibling edits drift. Legacy `body`-only records still write `SKILL.md`.

**U-Agent-Deps.** Wrapper enforces agent skill dependencies (KTD2): after writing an
agent, pull any name in its `skills[]` missing from `<root>/skills/`.

**U-MCP-Target.** Spike + implement the correct MCP write location (KTD3): write to the
location Claude loads, add file-based servers to `enabledMcpjsonServers` in `.claude.json`,
merge-not-clobber. Reconcile the `.mcp.json` vs `.claude.json` inconsistency in code + the
001/002 docs.

**U-MCP-Auth-Report.** Read/write `mcp-needs-auth-cache.json`: after writing an MCP server,
verify launch dependency + static env, and classify each server `ok` / `needs-auth` /
`failed`. Emit the interactive auth command for `needs-auth` (KTD5). No unattended OAuth.

**U-Plugin-Reg** *(depends on U-Plugin-Spike).* Wrapper reconciles
`installed_plugins.json` (KTD4) for every enabled+cached plugin.

**U-Verify-Gate.** A final verification pass over the effective enabled set asserting, per
item: skill dir + `name` frontmatter; agent file + valid frontmatter + its skills present;
MCP server `ok`/`needs-auth` (not silently `failed`); plugin registered. Emit a per-kind
table; **fail loudly** naming the item + missing piece. `claude+ sync-skills` exit code
reflects it. This is what turns "synced" into "set up."

**U-Reconcile-Differs.** Make Reconcile actuate `differs` (re-pull when the project pinned
a newer version / repo edit wins), instead of silently skipping it.

**U-Enable-Starter** *(THE immediate unblock — confirmed root cause).* The real reason
`/hq-*` doesn't work in a project like `fractions_tutorial` is that `command-hq-starter` is
**not in that project's enabled set** (`PROJ#fractions-tutorial` had `enabledBundles: []`,
`enabledSkills: 0`), so its skills never sync.
- **(a) Backfill existing projects — DONE (2026-06-07).** `command-hq-starter` + its 11
  flattened members written to `enabledBundles`/`enabledSkills` on both live projects
  (`fractions-tutorial`, `prove-it-livewatch`) via a direct catalog write. A device still
  must run an authenticated `claude+ sync-skills` to materialize them.
- **(b) Auto-enable at project-create — TODO (code unit).** Mirror the org-create starter
  clone into each new project's `enabledBundles`/`enabledSkills` so future projects get it
  without a manual backfill. (`projects.ts` create handler.)

### Phase B — Versioning model (makes project changes safe)

**U-Ver-Schema.** Add variant identity + provenance to `skillSchema`/`agentSchema`/
`mcpServerSchema` (`dto.ts`): `variantId` (derived from `name`+`repoId`+`userId`),
`baseName` (the shared skill name a variant belongs to), `repoId?`, `authorUserId?`,
`createdAt`. Legacy records become the **base variant** of their `name` (`repoId/authorUserId`
empty) with no behavior change.

**U-Ver-Keys.** Key scheme (KTD6): variant revision rows
`SKILL#<baseName>#R#<repoId>#U#<userId>#r<N>` (base variant = `SKILL#<baseName>#r<N>`), plus
a per-name **true pointer** `SKILL#<baseName>#TRUE → {variantId, rev}` (and the same for
`AGENT#`, `MCPSERVER#`). Repo gains `listVariants(name)`, `listRevisions(variantId)`,
`getRevision(variantId, rev)`, `getTrueVariant(name)`, `setTrueVariant`.

**U-Ver-Write (fork-on-edit + snapshot).** REST write from a project context forks/updates
the `(name, repo, person)` variant — never the base or another variant. A first edit creates
the variant's `r1`; each later edit by the same tuple appends `r<N+1>` (immutable history
kept). Add `POST /skills/:name/promote { variantId, rev? }` to repoint `trueVersion`, callable
by **any authed org member** (decided) — this replaces the retired 410 `/scope` verb.

**U-Ver-Pin (the dropdown).** Project enabled-set entries become `{ name, variantId }`;
enabling defaults `variantId` to the current `trueVersion`. Opt-in handlers + bundle flatten
carry the chosen `variantId`; the web project UI renders a per-skill variant **dropdown**
(default = true). Sync materializes the project's chosen variant and reports when the chosen
variant differs from a newer one / from `trueVersion`.

**U-Ver-Web.** Catalog UI shows the true variant by default with a variant switcher + a
"Promote to true" action; the add-to-project flow defaults to true and exposes the per-repo
dropdown (mirrors `ProjectSkills.tsx`).

**U-Ver-Seed.** Seed writes/updates only the **base variant** (hash-compare no-op
otherwise) and never overrides a promoted `trueVersion`; extend the general seed to agents +
MCP (KTD7).

### Phase C — Authoring skills

**U-Skill-hq-add-mcp.** New `hq-add-mcp` authoring skill over `/mcp-servers` REST
(structured `transport` record; plaintext-secret warning), version-aware.

**U-Skill-hq-update-agent.** New `hq-update-agent` skill to edit an agent + its
`skills`/`mcpServers`, cutting a new version.

**U-Docs-Reconcile.** Fix the scope-model contradiction across hq-add-skill /
hq-create-skill / hq-update-skills / hq-optimize-agent / hq-endforge so the docs match the
3-tier code and the new versioning flow.

---

## Scope Boundaries

**In scope:** end-to-end sync (skill dirs, agent deps, MCP target + approval list + auth
*reporting*, plugin registration, verification gate, `differs` actuation, starter-on-every-
project); immutable versioning with fork-on-edit + project pinning; `hq-add-mcp` /
`hq-update-agent`; seed extension to agents + MCP; doc reconciliation.

### Explicit Non-Goals
- **Unattended OAuth completion for MCP connectors.** Infeasible/insecure; sync reports
  needs-auth and wires the interactive flow (KTD5).
- **Secret encryption / KMS** for MCP env/headers — still plaintext per plan 001 KTD3.
- **MCP bundles** — servers stay flat (plan 001 KTD4).
- A general-purpose plugin **marketplace installer** in the wrapper beyond registering
  already-cached, already-enabled plugins (U-Plugin-Spike may expand or shrink this).

---

## Risks & Mitigations

- **R1 — Versioning is a schema + key migration on live data.** Mitigation: additive
  (`version` defaults to `v1`; legacy `SKILL#<name>` becomes the LATEST pointer); migrate
  lazily on first write; ship Phase A (no schema change) first so the user is unblocked
  before B lands.
- **R2 — Plugin delivery model — RESOLVED by inspection.** Inspecting a real
  `~/.claude+/roots/<fractions-slug>/` showed `hq-*` are **catalog skills, not plugin-
  delivered**; `installed_plugins.json` is populated and working for the only plugin present
  (third-party `compound-engineering`). So the wrapper does *not* need to manage `hq-*` via
  plugins, and U-Plugin-Reg shrinks to a low-priority "ensure already-cached, already-enabled
  third-party plugins are registered" (Claude Code already does this on interactive
  `/plugin install`). **The actual blocker for `/hq-*` in fractions is that
  `command-hq-starter` is not enabled on that project (U-Enable-Starter), so its skills never
  sync** — not a sync-completeness bug.
- **R3 — `.mcp.json` vs `.claude.json` target.** Wrong file = server silently never loads.
  Mitigation: U-MCP-Target spikes against a real claude+ root before implementing; pin a
  fixture from a known-good entry.
- **R4 — Can't fully verify headless.** This machine's claude+ may 401 on
  `sync-skills`, and OAuth/`/`-autocomplete need an interactive session. Mitigation: unit-
  test the wrapper reconcile + verification gate deterministically; treat live end-to-end
  verification as an interactive acceptance step, reported honestly (never claimed).
- **R5 — Catalog publish requires AWS seed creds + sign-off.** Mitigation: gate any live
  catalog seed behind explicit user approval; Phase A wrapper changes need no catalog
  write.
- **R6 — Project records have `org: undefined`.** Both live `PROJ#…` META records have no
  `org` field, so it is unclear which org catalog a device sync resolves the enabled set
  against. The recent fix "resolve opt-in catalog org from project/effective org" suggests a
  fallback to the device's effective org, but this must be verified end-to-end before the
  U-Enable-Starter backfill is declared effective (the enabled-set write itself is correct;
  resolution is the open risk). Mitigation: an authenticated `claude+ sync-skills` smoke
  test that confirms the 11 `hq-*` skills materialize into the project root.

---

## Open Questions (decide before building the relevant unit)

**Decided** (folded into KTD6 / Phase B): version identity = `(name, repo, person)`; each
variant stored as a separate record; **every edit snapshots an immutable revision** (history
kept); **one org-wide "true" version** per name (default to add/show) with **per-repo
dropdown override**; **any authed org member may promote** a variant to true.

**Resolved by inspection:** Plugin delivery (R2) — `hq-*` are catalog skills, not plugins;
the immediate `/hq-*` blocker is `command-hq-starter` not being enabled on the project
(U-Enable-Starter), now scoped as backfill-all + auto-enable-on-create.

**Resolved:** Backfill blast radius — enable on **all existing projects** (done, 2026-06-07;
both live projects). Remaining is the auto-enable-on-create code unit (U-Enable-Starter b).

No open questions block the build; the items above are decided. Remaining risks (R3 MCP
target, R6 org resolution) are spikes inside their units, not pre-build decisions.

---

## Build decomposition (for a Claude workflow)

This is the dependency graph a multi-agent build workflow can fan out over. Each node is a
self-contained unit with a verifiable exit (tests pass / gate green). Nodes on the same line
with no arrow between them are parallelizable.

```
PHASE A (no schema migration — ship first)
  U-Plugin-Spike ─┐                         (investigation; may delete U-Plugin-Reg)
  U-Skill-Dirs ───┤  shared+backend+wrapper, independent
  U-Agent-Deps ───┤  wrapper-only, independent
  U-MCP-Target ───┤  spike R3 → wrapper+backend
  U-MCP-Auth-Report┤ wrapper-only (depends loosely on U-MCP-Target)
  U-Reconcile-Differs┘ wrapper-only
        └────────────────► U-Verify-Gate  (depends on all Phase-A writers)
  U-Enable-Starter(b)  backend-only, independent  [(a) already done]

PHASE B (schema migration — versioning; after Phase A merges)
  U-Ver-Schema ──► U-Ver-Keys ──► U-Ver-Write ──► U-Ver-Pin ──► U-Ver-Web
                                          └────────► U-Ver-Seed

PHASE C (authoring skills + docs — after Phase B REST lands)
  U-Skill-hq-add-mcp   U-Skill-hq-update-agent   U-Docs-Reconcile   (parallel)
```

**Per-unit acceptance (what "done" means for the workflow's verification stage):**
- Wrapper units: `go build ./... && go test ./...` green, plus a unit test asserting the new
  behavior (e.g. whole-dir round-trip, agent-deps pulled, MCP in the right file, gate fails
  on a partial install).
- Backend units: `npm run build -w @harness/backend` + the package's vitest green; REST
  handler tests cover the new version/promote/pin paths and the fork-on-edit invariant.
- Versioning invariant tests (highest value): editing an org item from a project context
  creates a `(name, repo, person)` variant and does **not** mutate the base or another
  variant; promote repoints `trueVersion`; a project pin resolves to its chosen variant;
  re-seed does not reset a promoted true or bump an unchanged base.
- Live-catalog / OAuth / `/`-autocomplete checks are **interactive acceptance** steps,
  reported honestly — never asserted by an automated agent (R4).

**Workflow shape suggestion:** one workflow per phase (understand → implement-in-parallel →
adversarially-verify), reading results between phases, rather than one mega-run — Phase B's
schema migration should not start until Phase A is merged and green.

## Reconciliation with prior plans

- **001 (MCP servers tab):** landed; this plan extends it — adds seeding (001 deferred),
  the approval-list write + auth *reporting* (001 non-goaled OAuth), and resolves the
  `.mcp.json`/`.claude.json` target.
- **002 (HumanLayer ACE):** established `buildSeedAgents` + scoped agent/MCP seed scripts
  deliberately kept out of the org-wide path; this plan's U-Ver-Seed promotes a general,
  version-aware agent/MCP seed without disturbing 002's single-org containment.
