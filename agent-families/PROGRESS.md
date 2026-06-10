# Status: active
# Current plan: 001   Current unit: U1

This manifest is PRE-GENERATED from the five plans and is AUTHORITATIVE.
Loop agents: flip checkboxes and statuses only. NEVER add, remove, merge,
or renumber unit lines. 42 units total. A unit is `done` only when its
plan's Files exist, its Test scenarios are implemented, its Verification
holds, and the full offline suite is green.

## Units

### Plan 001 — Phase 0: Library Core (docs/plans/2026-06-10-001-feat-agent-families-phase0-library-core-plan.md)
- [ ] 001/U1 — Scaffold, config, and repo isolation
- [ ] 001/U2 — SQLite schema, store layer, snapshots, promotion queue
- [ ] 001/U3 — Embedding service and vector index
- [ ] 001/U4 — Judge runner with record/replay seam
- [ ] 001/U5 — add_idea pipeline
- [ ] 001/U6 — Lifecycle operations
- [ ] 001/U7 — Rendering: concatenation and delta-patch compile
- [ ] 001/U8 — SKILL.md export
- [ ] 001/U9 — CLI assembly, init, and end-to-end acceptance

### Plan 002 — Phase 1: Pipeline Skeleton (docs/plans/2026-06-10-002-feat-agent-families-phase1-pipeline-skeleton-plan.md)
- [ ] 002/U1 — Phase 1 schema migration
- [ ] 002/U2 — Output-stack template repo and workspace lifecycle
- [ ] 002/U3 — run_session seam and scripted-agent fake
- [ ] 002/U4 — Orchestrator core: run/ticket state machine, checkpoint/resume
- [ ] 002/U5 — Planning stage
- [ ] 002/U6 — Ticket loop: worker, harness gate, verifier
- [ ] 002/U7 — Trace capture, shadow tripwires, accounting
- [ ] 002/U8 — Toy-spec corpus and end-to-end acceptance

### Plan 003 — Phase 2: Explorer + Grader (docs/plans/2026-06-10-003-feat-agent-families-phase2-explorer-grader-plan.md)
- [ ] 003/U1 — Phase 2 schema migration
- [ ] 003/U2 — Target harness: linkding lifecycle
- [ ] 003/U3 — Registry pre-research and frontier ledger
- [ ] 003/U4 — Scenario harness: resolve, cache, replay, heal
- [ ] 003/U5 — Explorer subsystem
- [ ] 003/U6 — Episode orchestration
- [ ] 003/U7 — Grader settlement and report
- [ ] 003/U8 — Mutation-seeded verifier audits
- [ ] 003/U9 — Trace-query CLI, idea provenance, and episode e2e

### Plan 004 — Phase 3a: Close the Learning Loop (docs/plans/2026-06-10-004-feat-agent-families-phase3a-learning-loop-plan.md)
- [ ] 004/U1 — Schema spine migration
- [ ] 004/U2 — Retrieval into prompts
- [ ] 004/U3 — Run-scoped working memory
- [ ] 004/U4 — Kanboard onboarding + frozen micro-benchmark
- [ ] 004/U5 — Reflector Stage A
- [ ] 004/U6 — Reflector Stage B and batch formation
- [ ] 004/U7 — Validation and promotion
- [ ] 004/U8 — Ratchet and skill split
- [ ] 004/U9 — Learning-cycle e2e

### Plan 005 — Phase 3b: Training at Scale (docs/plans/2026-06-10-005-feat-agent-families-phase3b-training-at-scale-plan.md)
- [ ] 005/U1 — Benchmark suite and epochs
- [ ] 005/U2 — Rehearsal pass and one-shot metric
- [ ] 005/U3 — Improvement-tier grading
- [ ] 005/U4 — Family router and agent splitting
- [ ] 005/U5 — Parallel episodes and batch merging
- [ ] 005/U6 — Enforcement activation and annealing
- [ ] 005/U7 — RealWorld calibration and scale e2e

## Final milestone (after all 42 units, before COMPLETE)
- [ ] AUDIT — per-plan audit pass written to agent-families/AUDIT.md (every unit: Files exist, each Test scenario mapped to a test, Verification re-confirmed)
- [ ] LIVE — one full episode against linkding; clone built (`npm run build && npm run preview`) and core flows verified via Playwright; score/cost/start-command recorded below
- [ ] Plans 001–005 frontmatter flipped to `status: completed`

## Deviations

## Probe findings

## Blockers
