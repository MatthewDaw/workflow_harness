# Design Brief: Conversation Topic Capture & Learning Extraction

**Date:** 2026-06-06
**Status:** Design settled — technical planning next
**Scope:** claude+ wrapper → daemon → Command HQ backend → web

> This document captures the **goals and settled design** for making claude+ capture
> what each session is working on, segment a conversation into topics as focus drifts,
> and mine manual corrections into structured learnings that improve two agents. It is the
> foundation for the technical implementation plan (to be written next). It does **not**
> yet contain implementation units, file-by-file changes, or test scenarios.

---

## 1. The Goal

Users practice **loose document-driven development**: they write a spec, one-shot a Claude
implementation of most of it, then correct the result through manual prompts. Reality is messier
than the ideal — as users go through corrections they have new ideas, ask for features that
aren't in the docs, and sometimes **contradict** the docs outright.

We want to **capture what the user is actually focusing on** throughout that process and mine the
manual corrections so that, over time:

- the **implementing agent** accumulates enough context and conventions to one-shot a vague
  prompt like _"build a login page"_ **to completion** — including details that were never written
  in any doc — so there is **less correction to do**; and
- the **doc-writing agent** learns to **ask better questions up front**, so users think decisions
  through more fully before implementation.

The end state: the user says something vague, the implementing agent has the context to build it
right _and_ complete, and the doc-writing agent has already surfaced the decisions that used to
cause contradictions.

---

## 2. Why This Shape (grounding)

These facts, verified against the repo and the Claude API, drove the design:

- **The Claude API is stateless. Anthropic stores nothing you can query back.** There is no
  endpoint to pull a conversation transcript or list past conversations. The **only** durable copy
  of a Claude Code conversation is the local JSONL on the dev's machine
  (`~/.claude/projects/<hash>/<sessionId>.jsonl`), which the wrapper **already tails** via
  `wrapper/internal/capture/jsonl.go`. Client-side capture is therefore the _only_ way Command HQ
  can have this data — it is load-bearing, not optional.
- **Command HQ stores events, not transcripts.** The backend persists the granular event stream
  (`user.msg` / `assistant.msg` / `tool.call` / `tool.result`) and a derived projection — not the
  raw transcript. That is the right default and we keep it.
- **There is no Anthropic SDK anywhere in the backend**, and no full-session summarization today.
  The existing `summary` field on a session is literally the first user prompt, capped at 200 chars,
  and **never updates** (`wrapper/internal/daemon/daemon.go:259,286`). That frozen field is the
  "half-built" work this design completes.
- **Claude Code already titles tabs and auto-compacts**, writing both into the session JSONL the
  wrapper already reads — signal we can harvest rather than recompute.
- **Universal lesson from products + research:** titles are set-once/sticky, summaries roll;
  conflating a stable slug with an evolving topic causes "title thrash." Keep them as distinct fields.

**Consequence:** summarization/segmentation runs **wrapper-side, on the dev's own Claude
subscription**, over the local JSONL — and only small, distilled records ship to Command HQ. This is
cheaper, more private (raw transcripts with secrets/PII stay on the dev's machine), and needs no
backend API key.

---

## 3. What We're Building (v1)

A v1 that:

1. **Segments a single session into topics** and **relabels the current topic as focus drifts.**
2. **Mines manual-correction turns** into structured learnings.
3. **Persists** topic, a rolling description, and the routed learnings into Command HQ.
4. Surfaces topics **within the session** in the web UI.

All of it works with **zero documentation** — docs are optional everywhere.

---

## 4. Settled Design Decisions

### 4.1 Where it runs

- **Wrapper-side, on the dev's logged-in Claude subscription.** No backend API key, no Anthropic SDK
  in the backend, **no embeddings in v1.**
- The model judge call uses a **cheap tier (Haiku, `claude-haiku-4-5`)**.

### 4.2 Topic segmentation

- **Anchor the topic on the artifact under work** — files/diffs being edited (already captured in
  `tool.call`/`tool.result`) — plus the **user's prompts** as the leading drift signal (a user can
  ask for a new direction before any code exists).
