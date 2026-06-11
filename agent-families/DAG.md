# Agent Families — DAG Fan-Out Plan

Parallel execution schedule for the Ralph build. Units within a wave are
**dependency-satisfied by earlier waves** AND **file-disjoint** (no two touch the
same module) — so each wave's units build in isolated worktrees and merge with
zero git conflicts. Orchestrator owns the shared files (pyproject.toml, uv.lock,
conftest.py, PROGRESS.md); per-unit agents are forbidden to touch them.

Concurrency cap: 2 (the real ceiling — 3-way file-disjoint waves are rare and
triple the merge surface). Cross-plan boundaries are strict serial barriers:
each plan's schema migration (store.py) blocks on the prior plan being fully merged.

Legend: `U# {primary files}`

## Plan 001 — Library Core  (U1,U2,U3 done)
- W1: `U4 {judge.py, fixtures/judge}`            # solo (U3 already done)
- W2: `U5 {pipeline.py}`  ||  `U7 {rendering.py}`
- W3: `U6 {lifecycle.py}` ||  `U8 {export.py}`
- W4: `U9 {cli.py, test_e2e.py, README}`         # integration, solo
=== BARRIER: full suite green, Plan 001 complete ===

## Plan 002 — Pipeline Skeleton
- W5: `U1 {store.py migration}` || `U2 {template/, workspace.py}`
- W6: `U3 {sessions.py}`                          # solo (needs 002U1)
- W7: `U4 {orchestrator.py}` || `U5 {planning.py}`
- W8: `U6 {ticket_loop.py, gate.py, devserver.py}` || `U7 {tripwires.py}`
- W9: `U8 {specs/, test_pipeline_e2e.py}`        # integration, solo
=== BARRIER ===

## Plan 003 — Explorer + Grader
- W10: `U1 {store.py migration}` || `U2 {targets/linkding/, target_env.py}`
- W11: `U3 {registry.py, frontier.py}` || `U4 {scenarios.py}`
- W12: `U5 {explorer.py, oracle_check.py}` || `U7 {settle.py}`
- W13: `U6 {episode.py}` || `U8 {calibrate.py}`
- W14: `U9 {trace cli, test_e2e_episode.py}`     # integration, solo
=== BARRIER ===

## Plan 004 — Learning Loop
- W15: `U1 {store.py migration}` || `U4 {targets/kanboard/, benchmark.py}`
- W16: `U2 {retrieval.py}` || `U5 {stage_a.py, template coverage}`
- W17: `U3 {runmemory.py}` || `U6 {stage_b.py}`
- W18: `U7 {validate.py}`                         # solo (needs U2,U4)
- W19: `U8 {maintenance.py}`                      # solo (needs U7)
- W20: `U9 {test_e2e_learning.py}`               # integration, solo
=== BARRIER ===

## Plan 005 — Training at Scale
# U1-U6 depend ONLY on Plan 0-4 units (none on each other) and are file-disjoint -> 6-wide.
# Only U7 (scale e2e) needs all of them. Safe at 6-wide because the merge is PARTIAL:
# units that build green cherry-pick in; only build-failures/conflicts serialize (no all-or-nothing).
- W21: `U1 || U2 || U3 || U4 || U5 || U6`  # all six concurrent
- W22: `U7 {targets/realworld/, test_e2e_scale.py}`  # integration, solo (needs U1-U6)
=== FINAL: AUDIT + LIVE milestone ===

## Notes
- Solo waves (integration units, schema migrations) run a single Ralph loop.
- 2-wide waves run two worktree Ralph loops in parallel, then copy-merge
  (disjoint files) into the integration branch and gate on the full suite.
- Any wave whose merged suite goes red is discarded and re-run SERIALLY
  (fallback) so parallel speculation can never corrupt the integration branch.
- Later-plan waves (W10+) are provisional; the driver re-checks dependency
  satisfaction against PROGRESS.md and falls back to serial on any doubt.
