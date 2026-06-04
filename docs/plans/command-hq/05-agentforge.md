---
status: active
type: feature
created: 2026-06-02
completion: 93
feature: agentforge
ground-truth: this doc (folds in the former docs/brainstorms/2026-06-02-forge-commands-requirements.md)
origin: ../../ideation/2026-06-02-forge-agent-distillation.md
---

# Feature 5 — AgentForge (capture a unit of work → distill a reusable agent)

Two bundled claude+ commands bracket a slice of work and distill it into a
reusable agent — a short, focused prompt plus the minimal skills it needs —
registered to Command HQ. `/hq-startforge` opens the boundary (lands current work to
main, then branches a worktree); `/hq-endforge` closes it, distills the captured diff
+ session into an agent, and registers it into the **org catalog** via the
admin-gated agents/skills REST.

> **Model change (2026-06-03):** with the [org-catalog scope
> collapse](../../superpowers/specs/2026-06-03-org-catalog-scope-collapse-design.md),
> skills and agents are a single **org-scoped catalog** (see
> [feature 3](./03-claude-code-integration.md)). The older AgentForge design —
> register at the *author's own scope*, then a person *promotes* it org-wide via a
> scope-elevate control — is **retired**: the agents/skills REST now **forces org
> scope** and **admin-gates** every write (`rest/agents.ts` `createAgent`,
> `rest/skills.ts`), so a forged agent lands directly in the one org catalog and an
> admin enables it per project. There is no separate author-scope record and no
> scope-elevate promote step.

## Problem frame

Good work done in one claude+ session is trapped there — the patterns, ordering,
and skill choices that made a phase go well aren't reusable, so the next person
solving a similar phase problem starts from scratch. claude+ already captures
sessions and has an org-wide agent/skill registry (`wrapper/internal/config`), plus
a read-side Forge concept that mines fuzzy history (`packages/backend/src/forge`).
What's missing is a precise way to say "*this* slice is worth keeping," capture it
exactly, and turn it into a phase-specialist agent others can use. Today the cost
is silent: hard-won approaches are re-derived across 175+ users instead of
compounding.

## Key decisions

- **Distiller-first v1.** v1 ships the **capture → distill → register** path
  end-to-end. The Claude Code optimize/refine loop (running candidate prompts in a
  worktree and scoring them) is **deferred to the next increment** so the capture +
  registration plumbing ships and proves out first.
- **`/hq-startforge` lands current work to main, then branches.** It commits + pushes
  the current branch's work to main for a clean baseline, then creates a fresh
  worktree + branch off main. If the work isn't cleanly landable, it **aborts with a
  warning** rather than forcing a messy land.
- **The quality gate is admin opt-in per project, not the CLI.** *(Revised
  2026-06-03 — see the Model-change note.)* `/hq-endforge` registers the agent into
  the admin-gated **org catalog**; it is then enabled per project from a project's
  Agents tab (`screens/ProjectDetail/ProjectAgents.tsx`). The original author-scope +
  org-wide *promote* gate is retired with the scope collapse; the deliberate human
  step is now the per-project **enable**, not a scope elevation.
- **`/hq-endforge` classifies but never silently blocks.** It judges whether the diff
  is a coherent feature and warns (with reasons) when it looks incoherent, but the
  author can override and forge anyway.
- **Mint only the skills this agent needs.** Reference existing skills where they
  match what was actually used; create the minimal new skills the agent requires and
  register those alongside it. No broad skill synthesis.
- **Optimize the combined prompt, split afterward** *(applies once the optimizer
  lands)*. The distilled prompt may contain project-specifics; a post-step splits it
  into a reusable, project-agnostic agent prompt and a project-specific provenance
  doc.

## Actors

- **Forging user** — runs `/hq-startforge` and `/hq-endforge`; reviews and confirms the
  forged agent before registration.
- **claude+ daemon / capture** — records the session (transcript, skills used) and
  the diff between the start marker and `/hq-endforge`; the evidence the distiller uses.
- **Command HQ** — the org catalog the agent + skills register into, and the web UI
  where agents are viewed and enabled per project.
- **Admin** — the single admin who enables a forged agent on a project (the per-project
  opt-in that replaces the retired org-wide *promote*).