- A **gated Haiku judge** runs on the **`Stop` hook** (turn boundary), **debounced**, and **only when
  a cheap gate fires** (the artifact-of-focus changed, or the turn looks like a correction). Cheap
  signals decide _when_ to spend a model call; the model decides the boundary.
- On drift, **relabel the current session's topic.** Topic labels are **session-local** — we never
  reconcile or normalize a session's topic against anything outside that session.

### 4.3 Correction routing (additive, not exclusive)

- **Every correction feeds the implementing agent** — unconditionally. Doc-silent, doc-contradicting,
  or Claude-just-got-it-wrong: all of it becomes a convention/fix the impl agent learns. This is what
  lets the impl agent eventually one-shot _to completion even when the user goes off-spec_.
- **A clear contradiction with the docs _additionally_ produces a doc-writing-agent learning** — "a
  question to ask up front so the user thinks it through more fully." The doc comparison is purely
  **additive**; it never diverts a learning away from the impl agent.
- The contradiction check compares against the **nearest feature doc by the files touched**, falling
  back to the PRD, or **nothing** when no doc exists. With no docs, the doc-question stream simply
  never fires and impl-learnings still capture everything.
- Bias toward "not a contradiction" unless it's **unambiguous** — keep the doc-question stream
  high-precision.

### 4.4 Structured per-topic record

Folded incrementally (not free-text prose). Shape:

```
{
  topic_label,      // short, for in-session display / relabel
  description,      // RICH, self-contained — written to be searched/clustered later
  doc_ref,          // nearest feature doc, or null (undocumented feature)
  intended,         // what the doc / first prompt asked for
  actual,           // what the code + user converged on
  impl_learnings:[],// conventions / fixes — fed by EVERY correction
  doc_questions:[]  // questions to ask up front — fed ADDITIVELY by contradictions
}
```

### 4.5 Descriptions must be search/cluster-ready (acceptance criterion)

