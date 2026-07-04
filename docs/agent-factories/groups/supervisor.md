# Supervisor

**Type:** Top-level orchestrator (sits above every other group)

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Its sub-agents below are each their own prompt command + skills.

## Purpose

Take a task and run the right workflow to completion. The supervisor is the entry
point: it decides *which* agent groups to chain, in what order, and with what gates —
then drives that chain. Every other group in this folder is a building block it composes.

## Input

- **Task** — what to accomplish (a goal, a feature, a ticket, a bug, a question).
- **Workflow (optional)** — which orchestration to run. If omitted, the supervisor
  selects one from the task (see Routing).
- **Steps (optional)** — explicit step list / overrides when the caller wants to pin the
  exact sequence rather than let the orchestration decide.

## Output

- The completed work (code, docs, answers) produced by the chained groups.
- A run record: which orchestration ran, which groups fired, gate outcomes, what's left.

> **v2 reframe (see [DEFENSE.md](../DEFENSE.md)):** the modes below survive as **intake
> tiers**, not separate pipelines. The supervisor now runs one standard machine —
> **Define** (scope-tiered spec acquisition) → **Converge** (mechanical gap assessment →
> ticketize → standardized churn → verify, loop until the gap list is empty). A
> greenfield task simply runs the full Define chain (goal research → architecture →
> feature list → specs) before entering the loop; a bugfix enters the loop with a
> one-sentence mini-spec. Gap assessment is mechanical (unchecked tasks + untested ACs +
> failing tests + open questions), completion is a structured state change — never the
> model's self-report — and stall detection (gap trend, circuit breaker, budgets)
> escalates to a human. The mode table below remains useful as the tier-classification
> rubric.

## Sub-agents — orchestration modes

Each sub-agent is a **recipe**: a named chaining of the other groups with its own entry
conditions, gates, and stop criteria. The supervisor picks one (or runs the one named).

### `greenfield` — nothing → shipped product
Full pipeline from a standing start.
```
high-level-goal-research → goal-to-architecture → goal-to-feature-list
  → features-to-specs → specs-to-code → validation
```
- Idea loops ([idea-research ↔ idea-refinement](./idea-refinement.md)) fire inside the
  architecture and feature-list steps.
- Human gate after goal-research and after feature-list (cheap to redirect early).

### `ralph` — ticket-munching loop (HumanLayer-style autonomous)
Steady-state: a queue of ready tickets already exists; chew through it autonomously.
```
loop: pick ready ticket(s) → specs-to-code (parallel) → validation → merge → next
```
- Driven by the [specs-to-code](./specs-to-code.md) dependency scheduler (Kahn waves,
  cascade_skip on failure).
- Runs unattended; surfaces only blockers and the final batch for review.
- The "munch until the queue is dry" mode — closest to the existing ralph/oneshot skills.

### `feature` — brownfield feature add
Add one feature to an existing codebase.
```
code-research (does it exist? where? reuse vs centralize?)
  → idea-research ↔ idea-refinement (how best to build it here)
  → features-to-specs → specs-to-code → validation
```
- Always starts with [code-research](./code-research.md) so it builds *with* the grain
  of the repo, not against it.

### `fix` — single bug / small task
Minimal chain for a scoped change.
```
code-research (locate + root cause) → specs-to-code (one ticket) → validation
```
- No idea loop, no architecture step. Optimized for latency.

### `research-only` — answer, don't change code
```
code-research and/or idea-research → answer
```
- Hard gate: produces findings, never edits the repo. (Mirrors office-hours / research
  skills.)

### `custom` — caller-supplied steps
Runs the explicit **Steps** input as the chain, validating each named step resolves to a
real group. The escape hatch when none of the canned recipes fit.

## Routing (when no workflow is named)

The supervisor classifies the task → mode:

| Task looks like… | Mode |
|---|---|
| "build a new product/app from this idea" | `greenfield` |
| "work the backlog / implement these tickets" | `ralph` |
| "add X to the existing app" | `feature` |
| "fix / debug Y" | `fix` |
| "where/how/should we… (no change)" | `research-only` |
| caller pinned the steps | `custom` |

When ambiguous, the supervisor asks one clarifying question rather than guessing.

## Responsibilities (beyond just chaining)

- **Gating** — decide human-checkpoint vs. autonomous per mode; honor the parallel
  isolation + merge-gate rule from the [README](../README.md) wherever it runs agents.
- **State / handoff** — carry context between groups (goal → architecture → specs…) and
  emit a resumable run record so a stopped run can pick back up.
- **Budget & concurrency** — cap parallel agents (~4–6), set round budgets for idea
  loops, decide when to stop.
- **Failure policy** — propagate `cascade_skip`, retries, and escalation; know when to
  halt vs. continue independent branches.

## Orchestration model (decided)

### Linear phases that fan out — not a free-form DAG
The supervisor runs a **linear pipeline of named phases** (plan → implement → validate →
merge); each phase **internally fans out in parallel** via the existing Kahn scheduler.
Add cross-phase DAG edges *only* when a phase genuinely needs conditional branching or a
cycle (e.g. "tests red → remediate, else → ship"). This is "90% of the benefit at 20% of
the complexity" — full graph engines (LangGraph) are worth it only for inter-phase
conditionals/loops. So **`custom` is a linear step list by default**, with optional
conditional edges, not an arbitrary DAG.

### Routing: classifier-first, planner as fallback
**Fixed classifier routes to the N predefined modes; a single "complex/unknown" bucket
escalates to a dynamic LLM planner.** A small fast model classifies cheaply and
predictably for the common case (greenfield / ralph / feature / fix / research-only);
only genuinely novel task shapes invoke a capable model as an orchestrator-worker planner.
This matches Anthropic's "start simple, add capability/cost only when needed." The
supervisor does **not** run an idea-loop to pick a mode in the common path — that's
reserved for the escalation bucket.

### Resume: durable journal + idempotency
Persist a **step journal** so a crashed/paused run resumes mid-mode ("ralph died at
ticket 7" → resume at 8 without redoing 1–7):

```
workflow_runs(id, mode, status, current_step, updated_at)
step_log(workflow_id, step_id, step_name, output_json, idempotency_key, completed_at)
```
On each step: check `idempotency_key` (`{workflow_id}:{step_name}`) → skip if present;
else execute, append output to `step_log`, advance `current_step`. On restart: find
`status='running'` runs and replay from `step_log`. **Every external side effect (open a
PR, post a comment, trigger CI) must carry a deterministic idempotency key** or resume
double-fires it. This is a lightweight durable-execution engine; reach for Temporal /
LangGraph-checkpointer only if runs are long-lived and must auto-resume with zero human
intervention.

> Sources: Anthropic "Building Effective Agents" (router vs. orchestrator-worker);
> LangChain→LangGraph guidance; Temporal / LangGraph checkpointer durable-execution
> (Diagrid "checkpoints are not durable execution").
