# Specs → Code

**Type:** Pipeline — step 5

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

A worker that iterates through tickets and implements them.

## Input

- Ready-to-code specs / tickets from [Features → specs](./features-to-specs.md).

## Output

- Implemented code for each ticket.

## Behavior

- **Parallel** workers: tickets are implemented concurrently, not one-at-a-time.
- Leans on [Code Research](./code-research.md) for conventions and placement.

## Sub-agents (to flesh out)

- **Implementer** — does the actual coding for one ticket.
- (Possibly) per-domain implementers (frontend / backend / infra).

## Execution model (decided)

Tickets run in **parallel**, scheduled by an explicit dependency graph. Grounded in
DAG-scheduler practice (Bazel/Airflow) and agent-orchestration frameworks (LangGraph
`Send`, Flyte planner): the standard, proven pattern is **Kahn's algorithm with a
continuous ready queue** — not fixed "batch a wave, wait for all."

**Dependencies are declared upstream, at spec time.** The
[Features → specs](./features-to-specs.md) stage emits them per ticket; the scheduler is
a pure graph executor that trusts the graph. Ticket schema:

```json
{
  "id": "ticket-B",
  "depends_on": ["ticket-A"],          // authoritative edges the scheduler reads
  "provides": ["UserProfile schema"],  // what it produces (upstream wires deps from these)
  "file_owners": ["src/models/user.ts"] // files it writes (collision safety net)
}
```

**Scheduler (event-driven):**
```
ready = tickets with in_degree 0          # dispatch ALL of these in parallel
on ticket T done:
    inject T.output into dependents' context
    for d in T.blocks: if all deps done → ready.add(d)   # unblock immediately, not at wave end
use asyncio.wait(FIRST_COMPLETED), semaphore-cap parallelism at ~4–6 agents
```

Decisions:
- **Worktree per ticket** (same isolation rule as [Validation](./validation.md)).
- **Cycle detection before run** — any cycle is a planning error; bounce back to specs.
- **File-ownership safety net** — if two tickets own overlapping `file_owners` and
  neither declares a dep, force-add a serialization edge so they don't collide.
- **Blocking-ticket failure → `cascade_skip`**: mark transitive dependents `SKIPPED`,
  let independent branches finish, log skipped IDs for a targeted re-run. (Don't halt the
  whole pipeline; don't waste agent time on unreachable tickets.)
- **Dynamic deps discovered mid-implementation** → fail-fast: agent emits
  `{needs: ticket-X}`, scheduler re-queues it once X completes.
- **Validation hook** — runs per merged batch (after the merge gate), not per raw ticket,
  so it validates integrated state.

## Ready-queue ordering (decided)

**FIFO by default; switch to critical-path-first only past a threshold.** CPM priority
(dispatch the ticket with the longest remaining downstream chain first) buys ~5–10%
makespan over FIFO — but *only* when there's real queue pressure. Below that, FIFO and CPM
produce identical makespan, and the bookkeeping isn't worth it. For variable-latency LLM
agents the critical-path estimate is imprecise anyway, shrinking the gain further.

- **< 10 tickets, or ready-queue depth ≤ 2× worker slots** → FIFO.
- **Ready-queue depth routinely > 2× worker slots (and ≳10–15 nodes)** → sort the ready
  queue by a CPM weight, descending. `cpm_weight(t) = cost(t) + max(cpm_weight(successors))`,
  computed in one DFS pass at graph-build time.

> Sources: HEFT / CP-MISF DAG-scheduling studies (~6–10% makespan gains); Bazel scheduler.
