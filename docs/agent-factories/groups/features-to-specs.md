# Features → Specs

**Type:** Pipeline — step 4

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Convert features into **ready-to-code specs / Linear tickets**.

## Input

- The feature list (+ wireframe) from
  [Goal → feature list](./high-level-goal-to-feature-list.md).

## Output

- Specs / Linear tickets that are ready for a coding agent to pick up.

## Sub-agents (to flesh out)

- **Spec writer** — feature → detailed, implementable spec.
- **Ticket formatter** — spec → Linear ticket (fields, acceptance criteria).
- **Dependency/sequencing agent** — orders tickets, flags blockers.

## Decisions

### Definition of Ready — what "ready to code" means
A coding agent can't ask clarifying questions mid-task, so the bar is stricter than a
human ticket. Every ticket must carry (Addy Osmani / Factory.ai agent-readiness):

- **Goal** — one sentence: "Implement X so a user can Y."
- **Acceptance criteria** — Gherkin **Given/When/Then**, one binary condition per line
  (doubles as test scaffolding).
- **Affected files / entry points** — explicit paths, resolved via
  [code-research](./code-research.md) (placement advisor). The agent must not guess.
- **Interface contracts** — function signatures / API shapes / types it must conform to.
- **Test requirements** — framework, run command (e.g. `npm test`), TDD vs. agent-written.
- **Style exemplar** — one real snippet from the codebase ("one real snippet beats three
  paragraphs of description").
- **Boundaries** — three-tier Always-do / Ask-first / Never-touch file lists.
- **Dependencies & out-of-scope** — `depends_on` (feeds the
  [specs-to-code](./specs-to-code.md) scheduler) + an explicit not-in-this-ticket list.

### Granularity — vertical slices
**One vertical slice per ticket** (API + logic + DB + UI for one user-visible behavior),
**not** horizontal layers (a "just the migration" ticket isn't self-verifiable). Target
**1–3 files / 100–300 LOC / 3–5 steps** — roughly a 1-hour skilled-human task, one-shot-able
in a single agent context. If a feature exceeds that, split along behavior boundaries; if
several tickets share a contract, emit a **contract-stub ticket first** (types/interfaces,
no logic) so the rest have a stable target. Gating constraint: if it can't be tested
end-to-end in isolation, split it further.

> Sources: INVEST + Definition of Ready; Gherkin acceptance criteria; Addy Osmani "spec
> for AI agents"; Factory.ai agent readiness; vertical-slicing guidance (EclipseSource/CodeMag).
