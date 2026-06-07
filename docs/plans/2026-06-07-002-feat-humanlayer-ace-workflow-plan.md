---
title: "feat: Adopt HumanLayer ACE as Command HQ's engineering workflow"
type: feat
status: active
date: 2026-06-07
depth: deep
---

# feat: Adopt HumanLayer ACE as Command HQ's engineering workflow

## Summary

Import **everything HumanLayer ships** — all 27 of its bundled slash-commands and all 6 of its research subagents — into the Command HQ org catalog as the canonical engineering workflow, replacing whatever currently fills that role. Commands become HQ **skills** (the wrapper has no `commands/` concept — only `skills/` and `agents/` materialize), subagents become HQ **agents**, and the set ships as a new `humanlayer-ace` bundle registered to a **single org** (initially `test org`) via a dedicated scoped seed script — deliberately kept **out** of the shared `.claude/skills/bundles.json` so it never propagates org-wide. The seeder is extended to register **agents** (today it seeds skills only; agents are REST-only), establishing a repo-managed agent convention that parallels skills.

HumanLayer's human-in-the-loop **approvals MCP** already works locally in `claude+` (registered in `~/.claude+/.claude.json`), so HITL is dogfoodable today. Its *org-wide distribution* is the only piece that waits on the in-flight MCP-servers catalog (`docs/plans/2026-06-07-001-feat-mcp-servers-tab-plan.md`) — and that is a clean consuming seam, not a blocker. This plan ships the full workflow layer independently; approvals distribution slots in as a thin follow-up.

The third-party `compound-engineering` plugin is **unrelated** to this work and is removed as part of the cleanup, not treated as a source.

---

## Problem Frame

