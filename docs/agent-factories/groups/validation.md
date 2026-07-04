# Validation

**Type:** Pipeline — step 6

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Validate implemented code along several independent axes. Each axis is its own
concern (and likely its own sub-agent).

## Sub-agents / checks (to flesh out)

- **Consistency** — follows code consistency / conventions.
  May call [Code Research](./code-research.md).
- **Duplication** — checks against code duplication.
  Also via [Code Research](./code-research.md).
- **Testing** — writes good tests.
- **End-to-end expansion** — expands E2E testing to see the feature in context.
- **Simplify** — reduce complexity.
- **AI unslopify** — strip AI-slop patterns / tells.

## Input

- The code produced by [Specs → code](./specs-to-code.md).

## Output

- Validated code (consistent, de-duplicated, tested, simplified, un-slopified).
- Findings / fixes per axis.

## Execution model (decided)

Multiple sweeps run **in parallel**, each axis a concurrent sweep, then results merge
into one report. Conflict handling is the hard part — research is unambiguous that
parallel agents in a *shared* workspace perform **below** a single agent (instruction-only
coordination measured at 55.5% vs 57.2% baseline on PaperBench). So:

**Physical isolation is mandatory — one git worktree/branch per sweep.** No exceptions.

```
1. PLAN (one cheap LLM call):
   - predict which files each sweep will touch
   - flag "hot files" (config, registries, shared utils, __init__) for single-writer
2. RUN (parallel worktrees, all branched from current HEAD):
   - sweep/consistency, sweep/dedup, sweep/tests, sweep/simplify, sweep/unslop
   - probe `git merge-tree` periodically to surface conflicts early
3. MERGE GATE (sequential, tests between each):
   - order least- to most-invasive: dedup → consistency → simplify → unslop → tests
     (test-writing mostly adds new files → least conflict risk → merge last)
   - on conflict, the SWEEP THAT PRODUCED IT rebases and resolves (context is local
     to that agent; a central coordinator divining intent from both sides is worse)
4. RE-VALIDATE once on the final merged state
   - catches "sweep A fixed X, then sweep B's merge broke it"
```

Sequential-gated parallel execution (vs. shared workspace) measured **+14 to +27 points**
in the same research, so the merge gate is worth its wall-clock cost. Cap at ~4–6
concurrent sweeps before integration overhead eats the gains.

**Auto-fix vs. report:** mechanical axes auto-fix inside their worktree (dedup,
simplify, unslop, tests). Judgment-heavy consistency findings that can't be cleanly
auto-applied get reported for review rather than force-merged.

**Pass gate:** "passed validation" = all sweeps merged, the final re-validation pass is
clean, and the test suite is green on the merged state.

## Hot-file policy (decided)

**Optimistic merge + structural pre-partitioning — not pessimistic locks.** The dominant
2024–25 pattern for parallel coding agents is worktree isolation with an optimistic merge
gate; filesystem/branch-level locks create a single contention point and risk
`.git/index.lock` deadlock that freezes every agent. So the merge gate stays the default
path — but defuse hot files *structurally* before they get there:

1. **Pre-partition at plan time** — the planning pass assigns each likely-touched file to
   **at most one sweep per round**. This removes most conflicts without any lock.
2. **Genuinely shared files** (config, registries, `__init__`, shared utils) are owned by
   the **merge-gate step itself**, not by any parallel sweep. Sweeps emit a *structured
   intent* ("add key X to the registry") rather than editing the file directly; the gate
   applies those intents sequentially and atomically.

So: serialize the truly-hot files via single-writer ownership at the gate, and let the
optimistic merge gate catch the rare residual collision on everything else. This is the
same rule [specs-to-code](./specs-to-code.md) uses for parallel ticket agents.

> Sources: parallel-agent worktree/merge practice (Augment Code, Cursor 2.0, Claude Code);
> optimistic-vs-pessimistic locking tradeoffs; Claude Code worktree-lock-contention issue.
