# Status: active
# Current plan: 001   Current unit: U4

This manifest is PRE-GENERATED from the five plans and is AUTHORITATIVE.
Loop agents: flip checkboxes and statuses only. NEVER add, remove, merge,
or renumber unit lines. 78 units total (42 core + 15 greenfield + 21 R3 reform, Plans 008-010). A unit is `done` only when its
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
- [x] 003/U6 — Episode orchestration
- [x] 003/U7 — Grader settlement and report
- [x] 003/U8 — Mutation-seeded verifier audits
- [x] 003/U9 — Trace-query CLI, idea provenance, and episode e2e

### Plan 004 — Phase 3a: Close the Learning Loop (docs/plans/2026-06-10-004-feat-agent-families-phase3a-learning-loop-plan.md)
- [x] 004/U1 — Schema spine migration
- [x] 004/U2 — Retrieval into prompts
- [x] 004/U3 — Run-scoped working memory
- [x] 004/U4 — Kanboard onboarding + frozen micro-benchmark
- [x] 004/U5 — Reflector Stage A
- [x] 004/U6 — Reflector Stage B and batch formation
- [x] 004/U7 — Validation and promotion
- [x] 004/U8 — Ratchet and skill split
- [x] 004/U9 — Learning-cycle e2e

### Plan 005 — Phase 3b: Training at Scale (docs/plans/2026-06-10-005-feat-agent-families-phase3b-training-at-scale-plan.md)
- [x] 005/U1 — Benchmark suite and epochs
- [x] 005/U2 — Rehearsal pass and one-shot metric
- [x] 005/U3 — Improvement-tier grading
- [x] 005/U4 — Family router and agent splitting
- [x] 005/U5 — Parallel episodes and batch merging
- [x] 005/U6 — Enforcement activation and annealing
- [x] 005/U7 — RealWorld calibration and scale e2e

### Plan 007 - Greenfield Mode (docs/plans/2026-06-10-007-feat-agent-families-greenfield-mode-plan.md)
QUEUED after Plans 001-005. Execution follows the plan's dependency DAG, not unit-number order.
- [x] 007/U1 - Migration: greenfield schema + config section
- [x] 007/U2 - Planner contract: ASSUME refactor, PROPOSAL artifact, ranked questions
- [x] 007/U3 - Provenance lint + assumption gate
- [x] 007/U4 - DEC extraction + DEC-coverage lint
- [x] 007/U5 - Degradation generator + blur cache + blur lint
- [ ] 007/U5b - Human-as-founder trial (HUMAN STEP - manual, no production code)
- [ ] 007/U6 - Founder answering, adjudication and acceptance session
- [ ] 007/U7 - Episode world branch, workspace policy and rotation
- [ ] 007/U8 - Decomposition join + elicitation metrics v1
- [ ] 007/U9 - Stage A founder branches
- [x] 007/U10 - Provenance and world telemetry plumbing
- [x] 007/U13a - Kanboard registry pre-research (FEAT + DEC)
- [ ] 007/U13b - Greenfield episode benchmark
- [ ] 007/U11 - af induct <domain> researched-insight induction
- [ ] 007/U12 - Define-chain seed batch

### Plan 008 - R3 Phase A: schema v6 + ingest gauntlet (docs/plans/2026-06-12-008-feat-agent-families-r3-ingest-gauntlet-plan.md)
R3 reform - supersedes Phase 0 author-at-ingest. Scheduled before the 007/U5b human gate (autonomous).
- [x] 008/U1 - Migration v6 + store methods
- [x] 008/U2 - Embedding: three vectors + Matryoshka
- [x] 008/U3 - Vecindex: 3-column table + on= + flattener fix
- [x] 008/U4 - NLI seam (nli.py)
- [x] 008/U5 - Admission gate (Operation 1)
- [x] 008/U6 - add_idea rewrite (key-collision + NLI + corroborate/refine/deferred-supersede)
- [x] 008/U7 - Lifecycle: deferred-supersede at promotion + dormant
- [x] 008/U8 - Reflector routing through the R3 gate
- [x] 008/U9 - Config, thresholds, and e2e acceptance

### Plan 009 - R3 Phase B: derive pass + organization objective + retrieval (docs/plans/2026-06-12-009-feat-agent-families-r3-derive-objective-plan.md)
- [x] 009/U1 - The organization objective (objective.py)
- [x] 009/U2 - Graph build + vecindex neighbors
- [x] 009/U3 - Partitioners + dependencies
- [x] 009/U4 - The derive pass + identity tracking
- [x] 009/U5 - Consolidation + retirement
- [x] 009/U6 - Retrieval: insight-level whole-store rewrite
- [x] 009/U7 - Demotions: router, agent_split, maintenance
- [x] 009/U8 - Rendering / export under derived membership
- [x] 009/U9 - Config + e2e

### Plan 010 - R3 Phase C: stages pivot + assign stage (docs/plans/2026-06-12-010-feat-agent-families-r3-stages-assign-plan.md)
- [ ] 010/U1 - The assign stage
- [ ] 010/U2 - The retrieval vector (third vector)
- [ ] 010/U3 - Finish runtime demotion + re-scope downstream plans

## Final milestone (after all 78 units, before COMPLETE)
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

- REVIEW CHECKPOINT: paused before Plan 005 (novel phase). To proceed: review the prior plan's units + their ## Conformance notes, then create agent-families\REVIEW-OK-005.txt and relaunch ralph-wave.ps1.

- HUMAN STEP: 007/U5b is a manual human-as-founder trial (no production code). Run it per the plan, record transcripts under docs/, flip 007/U5b to [x] in this file, create agent-families\U5B-DONE.txt, then relaunch ralph-wave.ps1.