Command HQ curates skills and agents in an org catalog and syncs the opted-in set down to each connected `claude+` daemon's isolated `~/.claude+` config root. Today the catalog has Command HQ's own *platform* tooling (the `command-hq-starter` bundle: forge, weekly-update, progress, skill authoring) but **no engineering workflow** — no research, plan, implement, validate, commit loop. Developers who want one install the third-party `compound-engineering` plugin per-machine (the repo's `compound-engineering` skill is just an installer for it, seeded at narrow user scope and deliberately excluded from the org default — `packages/backend/src/seed/skills.ts:74-78`).

HumanLayer has invested heavily in this exact problem ("Advanced Context Engineering for Coding Agents"): a research → plan → implement → validate workflow built on parallel research subagents, plus a human-in-the-loop approval mechanism for high-stakes operations. The decision is to adopt that workflow wholesale as Command HQ's engineering workflow rather than build or curate one. This plan imports it through the existing catalog machinery so every opted-in project gets it, and wires it to the approvals MCP so HITL is available now and distributes org-wide when the MCP catalog lands.

---

## Source Inventory

**Location:** `C:\Users\mattd\AppData\Roaming\npm\node_modules\humanlayer\.claude\` (npm `humanlayer@0.17.2-npm`). Paths below are repo-relative *targets*; the source is this read-only package directory.

**27 commands → skills** (`.claude/commands/<name>.md`, frontmatter `description` + optional `model`):
`ci_commit`, `ci_describe_pr`, `commit`, `create_handoff`, `create_plan`, `create_plan_generic`, `create_plan_nt`, `create_worktree`, `debug`, `describe_pr`, `describe_pr_nt`, `founder_mode`, `implement_plan`, `iterate_plan`, `iterate_plan_nt`, `linear`, `local_review`, `oneshot`, `oneshot_plan`, `ralph_impl`, `ralph_plan`, `ralph_research`, `research_codebase`, `research_codebase_generic`, `research_codebase_nt`, `resume_handoff`, `validate_plan`.

**6 subagents → agents** (`.claude/agents/<name>.md`, frontmatter `name` + `description` + `tools` + `model`):
`codebase-analyzer` (sonnet; Read, Grep, Glob, LS), `codebase-locator` (sonnet; Grep, Glob, LS), `codebase-pattern-finder` (sonnet; Grep, Glob, Read, LS), `thoughts-analyzer` (sonnet; Read, Grep, Glob, LS), `thoughts-locator` (sonnet; Grep, Glob, LS), `web-search-researcher` (sonnet; WebSearch, WebFetch, TodoWrite, Read, Grep, Glob, LS).

No name collisions exist with the current catalog (`hq-*`, `gstack`, `playwright-cli`, `compound-engineering`).

---

## Key Technical Decisions

### KTD1 — Commands import as skills, not commands
The wrapper materializes only `skills/` and `agents/` under `~/.claude+` (`wrapper/internal/config/overlay.go:72`, `claude.go:62-73`); there is no `commands/` directory and the REST surface is `/skills` + `/agents` only. Each HumanLayer command therefore becomes `.claude/skills/<name>/SKILL.md`: the markdown body is preserved verbatim; frontmatter is rewritten from `description` (+ `model`) to the skill shape `name` + `description`. The `description` is augmented with trigger phrasing per the repo's authoring convention (`.claude/skills/hq-create-skill/SKILL.md`) so the skill is discoverable.

### KTD2 — Preserve HumanLayer's original names
HumanLayer's commands cross-reference each other by name (e.g. `oneshot` launches `create_plan`, `create_worktree` launches `implement_plan`) and its commands invoke its subagents by their kebab names (`codebase-locator`, etc.). Renaming to the repo's kebab-case convention would silently break those references throughout the bodies. Decision: **keep the source names** — snake_case for the command-derived skills, kebab-case for the agents — accepting snake_case skill names as a deliberate exception to the kebab convention. Rationale beats convention here: an intact workflow is the whole point of adopting HumanLayer's research.

### KTD3 — Model pins survive on agents, not on skills
`skillSchema` (`packages/shared/src/dto.ts:207-237`) has **no `model` field**, so the `model: opus` pin on planning/research commands is dropped — those skills run at the session's model. `agentSchema` (`dto.ts:182-204`) **does** carry `model` (and `tools`), and per KTD8 those fields now *render* into the materialized subagent's frontmatter rather than being stored-but-discarded — so the 6 subagents keep AND apply their `sonnet` pin and declared `tools`. This is an accepted fidelity loss for the command (skill) layer only (see Risks R2); the workflow still functions, it just isn't model-pinned per skill.

### KTD4 — Extend the seeder to register agents
The seed registers skills only (`buildSeedSkills`/`seedSkills`, `packages/backend/src/seed/skills.ts`); agents are REST-only today and the repo has no `.claude/agents/` directory. To make the 6 agents reproducible and repo-managed (not one-off REST calls), add a parallel `buildSeedAgents`/`seedAgents` that reads `.claude/agents/*.md`, parses frontmatter (`name`, `description`, `tools`, `model`) plus body, and emits `agentSchema` records (`prompt` = body, `tools` = parsed list, `skills` = `[]`). This is the meatiest backend change and the only net-new mechanism; everything else reuses existing paths.

### KTD5 — Register `humanlayer-ace` to a single org (not org-wide); keep the platform bundle
The imported skills are grouped into a new `humanlayer-ace` bundle and registered at **org scope for one org** (initially `test org`) via a dedicated script (`infra/scripts/seed-humanlayer-ace.mjs`). Crucially it does **not** add the bundle to the shared `.claude/skills/bundles.json`: that manifest feeds both the `acme` template's clone-on-create (`starter.ts`) and the every-org backfill (`seed-all-orgs.mjs`), either of which would push the set org-wide. Keeping it out of the manifest — and registering only against the explicit target org — contains it to that org's catalog and nowhere else. The script reuses the canonical `buildSeedSkills` builder (parity-safe) with an in-memory manifest. The existing `command-hq-starter` platform bundle (forge/weekly/progress/skill-authoring) **stays** untouched. The 6 agents register at org scope for the same org (agents have no bundle concept; `addAgentToProject` unions their declared skills on opt-in — `repo.ts:640`). *(Status: the 27 skills + bundle are already registered to `org#test org`; see U4.)*

### KTD6 — Approvals: local now, org-distributed via the MCP catalog seam
The `humanlayer-approvals` MCP (`command: humanlayer`, `args: ["mcp","claude_approvals"]`) is already registered at `~/.claude+/.claude.json` top-level `mcpServers`, so `claude+` sessions can call `mcp__humanlayer-approvals__request_permission` today. High-stakes skills (`commit`, `implement_plan`, `create_worktree`, `debug`) get a small shared approval directive referencing that tool by name, degrading gracefully when it is absent. **Org-wide distribution** is explicitly deferred to consume `docs/plans/2026-06-07-001-feat-mcp-servers-tab-plan.md` (its KTD6 merges catalog MCP servers into `~/.claude+/.mcp.json`; its agent union-on-add attaches servers via agents) — once that ships, register `humanlayer-approvals` as a catalog MCP record and attach it. This plan is **not blocked** on that work.

### KTD7 — Remove the `compound-engineering` installer skill
It is an installer for an unrelated third-party plugin and has no role once HumanLayer is the workflow. Delete `.claude/skills/compound-engineering/` and scrub its references from seed comments and the user-grant fallback list (`packages/backend/src/seed/skills.ts`, `starter.ts`).

### KTD8 — An agent is a structured record rendered to subagent frontmatter
An HQ agent is **not** a bare prompt — it is a structured record (`name`, `description`, `model`, `tools`, `prompt`) that *renders* to a valid Claude Code subagent file, exactly as a skill record renders to its full `SKILL.md`. Today the wrapper writes only the raw `prompt` to `~/.claude+/agents/<name>.md` with no frontmatter (`remote.go` caches `a.Prompt` as the body and hashes it; `claude.go:178-199` writes it verbatim), so the stored `model` and `tools` are **discarded** and there is no `description` field at all (`agentSchema`, `dto.ts:182-204`). This unit fixes the definition: `description` is added to the schema as the **mandatory delegation trigger** Claude Code uses to decide which subagent to invoke, and the wrapper materializes the agent as `--- name/description/tools/model --- + prompt` (omitting `tools` when empty and `model` when `""`, including `"inherit"`), hashing that **same** rendered string for drift parity. This mirrors the MCP-server *structured-record-rendered-to-config* pattern (plan 001's KTD1/KTD6: a structured catalog record reconstructed into an on-disk `.mcp.json` entry, hashed canonically on both sides). Model + tools are no longer stored-but-discarded — they render. Reference: `agentSchema` (`dto.ts:182-204`), the wrapper materialization (`remote.go`), and the subagent frontmatter shape Claude Code reads (`claude.go:132-149`).

---

## High-Level Technical Design

### Component parallel — what each source maps to

| HumanLayer source | HQ catalog record | Materialized at | Registration path |
|---|---|---|---|
| `commands/<cmd>.md` (description, model) | Skill (`kind:skill`, body=full md) | `~/.claude+/skills/<cmd>/SKILL.md` | seeder (extended bundle) |
| `agents/<name>.md` (name, desc, tools, model) | Agent (name, **description**, model, tools[], prompt) | `~/.claude+/agents/<name>.md` = **frontmatter (name/description/tools/model) + prompt** (rendered, KTD8) | seeder (**new** `seedAgents`) |
| approvals MCP (stdio) | *(MCP catalog record — plan 001)* | `~/.claude+/.mcp.json` | deferred follow-up |

### Registration + materialization data flow

```mermaid
flowchart LR
  subgraph repo[".claude/ in repo"]
    S[skills/*/SKILL.md<br/>27 imported]
    A[agents/*.md<br/>6 imported]
    B[seed-humanlayer-ace.mjs<br/>scoped to one org]
  end
  subgraph seed["seeder (extended)"]
    SS[seedSkills]
    SA[seedAgents — NEW]
  end
  DB[(DynamoDB<br/>org catalog)]
  subgraph hq["HQ web / REST"]
    P[project opt-in<br/>enabledSkills/enabledAgents]
  end
  W[claude+ wrapper<br/>remote.go → ApplyPulled]
  subgraph cfg["~/.claude+"]
    CS[skills/*/SKILL.md]
    CA[agents/*.md]
    CM[".mcp.json<br/>(approvals — via plan 001)"]
  end
  SESS[claude+ session]

  S --> SS --> DB
  A --> SA --> DB
  B --> SS
  DB --> P --> W
  W --> CS & CA
  CM -. "deferred: MCP catalog" .-> SESS
  CS & CA --> SESS
```

The only novel edge is `seedAgents` (NEW); every other edge already exists for skills. The dashed approvals edge is owned by plan 001 and consumed later. Note the `W --> CA` edge: the wrapper materializes each agent as a **rendered subagent file** — frontmatter (`name`/`description`/`tools`/`model`) **+ prompt**, hashed over that same rendered string (KTD8) — not a bare prompt; this is what makes the stored `model`/`tools` actually take effect on disk.

---

## Output Structure

```
.claude/
  skills/
    ci_commit/SKILL.md
    commit/SKILL.md
    create_plan/SKILL.md
    create_plan_nt/SKILL.md
    implement_plan/SKILL.md
    research_codebase/SKILL.md
    validate_plan/SKILL.md
    ... (27 total, original snake_case names)
    bundles.json            # UNCHANGED — humanlayer-ace deliberately NOT added here
infra/scripts/
  seed-humanlayer-ace.mjs   # NEW: scoped registration to one org (test org)
  agents/                   # NEW repo-managed agents directory
    codebase-analyzer.md
    codebase-locator.md
    codebase-pattern-finder.md
    thoughts-analyzer.md
    thoughts-locator.md
    web-search-researcher.md
packages/backend/src/seed/
  agents.ts                 # NEW: buildSeedAgents / seedAgents
  skills.ts                 # scrub compound-engineering grant reference
  starter.ts                # seed agents into new orgs
```

The per-unit Files lists remain authoritative; the implementer may adjust layout if a better one emerges.

---

## Implementation Units

### U1. Import the 27 commands as skills
**Goal:** Every HumanLayer command exists as a repo-managed HQ skill with valid frontmatter and verbatim body.
**Requirements:** Adopts the full HumanLayer command set (KTD1, KTD2).
**Dependencies:** none.
**Files:** `.claude/skills/<name>/SKILL.md` × 27 (names per Source Inventory).
**Approach:** For each command, create `skills/<name>/SKILL.md`. Body = the source markdown verbatim. Frontmatter = `name: <name>` (matching the directory) + a folded `description` that preserves the source description and adds trigger phrasing ("Use when the user says `/<name>`…") so the skill is discoverable. Drop the `model:` key (KTD3). Do **not** rewrite intra-body references to other commands/agents — KTD2 keeps names stable so they stay valid.
**Patterns to follow:** Frontmatter shape and folded-description style of existing skills (`.claude/skills/hq-init/SKILL.md`); the minimal parser the seed relies on (`infra/scripts/seed-skills.mjs` `parseFrontmatter`).
**Test scenarios:** Test expectation: none — content import, no behavior. Verified structurally in U7 (every SKILL.md parses through `parseFrontmatter` yielding a non-empty `name` + `description`).

### U2. Vendor the 6 subagents as repo-managed agent files
**Goal:** Establish `.claude/agents/` and populate it with the 6 research subagents, frontmatter intact.
**Requirements:** Adopts HumanLayer's research subagents (KTD2, KTD4, KTD8).
**Dependencies:** U8 (the agent record + render contract must exist before agent files are vendored against it).
**Files:** `.claude/agents/{codebase-analyzer,codebase-locator,codebase-pattern-finder,thoughts-analyzer,thoughts-locator,web-search-researcher}.md`.
**Approach:** Copy each subagent file verbatim, preserving `name`, `description`, `tools`, `model` frontmatter and body. Keep kebab names (KTD2) — the imported skills reference these exact names. This directory is new; it is the source the extended seeder reads in U3.
**Patterns to follow:** The agent record shape in `packages/backend/test/agents.test.ts:37-39` (`{ name, scope, model, prompt, skills, tools }`).
**Test scenarios:** Test expectation: none — content vendoring. Frontmatter parse is exercised by U3's tests.

### U3. Extend the seeder to register agents
**Goal:** Agents seed from `.claude/agents/*.md` the way skills seed from `.claude/skills/*`, so the 6 subagents land in the org catalog reproducibly.
**Requirements:** KTD4 — durable, repo-managed agent registration.
**Dependencies:** U8, U2.
**Files:** `packages/backend/src/seed/agents.ts` (new), `packages/backend/src/seed/starter.ts` (seed agents on `POST /orgs`), `infra/scripts/seed-skills.mjs` and `infra/scripts/seed-all-orgs.mjs` (read+register agents alongside skills), `packages/backend/test/seedAgents.test.ts` (new).
**Approach:** Add a pure `buildSeedAgents(org, files)` mirroring `buildSeedSkills` (`seed/skills.ts:80-123`): parse each agent file's frontmatter (`name`, `description`, `tools` → string[], `model`) + body, emit `agentSchema.parse({ name, scope: orgScope(org), model, description, prompt: body, tools, skills: [] })` with `createdBy:{userId:'system',name:'system'}`. The seeder ALSO parses and seeds `description` from the source agent file frontmatter (it already parses `tools`/`model`) so the materialized subagent carries its delegation trigger (KTD8); a source file lacking `description` defaults to `''` (schema default). Add `seedAgents(repo, org, files)` upserting via `repo.putAgent` (idempotent, matching `seedSkills`). Wire into the disk + clone seed scripts and into `seedStarterForOrg` so new orgs get the agents at org scope. Reuse the scripts' existing `parseFrontmatter`, extended to also surface `tools`/`model`/`description`.
**Patterns to follow:** `buildSeedSkills`/`seedSkills` purity + idempotency (`seed/skills.ts`); `seedStarterForOrg` clone-then-disk fallback (`starter.ts:118-132`); `putAgent` semantics (`packages/backend/src/db/repo.ts`).
**Test scenarios:**
- Happy path: `buildSeedAgents` over the 6 files yields 6 org-scoped records; `model`/`tools`/`description` parsed correctly (e.g. `web-search-researcher` → `tools:[WebSearch,WebFetch,TodoWrite,Read,Grep,Glob,LS]`, `model:'sonnet'`, non-empty `description`); `prompt` equals the file body; `skills:[]`.
- Description seeding: a source file's frontmatter `description` is carried onto the record verbatim; a file lacking `description` yields `description:''` (schema default, no crash).
- Edge: a file missing `tools` frontmatter → `tools:[]` (not crash); missing `model` → record rejected by `agentSchema` (model is min-1) — assert the builder surfaces/skips rather than writing an invalid record.
- Idempotency: running `seedAgents` twice writes one record per agent (second run converges).
- Integration: `seedStarterForOrg` on a fresh org registers both the starter skills **and** the 6 ACE agents at org scope; `repo.listAgents(org)` returns them.

### U4. Bundle the skills and register them to a single org
**Goal:** The 27 skills form a `humanlayer-ace` bundle registered at org scope for **one** org (initially `test org`), not org-wide; the platform bundle is untouched.
**Requirements:** KTD5.
**Dependencies:** U8, U1, U3.
**Files:** `infra/scripts/seed-humanlayer-ace.mjs` (dedicated scoped registration). Deliberately **not** `.claude/skills/bundles.json`.
**Approach:** The script reads only the 27 imported skill dirs, builds an **in-memory** manifest (`{ 'humanlayer-ace': { members: [all 27] } }`), and runs it through the canonical `buildSeedSkills` so the 27 skills + the bundle record all land at org scope for the explicit `SEED_ORG`. It does **not** modify the shared `bundles.json`, which is what prevents `seed-all-orgs.mjs` / the `acme` template clone from propagating the set to other orgs. Run: `SEED_ORG="test org" node infra/scripts/seed-humanlayer-ace.mjs` (idempotent upsert; supports `SEED_DRY_RUN=1`). *(Already executed — 28 records confirmed in `org#test org`.)*
**Patterns to follow:** `seed-skills.mjs` (record build + `skillKey` put); the org-vs-grant scoping rule in `buildSeedSkills`.
**Test scenarios:**
- Happy path: after running, the `humanlayer-ace` bundle record + all 27 member skills exist at org scope for the target org (verified: 27 skills + bundle in `org#test org`).
- Containment: a *different* org's catalog is unchanged — the set did not propagate (it is absent from `bundles.json`, so `seed-all-orgs` won't pick it up).
- Edge: a manifest member with no matching SKILL.md is dropped from the bundle's stored `members` (existing `known.has` filter) — assert no dangling member.
- Regression: `command-hq-starter` members and scope are unchanged by the addition.

### U5. Approval directive in high-stakes skills + document the distribution seam
**Goal:** High-stakes skills request human approval via the already-registered approvals tool; the org-distribution seam is documented for the follow-up.
**Requirements:** KTD6.
**Dependencies:** U1.
**Files:** `.claude/skills/{commit,implement_plan,create_worktree,debug}/SKILL.md` (append directive), `docs/plans/2026-06-07-002-feat-humanlayer-ace-workflow-plan.md` (this doc's Deferred section already records the seam).
**Approach:** Append a short, shared "Human approval" directive to the four high-stakes skill bodies instructing the agent, before irreversible/outward-facing actions, to call `mcp__humanlayer-approvals__request_permission` when that tool is available and to proceed normally when it is not (graceful degradation — keeps the skill usable in non-`claude+` contexts). Do **not** hard-code a `--permission-prompt-tool` launch flag or edit the wrapper here; session-wide enforcement and org distribution are owned by the MCP-catalog follow-up (plan 001).
**Patterns to follow:** Tool naming `mcp__<server>__<tool>` from the registered server `humanlayer-approvals`; the soft/optional phrasing used by existing skills when a capability may be absent.
**Test scenarios:** Test expectation: none — instructional content. Behavior verified manually in U7 (a high-stakes action in a `claude+` session triggers a `request_permission` prompt; the same skill run where the tool is absent proceeds without error).

### U6. Remove the compound-engineering installer skill
**Goal:** The unrelated third-party installer is gone and the catalog no longer references it.
**Requirements:** KTD7.
**Dependencies:** none (independent; sequence anytime).
**Files:** delete `.claude/skills/compound-engineering/` (and its `SKILL.md`); edit `packages/backend/src/seed/skills.ts` and `packages/backend/src/seed/starter.ts` to remove `compound-engineering` from comments/examples of the user-granted built-ins list.
**Approach:** Delete the directory; scrub the references so the documented grant set (`gstack`, `playwright-cli`) no longer names it. No bundle lists it today, so no manifest edit is required.
**Patterns to follow:** The existing grant-owner comment block (`seed/skills.ts:74-78`).
**Test scenarios:**
- Regression: after a fresh seed, the catalog contains **no** record named `compound-engineering` at any scope; `gstack`/`playwright-cli` grants are unaffected.

### U7. End-to-end verification in a claude+ session
**Goal:** Prove the imported workflow registers, syncs, and runs — including a live approval prompt.
**Requirements:** Validates KTD1–KTD6 end to end.
**Dependencies:** U1–U6.
**Files:** none (verification only); optionally `docs/plans/...-002-...-plan.md` checkboxes.
**Approach:** Seed locally (`npm run build -w @harness/backend && SEED_ORG=<org> node infra/scripts/seed-skills.mjs`), opt a test project into the `humanlayer-ace` bundle and the 6 agents, run `claude+ sync-skills` (`/hq-update-skills`), and confirm `~/.claude+/skills/*` and `~/.claude+/agents/*` materialize. Smoke-test the workflow: `research_codebase` fans out to `codebase-locator`/`codebase-analyzer`; `create_plan` → `implement_plan`; and a high-stakes `commit` triggers the approvals prompt.
**Patterns to follow:** The sync/opt-in procedure documented in `.claude/skills/hq-add-skill/SKILL.md` (registration → per-project opt-in → `sync-skills`).
**Test scenarios:**
- Integration: opted-in project session lists all 27 skills + 6 agents; a research command actually dispatches the subagents; the approval prompt fires for a high-stakes action and is absent (no crash) when the MCP is unregistered.

### U8. Make agents first-class — description field + frontmatter render
**Goal:** An HQ agent becomes a *structured record* that renders to a valid Claude Code subagent file. Today the wrapper writes only the raw `prompt` to `~/.claude+/agents/<name>.md` with **no frontmatter**, so the stored `model` and `tools` are silently discarded and there is no `description` at all. After this unit an agent carries a `description`, and materializing it produces a real subagent file with `name`/`description`/`tools`/`model` frontmatter — mirroring how a skill already materializes as its full `SKILL.md` (KTD8).
**Requirements:** KTD8 — agents are structured records rendered to subagent frontmatter. This is a foundational data-model fix the agent-import path depends on.
**Dependencies:** none. **Sequences first** (before U2/U3/U4).
**Files:** `packages/shared/src/dto.ts` (add `description` to `agentSchema`), `packages/backend/src/rest/agents.ts` (passthrough + doc-comment), `wrapper/internal/config/remote.go` (`remoteAgent` fields + `renderAgentFile` + hash over the rendered file + push round-trip parse), `packages/web/src/screens/Agents/AgentEditor.tsx` (description field + tests).
**Approach:**
- **Schema:** add `description: z.string().default('')` to `agentSchema` (`dto.ts:182-204`). The `.default('')` is **required for back-compat** — existing agent records and tests have no `description`, and an org-only additive change must not invalidate them. This is additive and does not conflict with plan 001's `mcpServers[]` addition on the same schema.
- **REST:** `packages/backend/src/rest/agents.ts` passes `description` through create/update unchanged (it already passes `model`/`tools`); document that the field is the delegation trigger Claude Code uses to pick a subagent.
- **Wrapper render (the core change):** extend `remoteAgent` with `Description`, `Tools []string`, and `Model` (today it carries only `Name`/`Scope`/`Prompt`, so `model`/`tools` never reach disk). Add a `renderAgentFile(a remoteAgent) string` that emits the **MATERIALIZED AGENT FILE FORMAT**:

  ```
  ---
  name: <name>
  description: <description>
  tools: <tools joined with ", ">      ← OMIT this line entirely when tools is empty
  model: <model>                       ← OMIT this line entirely when model is "" (include "inherit")
  ---
  <prompt>
  ```

  with exactly one trailing newline after the prompt. In `Fetch`, set the agent's cached body to this rendered string and compute its `Hash` over that **same** rendered string (replacing the current `hashContent([]byte(a.Prompt))` over the bare prompt). This is the **hash-parity** requirement from the SHARED CONTRACT: the body served/materialized and the hash must be computed over one identical rendered string, so a freshly pulled agent reads back `in_sync` — exactly how skills already hash the full `SKILL.md` body (`remote.go:196-204`). `ApplyPulled` already writes `body` verbatim to `agents/<name>.md` (`claude.go:178-199`); no change there once the body is the rendered file.
- **Push round-trip:** the `KindAgent` push path must parse the rendered file back into `{name, description, tools, model, prompt}` (strip the frontmatter, split `tools` on `", "`) so pushing a locally-edited agent does not double-wrap frontmatter or smuggle the `---` block into `prompt`. This mirrors the skill push, which round-trips the full `SKILL.md`.
**Patterns to follow:** the skill body+hash parity in `Fetch` (`remote.go:196-204`); `ApplyPulled`'s verbatim write and the "writing exactly `body` keeps the local hash equal to the HQ hash" invariant (`claude.go:163-199`); the subagent frontmatter shape Claude Code reads (`claude.go:132-149`); the agent record shape in `packages/backend/test/agents.test.ts`.
**Test scenarios:**
- Schema back-compat: an agent JSON lacking `description` parses with `description === ''`; a provided `description` is preserved verbatim through `agentSchema.parse`.
- Wrapper render — full: an agent with `tools` and a non-`inherit` `model` renders all four frontmatter lines + body + exactly one trailing newline.
- Wrapper render — omissions: empty `tools` omits the `tools:` line entirely; `model:""` omits the `model:` line; `model:"inherit"` **includes** the `model: inherit` line.
- Hash parity: `Fetch` → `ApplyPulled` (materialize) → `ReadLocal` re-hash equals the served `Hash` (`Diff` reports `in_sync`).
- Push round-trip stability: render → push-parse → render again yields a byte-identical file (no double frontmatter, `prompt` free of the `---` block).
- Web editor: the AgentEditor exposes a `description` field whose value is included in the saved payload.

---

## Scope Boundaries

### In scope
- Importing all 27 HumanLayer commands as skills and all 6 subagents as agents.
- Extending the seeder to register agents (new mechanism).
- A `humanlayer-ace` bundle registered to a single org (`test org`), not org-wide; removal of the `compound-engineering` installer.
- Referencing the already-local approvals tool from high-stakes skills.

### Deferred to Follow-Up Work
- **Org-wide approvals distribution.** Register `humanlayer-approvals` as a catalog MCP-server record and attach it to ACE projects/agents, consuming `docs/plans/2026-06-07-001-feat-mcp-servers-tab-plan.md` (its `.mcp.json` merge + agent union-on-add). Until then approvals work via the existing local `~/.claude+` registration. Includes the optional `--permission-prompt-tool` launch-flag enforcement at the wrapper.
- **Prune thoughts/Linear-coupled duplicates.** The `_nt`/`_generic` command variants and the `thoughts-*` agents exist because of HumanLayer's `thoughts/` and Linear coupling, which this plan does not integrate (see Risks R1). Once the workflow is live, consider collapsing the redundant planning/research variants and dropping the thoughts agents — a catalog-curation pass, not part of the initial "import everything" landing.
- **Per-skill model pinning.** If the lost `opus` pins on planning/research skills matter, add a model field to skills or split those into agents — out of scope here (KTD3).

### Explicit Non-Goals
- The HumanLayer `thoughts` system (HQ has its own docs/PRD knowledge layer).
- `hlyr launch` / the `hld` daemon as a session launcher (Command HQ owns session spawning; adopting it would conflict).
- Any change to how existing skills/agents or the `command-hq-starter` bundle behave.

---

## Risks & Mitigations

### R1 — Thoughts/Linear coupling means some imported commands reference infrastructure that won't exist
Many commands assume a `thoughts/` directory (`implement_plan` reads `thoughts/shared/plans/`) or Linear (`linear`, `ralph_*`, `founder_mode`, `oneshot`); `thoughts-locator`/`thoughts-analyzer` have nothing to search without thoughts. The `_nt` ("no thoughts") and `_generic` variants are HumanLayer's own thoughts-free versions. **Mitigation:** import everything per the directive, but document (Deferred) that the `_nt`/`generic` variants are the functional ones in HQ and the thoughts/Linear-coupled ones degrade to no-ops or missing-path errors until/unless that infra is added. Flag this prominently at review so the catalog-curation follow-up is a conscious choice, not a surprise.

### R2 — Skills lose per-command model pins
Planning/research commands pinned `model: opus`; as skills they inherit the session model (KTD3). **Mitigation:** accept for v1; the workflow is unchanged in structure. The fidelity loss is confined to the skill layer: the agents (the parallel fan-out) now both **keep AND render** their `model` (`sonnet`) and declared `tools` into the materialized subagent frontmatter (KTD8 / U8), where previously those fields were stored-but-discarded — so the research subagents run model-pinned and tool-scoped as intended. Per-skill model pinning remains a documented follow-up.

### R3 — Snake_case skill names deviate from the kebab convention
Tooling that assumes kebab names could mishandle them. **Mitigation:** the seed and wrapper key skills by directory/frontmatter `name` with no case constraint (`parseFrontmatter` accepts `[A-Za-z0-9_-]+`); U7 verifies materialization. The deviation is contained to names and justified by KTD2 (intact cross-references).

### R4 — Seeder agent extension could mis-handle frontmatter
The new `buildSeedAgents` parses richer frontmatter (`tools` list, `model`) than the skills path. **Mitigation:** U3's unit tests cover the tools-list parse, missing-`tools`, and missing-`model` (schema-reject) cases before any deploy; the builder is pure and idempotent like its skills sibling.

---

## Dependencies / Sequencing

1. **U8 sequences first.** It fixes the agent data-model + render contract (schema `description`, frontmatter materialization, hash parity) that the agent-import and agent-seed units build against. No dependencies; land it before U2/U3/U4.
2. **U1, U6** are independent and can land in parallel with U8 (content + deletion).
3. **U2** depends on U8 (vendors agent files against the corrected record/render contract).
4. **U3** depends on U8 + U2 (needs the agent files and the `description` field to seed); it is the only seed-heavy unit.
5. **U4** depends on U8 + U1 + U3 (bundle references skills; the scoped registration consumes the imported skill files).
6. **U5** depends on U1 (edits skill bodies).
7. **U7** depends on all; it is the acceptance gate.
8. **Approvals org-distribution** depends on `docs/plans/2026-06-07-001-feat-mcp-servers-tab-plan.md` shipping — tracked as Deferred, not on this plan's critical path.

---

## Sources & Research

- HumanLayer package assets: `C:\Users\mattd\AppData\Roaming\npm\node_modules\humanlayer\.claude\` (commands, agents, settings) — `humanlayer@0.17.2-npm`.
- Local approvals MCP registration: `~/.claude+/.claude.json` top-level `mcpServers.humanlayer-approvals` (stdio; `humanlayer mcp claude_approvals`).
- HQ catalog mechanics: `packages/shared/src/dto.ts:174-237` (agent/skill schemas), `packages/backend/src/rest/{skills,agents}.ts` (admin-gated CRUD, org-scope forcing), `packages/backend/src/seed/{skills,starter}.ts` (seeding), `packages/backend/src/db/repo.ts:605-696` (project opt-in, agent union-on-add).
- Wrapper materialization: `wrapper/internal/config/{overlay,remote,claude}.go` (skills/agents only; `ApplyPulled` writes bodies; no `commands/` dir).
- Registration procedure: `.claude/skills/hq-add-skill/SKILL.md`, `.claude/skills/hq-create-skill/SKILL.md`.
- Consuming seam: `docs/plans/2026-06-07-001-feat-mcp-servers-tab-plan.md` (MCP-server catalog; KTD6 `.mcp.json` merge, agent union-on-add).