- **Consuming users** — org members who receive an enabled agent via the existing
  config-sync drift/pull once it is turned on for their project.

## Flows

- **F1 — Start a forge.** `/hq-startforge [what you're working on]` → land current work
  to main (commit + push); if not cleanly landable, **abort with a warning and
  stop**. Otherwise resolve a name (provided, or generated + user-confirmed), create
  a worktree + branch off main, and record the start marker (base commit).
- **F2 — Do the work.** The user works in the forge branch; claude+ captures the
  session as it already does. No new user action.
- **F3 — End a forge.** `/hq-endforge` → ask whether to generate a reusable agent (no →
  forge closes, no agent). If yes: classify coherence + warn if not (override
  allowed); distill a short focused prompt from the diff + session; curate the
  minimal skill set (reference existing, create needed-but-missing); show the drafted
  agent; on confirm, register the agent + any new skills into the org catalog (the
  REST forces org scope, admin-gated).
- **F4 — Enable per project.** *(Revised 2026-06-03.)* An admin opens the forged
  agent in Command HQ and **enables it on a project** from that project's Agents tab
  (which unions its skills into the project's `enabledSkills`). Config-sync drift then
  makes it pullable by users on that project. The old org-wide scope-elevate *promote*
  is retired.

## Requirements

**Capture & boundary**
- **R1** `/hq-startforge` accepts an optional description; when absent, it asks what the user is working on.
- **R2** `/hq-startforge` names the forge: user-provided if given, else a generated name the user can accept or change.
- **R3** `/hq-startforge` lands the current branch's work to main (commit + push) before creating the forge branch.
- **R4** If the work can't be landed cleanly (conflicts / unrelated WIP), `/hq-startforge` aborts with an explanatory warning and makes no changes.
- **R5** `/hq-startforge` creates a fresh worktree + branch off main and records the start marker for the end diff.
- **R6** The evidence for distillation is the branch diff (start marker → `/hq-endforge`) plus the session claude+ already records; no new capture mechanism.

**Distillation & split**
- **R7** `/hq-endforge` first asks whether to generate a reusable agent; "no" closes the forge without producing one.
- **R8** When generating, `/hq-endforge` classifies whether the diff is a coherent feature and warns with reasons when it is not, but allows override.
- **R9** `/hq-endforge` distills a short, focused prompt aimed at the phase problem; conciseness is for clarity, not a token-spend target.
- **R10** The prompt encodes good working principles — foundation first, correct ordering, avoid known dead-ends — drawn partly from standing guidance and partly from the order actually followed in the captured session.
- **R11** v1 registers the distilled prompt **as-is** (it may contain project-specifics), labeled "not yet generalized" in the registry. The project-agnostic split is **deferred** (see Deferred).

**Skills**
- **R12** The agent's skill set is curated, not searched: anchored on the skills actually used, kept minimal.
- **R13** Where a needed capability matches an existing skill, the agent references it rather than duplicating it.
- **R14** Where the agent needs a capability with no existing skill, `/hq-endforge` creates only that skill and registers it alongside the agent.
- **R15** The registered agent references its skills as pointers; agent and skills are distinct registry records.

**Registration & HQ opt-in** *(R17/R19/R20 revised 2026-06-03 for the org-catalog scope collapse — see the Model-change note)*
- **R16** `/hq-endforge` shows the drafted agent (prompt + curated skills) and registers only on the user's confirmation.
- **R17** `/hq-endforge` registers the agent (and any new skills) into the **org catalog** via the agents/skills REST (server forces org scope, admin-gated). *(Originally: author's own scope.)*
- **R18** Registration completes such that the agent's skill pointers resolve (skills present before the agent is consumable).
- **R19** Command HQ provides a control to view a catalog agent and **enable it per project** from the project's Agents tab. *(Originally: a scope-elevate "promote to org" control — retired.)*
- **R20** An enabled agent becomes available to users on that project through the existing config-sync drift/pull. *(Originally: a promoted agent reaching all org users.)*

## Acceptance examples

- **AE1 (R3, R4)** — uncommitted, conflicting WIP → `/hq-startforge` aborts with a warning and creates no branch/worktree.
- **AE2 (R7)** — answering "no" at `/hq-endforge` registers no agent or skill.
- **AE3 (R8)** — a diff of several unrelated changes → `/hq-endforge` warns it looks incoherent (with reasons) and still proceeds on override.
- **AE4 (R13, R14)** — a session using one existing skill + one capability with no matching skill → the agent references the existing skill and one new skill is created and registered.
- **AE5 (R17, R19, R20)** — a freshly forged agent lands in the org catalog but is not yet active on any project until an admin enables it from a project's Agents tab, after which users on that project pull it via config-sync.

## Builds on existing code

- **Distillation is client-side, no server pipeline.** As built, the fuzzy read-side
  `packages/backend/src/forge/` backend (`embed.ts`/`propose.ts`/`search.ts`) and
  the OpenSearch `SearchStack` were **removed** — distillation runs inside the
  claude+ PTY on the user's own Claude subscription (`.claude/skills/hq-endforge/`),
  not against a backend embeddings/search service. Registration reuses the existing
  org-scoped agents/skills REST (`rest/agents.ts`, `rest/skills.ts`) only.
- **Registry + sync** `wrapper/internal/config/` — content-hashed items, push/pull
  drift — carries registration and the per-project enable-and-pull distribution. (The
  catalog is org-scoped now; the wrapper materializes only the linked project's
  enabled items into `~/.claude+`.)
- **Capture** `wrapper/internal/capture/` — already records sessions + skills used,
  so the evidence is observable for free.

## Deferred (later increments)

- **The optimize/refine loop** — run candidate prompts in a worktree, score for
  outcome + process quality, iterate (~3–5 times on the user's Claude subscription),
  plus the **split pass** (reusable prompt vs. project-specific provenance doc).
  *Partially landed:* an `/hq-optimize-agent` skill (`.claude/skills/hq-optimize-agent/`)
  now runs a client-side refine → self-judge → keep-best loop against an existing
  HQ agent and writes the improved prompt back via the agents REST. The
  capture-time split pass and an automated transfer-validation gate remain deferred.
  Rationale + 2026 research in the
  [ideation doc](../../ideation/2026-06-02-forge-agent-distillation.md).
- The "try it out" propagation / trainset loop; agent/skill versioning, hash-pinning,
  CI-on-skill-change, `AGENTS.md` export.
- The read-side Forge tab and its backend are **removed** (no `wrapper/internal/tui/`
  package, no `backend/src/forge/`), so only `/hq-startforge`…`/hq-endforge` is
  user-visible — already done, not a fast-follow.

**Outside this v1's identity:** token-spend / prompt-length optimization; search-based
skill selection or broad skill synthesis beyond what the forged agent needs.

## Open questions

- **Resolved (2026-06-03):** v1 runs with a **single admin user** who enables forged
  agents per project (the opt-in that replaced the retired org-wide promote);
  permissions/roles and a transfer-validation gate are out of scope for now.
- **During planning:** exact start-marker shape + how `/hq-endforge` computes the diff;
  where the provenance doc is stored and how it links to the agent record; name/dedup
  when a forged agent collides with an existing one; abandoned-forge cleanup
  (worktree/branch left when `/hq-endforge` is never run).
- **[Deferred — 2026-06-03 review]** Redact secrets (key/token patterns) from the
  diff + transcript before Claude Code distills them, so secrets can't land in a registered prompt.
- **Interaction states to define (design phase):** `/hq-startforge` abort output (what
  the warning lists; partial-land handling) and the per-project enable control (what the
  admin sees before confirming).

## Status

- **Built:** `/hq-startforge` (land-to-main + worktree/branch + start marker) and
  `/hq-endforge` (classify → distill → curate skills → register into the org catalog)
  under `.claude/skills/`; the single-admin **per-project enable** path on a project's
  Agents tab (`screens/ProjectDetail/ProjectAgents.tsx`); capture; the config-sync
  registry + per-project drift/pull. An `/hq-optimize-agent` skill ships a client-side
  prompt-refinement loop. The read-side fuzzy Forge backend + OpenSearch stack are
  removed.
- **Deferred:** the capture-time optimize/refine + reusable/provenance split pass
  inside `/hq-endforge`; an automated transfer-validation gate; secret redaction of the
  diff/transcript before distillation.