Even though clustering is deferred, **each topic's `description` must be rich and self-contained** —
not a 3-word title — so the later search+cluster phase has good signal. This is cheap to bake in now
and expensive to retrofit (you'd have to re-summarize all history). It shapes:

- the **judge's output**: emit a descriptive, standalone summary per topic, plus a short label; and
- the **stored schema**: keep `topic_label` (display) **and** `description` (search-ready) separately.

Research backs this: self-contained summaries cluster far better; terse ones "rot."

### 4.6 Persistence & UI

- Persist topic + rolling description + structured learnings into Command HQ via a **new event type**,
  folded into the `SessionProjection`. Add fields distinguishing **stable slug/name** vs **evolving
  topic** vs **rolling description/summary** (avoids title thrash), plus `summaryUpdatedAt` /
  `titleSource`.
- Web UI surfaces **within-session topic segments** in the sessions view.

---

## 5. Resolved Decisions

| #   | Decision                         | Resolution                                                                                                                                                                            |
| --- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Where v1 stops on the agent side | Stop at **captured → classified → stored** learning records (+ a thin hand-off stub). The live `hq-optimize-agent` editing loop is the **next tier**.                                 |
| 2   | How the wrapper invokes Haiku    | **Headless Claude Code subprocess** on the dev's logged-in subscription — no API key.                                                                                                 |
| 3   | The two target agents            | Assumed **minted/managed by the existing Forge machinery**; v1 just produces their inputs.                                                                                            |
| 4   | Cross-session grouping           | **Out of v1.** Cross-session combination is **search-driven** (the agent-builder searches for similar topics later), not UI grouping — and it depends on the deferred embedding tier. |
| 5   | Routing model                    | **Additive**, not a router: every correction → impl agent; contradictions _also_ → doc-writing agent.                                                                                 |

---

## 6. Out of Scope / Deferred

- **Embeddings + semantic search/clustering across sessions.** This is the real payoff — the
  agent-builder searches stored `description`s for topics similar to what it's creating, and recurring
  failure modes cluster into agent improvements. It reuses the **dormant `SessionVector.vector` field**
  (`packages/shared/src/dto.ts` ~line 366) and the **synth-only `forge-sessions` OpenSearch collection**
  (`infra/lib/search-stack.ts`). **Not built in v1**, but v1's descriptions must be rich enough to feed it.
- **The live agent-improvement loop** (`hq-optimize-agent` actually editing the two agents). v1 stops at
  stored records + a thin hand-off stub.
- **Verbatim transcript upload** to Command HQ. The event stream stays the unit of capture; raw
  transcripts stay local.
- **Any backend-side LLM calls / Anthropic API key in the backend.**
- **Cross-session topic grouping in the UI** (including cheap exact-match grouping).

---

## 7. End-State Vision

The two learning streams compound from the same captured corrections:

- The **implementing agent** accumulates a _convention library_ — every time someone had to add
  forgot-password, rate-limiting, or validation states, it learns "a login page includes these by
  default" → it builds **to completion** from a vague prompt, filling gaps the docs never mentioned.
- The **doc-writing agent** accumulates an _interview checklist_ — every contradiction teaches it a
  question to ask up front → fewer contradictions reach implementation → **less correction** overall.

---

## 8. Key Existing Code To Build On (verified)

| Area                                                                                                              | Location                                              | State                          |
| ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- | ------------------------------ |
| Daemon emits `session.rename` (name = slug from `autoname.go`; summary = first prompt, capped 200, never updates) | `wrapper/internal/daemon/daemon.go:259,286`           | Build on                       |
| Event schema (`sessionRenameEventSchema` has optional `summary`)                                                  | `packages/shared/src/events.ts`                       | Extend                         |
| Projection fold                                                                                                   | `packages/backend/src/ws/projection.ts:89`            | Extend                         |
| Session DTO (`sessionProjectionSchema.summary`)                                                                   | `packages/shared/src/dto.ts:142`                      | Extend                         |
| Session vector schema (unused `summary` + `vector`)                                                               | `packages/shared/src/dto.ts:~366`                     | Deferred tier                  |
| Transcript → events capture                                                                                       | `wrapper/internal/capture/jsonl.go`                   | Build on                       |
| Hooks (Stop, UserPromptSubmit, …)                                                                                 | `wrapper/internal/capture/hooks.go`                   | Build on                       |
| Sessions REST                                                                                                     | `packages/backend/src/rest/sessions.ts`               | Extend                         |
| DB single-table (`PROJ#<projectId>` / `SESS#<sessionId>`)                                                         | `packages/backend/src/db/repo.ts`, `keys.ts`          | Extend                         |
| Web sessions table                                                                                                | `packages/web/src/screens/Sessions/SessionsTable.tsx` | Extend                         |
| Drift detection / Anthropic SDK call / full-session summarization                                                 | —                                                     | **Absent — this is the build** |

---

## 9. Open Technical Questions (for the next planning pass)

These are deliberately deferred to technical planning, not resolved here:

- **Where the judge runs** — daemon (Go) shelling out to headless Claude Code, vs. the wrapper CLI process.
- **The headless invocation path itself** — it does not exist yet; it's its own work item (how to call the
  dev's logged-in Claude Code non-interactively and get structured output back).
- **The gate** — exact cheap signals and thresholds that decide _when_ to spend a judge call; debounce window.
- **Transcript slice** — what bounded, position-aware slice of the JSONL the judge sees per call.
- **Event schema specifics** — the new event type's shape and how it folds into the projection.
- **DynamoDB layout** — how the structured per-topic learning records attach under the existing table.
- **The thin hand-off stub** — the minimal shape that leaves records "ready for" the deferred optimize loop.
- **Correction / contradiction detection** — the judge prompt(s) and how `is_correction` / `contradicts_doc`
  are determined from the slice + nearest doc.
