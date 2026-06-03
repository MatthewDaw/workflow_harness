---
name: endforge
description: >-
  Close an AgentForge capture boundary inside the claude+ PTY. It asks whether
  to generate a reusable agent; if yes it classifies whether the captured diff
  is a coherent feature (warns with reasons, never silently blocks — override
  allowed), distills a short focused prompt from the diff + session, curates the
  minimal skill set (references existing skills, mints only missing ones), shows
  the draft, and on confirmation registers the agent + new skills to Command HQ
  at the AUTHOR's own scope. A single admin later promotes it org-wide in HQ.
  Pair with /startforge. Use when the user says "/endforge", "end the forge",
  "distill this into an agent", or "register the forged agent".
---

# /endforge

The second half of the AgentForge v1 command pair (distiller-first). It turns
the captured slice into a reusable agent — a short, focused prompt plus the
minimal skills it needs — registered at the author's scope. Going org-wide is a
deliberate human action in Command HQ (the promote step), not something this
command does.

## Reuse — do not re-implement

Reuse the existing backend distillation machinery rather than re-implementing it:

- **`BedrockAgentDrafter`** (`packages/backend/src/forge/propose.ts`) — the
  Bedrock-backed drafter; its `draft({ description, skills, tools, sessions })`
  returns `{ name, model, prompt }`. The forge trigger changes from a fuzzy
  similarity query to this precise start/end checkpoint, but the drafter +
  proposal shape stay.
- **The proposal shape** `AgentProposal` (`packages/shared/src/dto.ts`):
  `{ name, model, prompt, skills[], tools[], lowConfidence[], evidence[],
  insufficientHistory }`. `/endforge` produces a proposal in this shape from the
  captured slice (the evidence here is the branch diff + this session, not
  k-NN-mined history).
- **Aggregation helpers** `aggregateFrequencies` / `splitConfidence` (same file)
  — to rank the skills/tools actually used in the captured session and flag
  low-confidence ones.

v1 is **distiller-first**: the optimize/refine loop and the reusable/provenance
split are **DEFERRED** (05-agentforge.md "Deferred"). The fuzzy read-side Forge
surface is hidden in v1 (no `rest/forge.ts` route; the read-side tab is
hidden) — only `/startforge`…`/endforge` is user-visible.

## Steps (F3 in 05-agentforge.md; R7–R18)

1. **Read the start marker.** Load `.claude/forge/<name>.json` written by
   `/startforge`; the slice is `baseCommit..HEAD` plus the session claude+
   captured (transcript + skills used) (R6).
2. **Ask whether to forge (R7).** "Generate a reusable agent from this slice?"
   If **no**, close the forge cleanly and register nothing — done (AE2).
3. **Classify coherence (R8).** Judge whether the diff is one coherent feature.
   If it looks like several unrelated changes, **warn with reasons** (list the
   incoherent groupings) but allow the user to override and proceed (AE3). Never
   silently block.
4. **Distill the prompt (R9–R11).** Compute the diff and summarize the session,
   then call the drafter (`BedrockAgentDrafter.draft`) to produce a short,
   focused phase-specialist prompt. It encodes good working principles —
   foundation first, correct ordering, avoid known dead-ends — drawn from
   standing guidance + the order actually followed in the session. v1 registers
   the prompt **as-is** (may contain project-specifics), labeled "not yet
   generalized." The project-agnostic split is deferred.
5. **Curate skills (R12–R15).** From the skills actually used in the captured
   session (ranked via `aggregateFrequencies`), keep a minimal set. For each:
   - if it matches an **existing** registered skill, **reference** it by name
     (don't duplicate) (R13);
   - if the agent needs a capability with **no** existing skill, mint **only**
     that one new skill and plan to register it alongside the agent (R14).
   The agent stores its skills as **name pointers** (R15) — agent and skills are
   distinct registry records.
6. **Show the draft (R16).** Present the drafted agent: name, model, prompt,
   referenced skills, any new skills to be minted, and the coherence verdict.
   Register **only on the user's confirmation**.
7. **Register at author scope (R17, R18).** Register new skills *first* so the
   agent's pointers resolve when consumed (R18), then the agent — all at the
   **author's user scope** (`scope: { tier: "user", id: <authorUserId> }`). Use
   the HQ registry REST:
   - Skills: `POST /skills` with body `{ name, scope, kind: "skill",
     description, source: "custom", members: [] }` (the `skillSchema` shape).
   - Agent: `POST /agents` with body `{ name, scope, model, prompt, skills:
     [<skill names>], tools: [...] }` (the `agentSchema` shape).
   Both at `tier: "user"` — `canWriteScope` lets a user write their own user
   scope without admin. Do **not** post at org scope.

The forge boundary is now closed; the agent exists at the author's scope only.

## Promote org-wide (F4 — a separate human step, R19/R20)

`/endforge` never publishes org-wide. A **single admin** (v1 = one admin user =
the promoter) opens the forged agent in Command HQ, reviews prompt + skills, and
flips it to org scope via the existing scope-elevate path:

- `POST /agents/:name/scope` with body `{ scope: { tier: "org", id: <org> } }`
  (`scopeChangeSchema`). The handler rewrites the scope key (delete old, put
  new). `canWriteScope` gates the org tier behind the `custom:admin` claim
  (`scopeauth.ts`), so only the admin can promote.

Once promoted, the agent reaches all org users through the existing config-sync
drift/pull. The skills it references should be promoted the same way so the
pointers resolve org-wide (`POST /skills/:name/scope`).

## Worked dry-run example (against THIS repo)

Following the `/startforge` example (`skill-authoring-flow`):

```
/endforge
```

1. Reads `.claude/forge/skill-authoring-flow.json`; diff = the four new
   SKILL.md files; session used a doc-writing flow.
2. "Generate a reusable agent?" → yes.
3. Coherence: all four files are one coherent "author client skills" feature →
   no warning (counter-example: had the diff also touched unrelated infra, it
   would warn "looks like 2 features: skill authoring + infra; forge anyway?").
4. Drafter produces agent `skill-author`, model
   `anthropic.claude-3-5-sonnet-20240620-v1:0`, a focused prompt for "author a
   well-formed Claude Code SKILL.md from a slice of work."
5. Curate skills: references the existing doc-writing skill if one is
   registered; mints one new skill `skillmd-frontmatter` (no existing match).
6. Shows the draft; user confirms.
7. `POST /skills` (skillmd-frontmatter, scope user) then `POST /agents`
   (skill-author, scope user, skills:["skillmd-frontmatter", ...]).
8. Prints: "Registered 'skill-author' at your scope. An admin can promote it
   org-wide in HQ → Agents → Promote."

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. Verified by running it: "no" at the
prompt registers nothing; "yes" distills a prompt, curates skills, and registers
the agent + new skills at the author's user scope; the org-promote control
(scope-elevate to org) is admin-gated. The fuzzy-Forge pipeline stays unrouted.
