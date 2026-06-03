# Forge: capture a phase of work → distill a reusable agent

- **Date:** 2026-06-02
- **Status:** Ideation — converged on a minimal v1; ready to take into brainstorming/planning.
- **Topic:** A bundled claude+ command pair (`/startforge` … `/endforge`) that captures a
  slice of work and distills it into a small, reusable, org-wide agent — a short,
  token-efficient prompt plus a curated skill set — aimed at one kind of phase problem.

## The gist (one sentence)

> Mark a slice of work, then distill it into a small reusable agent — a short
> prompt plus a curated set of skills aimed at one kind of phase problem — and
> register it in Command HQ for everyone.

## End goal

**Short, token-efficient prompts with a reasonably curated skill set, each good at a
specific phase problem.** Forged agents are phase specialists: narrow scope, small
prompt, a handful of skills.

## Lifecycle

1. **`/startforge [what you're working on]`** — marks the start point: records the
   base commit, and optionally creates a worktree + branch. If the user didn't say
   what they're working on, ask, and generate (or accept) a name. This is the only
   ceremony.
2. **…do the work…** — the per-repo daemon already captures the session (events,
   transcript, and the skills/tools actually used).
3. **`/endforge`** — distills the captured work into a registered agent (details
   below).

## What `/endforge` does

a. **Distill a seed prompt + curate the skill set.**
   - *Prompt:* a first draft that reproduces the captured work. It **is allowed to
     contain project-specific instructions** at this stage.
   - *Skills:* the distiller has **full access to the entire skill catalog**, but
     anchors on the **skills actually used** in the captured session, rounding the
     set out from the catalog only where something obviously fits. **Skills are
     curated, not searched/optimized.**

b. **Gradient-descent the prompt (prompt only).**
   - Run the seed prompt through a **third-party prompt-gradient-descent tool** to
     make it more **token-efficient and higher-performing**. (v1 target: **TextGrad**,
     behind a thin interface so DSPy/AdalFlow can swap in later.)
   - The optimizer needs an objective + an eval case to descend on. **The captured
     diff/session is that eval target** — the captured work both seeds the prompt and
     grades the optimizer's candidates.
   - The optimizer touches **only the prompt**. Skills are decided once by curation
     and handed to registration untouched.

c. **Split pass (after optimization).**
   - Decompose the optimized prompt into two pieces:
     1. **Reusable prompt** — the generalizable, project-agnostic agent/skill
        instructions. *This is what gets registered and reused on other projects.*
     2. **Project-specific instructions** — what this particular unit of work was
        doing. **Mostly documentation**; rides along as the agent's forge-provenance
        record, **not** shipped into other projects at runtime.
   - Why split *after* optimizing: the optimizer needs the specifics in the prompt to
     hit the eval target; generalization is a single decomposition step at the end
     rather than a constraint that hobbles the descent.

d. **Register org-wide.**
   - Register the agent = `{ reusable prompt, model, skill refs }` to Command HQ at
     **org scope, for all users.**
   - **Co-register any new skills** the agent points to (no dangling skill pointers).
   - Write the **agent ↔ skill association both ways**: the agent lists the skills it
     uses; a skill records the agent it's likely to be used in.
   - Keep the project-specific instructions as the agent's forge-provenance doc.

## How this builds on what already exists

This is largely the **capture/authoring side** of machinery already in the repo:

- **Forge backend** (`packages/backend/src/forge/`): `embed.ts` (session →
  summary → embedding, with `SessionActivity.skills`/`tools`), `propose.ts`
  (`aggregateFrequencies`/`splitConfidence` over sessions → drafted agent), `search.ts`.
  The distiller reuses `propose.ts`'s drafter for the seed prompt and the frequency
  aggregation as the skill-curation spine. The **trigger** changes (precise
  start/end checkpoint instead of a fuzzy text query); the pipeline mostly stays.
- **Config sync / Agents+Skills registry** (`wrapper/internal/config/`): `Item`
  (`Kind = agent | skill`, content-hashed), `Diff` (push/pull/differs drift),
  scoped `org | user#uid | proj#pid`. Registration rides this: publish skills first,
  then the agent; distribution to all users is the existing `needs_pull` drift +
  reconcile.
- **Capture** (`wrapper/internal/capture/`): already records sessions, transcripts,
  and skills used — so "the skills it uses" is observable for free.

### Kill the Forge tab

The current `tab_forge.go` is the **read-side** authoring UI (fuzzy description →
semantic-search past sessions → propose). The command flow supersedes it with
precise evidence, so the tab becomes a weaker duplicate and should be removed.
**Keep** the **Agents/Skills tab** (config drift/reconcile) as the browse/manage
surface where org-published agents appear and pull down. **Repurpose**, don't delete,
the backend `forge/` code.

## Decisions settled during ideation

- **Interface = commands, not a GUI.** No dedicated Forge panel; the "UI" is the
  `/startforge` → `/endforge` lifecycle plus the `/endforge` approval prompt.
- **Prompt: yes gradient descent** (third-party tool, prompt only).
- **Skills: no gradient descent.** Full-catalog access + curation anchored on actual
  usage.
- **One optimizer, specifics allowed in, split afterward** into reusable prompt +
  project-specific doc.
- **Register at org scope, for all users**, co-registering new skills, with
  bidirectional agent↔skill links.

## Deferred (recoverable later, not in v1)

- **Self-built eval/optimization harness** (sandboxed re-execution + scoring) — we
  buy this via the third-party tool instead of building it.
- **Continual propagation / trainset loop** — trying out a registered agent would
  silently open a `/startforge`, and a dissatisfied run would re-optimize over the
  new trial ∪ previous sessions (a per-agent "trainset", with a `parentAgent` +
  `satisfaction` link on session records, regression-guarded against the originals,
  emitting a new pinned version). Deferred entirely; for now, just `/startforge` →
  `/endforge` again.
- **Prompt × skill co-optimization** (DSPy-style joint search) — skills stay curated.
- **"Is this a clear feature type?" classifier gate** — folded into the distiller: if
  there's nothing coherent to save, it says so.
- **Version pinning + publish governance/review** — matters once many agents exist
  and org-wide publish-by-anyone becomes a supply-chain surface.

## Risks / things to watch

- **Split-pass fidelity** (axis C) — leave specifics in the reusable prompt and the
  agent overfits to one repo; strip too aggressively and it loses useful patterns.
  This single pass is where the project-agnostic guarantee is actually enforced.
- **`/startforge` push-to-main / worktree ceremony** is destructive and surprising —
  needs guardrails and a sane default for dirty trees / abandoned forges.
- **Org-wide publish-by-anyone** — a supply-chain surface for 175+ users; likely
  wants a review step before deferral above is reversed.
- **Optimizer cost/latency** — gradient-descent tools make many model calls; the
  `/endforge` optimize step is heavy but explicit and infrequent.
- **Python optimizer ↔ Go/TS stack** — the optimizer is a server-side step in the
  forge backend, not in the Go wrapper.

## Open questions for planning

- Exact `/startforge` git semantics: always worktree+branch, or optional? push-to-main
  guardrails; dirty-tree handling; abandoned-forge cleanup.
- Where the optimizer runs (forge backend service vs. workflow step) and how the
  captured work is packaged as its eval target.
- Skill provenance for **new** skills the distiller mints — how they're named,
  scoped, and reviewed before org publish.
- Agent naming / dedup against existing registered agents.
