# Status: active
# Current plan: 001   Current unit: U4

This manifest is PRE-GENERATED from the five plans and is AUTHORITATIVE.
Loop agents: flip checkboxes and statuses only. NEVER add, remove, merge,
or renumber unit lines. 42 units total. A unit is `done` only when its
plan's Files exist, its Test scenarios are implemented, its Verification
holds, and the full offline suite is green.

## Units

### Plan 001 — Phase 0: Library Core (docs/plans/2026-06-10-001-feat-agent-families-phase0-library-core-plan.md)
- [x] 001/U1 — Scaffold, config, and repo isolation
- [x] 001/U2 — SQLite schema, store layer, snapshots, promotion queue
- [x] 001/U3 — Embedding service and vector index
- [x] 001/U4 — Judge runner with record/replay seam
- [x] 001/U5 — add_idea pipeline
- [x] 001/U6 — Lifecycle operations
- [x] 001/U7 — Rendering: concatenation and delta-patch compile
- [x] 001/U8 — SKILL.md export
- [x] 001/U9 — CLI assembly, init, and end-to-end acceptance

### Plan 002 — Phase 1: Pipeline Skeleton (docs/plans/2026-06-10-002-feat-agent-families-phase1-pipeline-skeleton-plan.md)
- [x] 002/U1 — Phase 1 schema migration
- [x] 002/U2 — Output-stack template repo and workspace lifecycle
- [x] 002/U3 — run_session seam and scripted-agent fake
- [x] 002/U4 — Orchestrator core: run/ticket state machine, checkpoint/resume
- [x] 002/U5 — Planning stage
- [x] 002/U6 — Ticket loop: worker, harness gate, verifier
- [x] 002/U7 — Trace capture, shadow tripwires, accounting
- [x] 002/U8 — Toy-spec corpus and end-to-end acceptance

### Plan 003 — Phase 2: Explorer + Grader (docs/plans/2026-06-10-003-feat-agent-families-phase2-explorer-grader-plan.md)
- [x] 003/U1 — Phase 2 schema migration
- [x] 003/U2 — Target harness: linkding lifecycle
- [x] 003/U3 — Registry pre-research and frontier ledger
- [x] 003/U4 — Scenario harness: resolve, cache, replay, heal
- [x] 003/U5 — Explorer subsystem
- [ ] 003/U6 — Episode orchestration
- [x] 003/U7 — Grader settlement and report
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

- **001/U1 commit scope** — U1's Files/Verification explicitly require repo-root
  `.gitignore` (Python section) and `.prettierignore` (+`agent-families/`) edits. This
  is in tension with the loop's "commit ONLY agent-families/ + docs/plans/" rule.
  Resolved in favor of the plan (authoritative for WHAT); the two ignore files are
  committed with U1. The forbidden product dirs (packages/, infra/, wrapper/,
  catalog/, scripts/) were not touched. Subsequent units should not need root changes.
- **001/U1 scaffold method** — Used hand-written `pyproject.toml` + `uv sync` rather
  than literal `uv init --package` to avoid clobbering the pre-existing PROGRESS.md /
  ralph.* files in the dir. Outcome is equivalent: src layout, py3.12 pin, `af`
  console script, committed `uv.lock`. `readme` key omitted from pyproject so README.md
  stays owned by U9.

## Probe findings

- **Environment bootstrap (001/U1)** — Neither `uv` nor Python 3.12 was present on
  this machine (only Python 3.14 + an active outer `.venv`). Installed `uv 0.11.19`
  via the official standalone installer to `%USERPROFILE%\.local\bin`, then
  `uv python install 3.12` → CPython 3.12.13. The plan mandates uv + py3.12; this is
  a one-time host setup, no repo files involved. NOTE for future iterations: prepend
  `$env:USERPROFILE\.local\bin` to PATH and clear `VIRTUAL_ENV` (an outer `.venv` is
  active and uv warns/ignores it) before `uv run`.

## Blockers

- DRIVER: iteration 1 reverted (red suite)
