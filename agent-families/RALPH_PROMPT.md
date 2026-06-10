# Agent Families — Autonomous Implementation Loop

You are one iteration of a Ralph loop building the agent-families system end to end.
You have fresh context. Everything you need to know is in the files below. Do ONE unit
of work well, leave the trail clean, and exit. The loop will run you again.

## Authoritative sources (read in this order, every iteration)
1. `agent-families/PROGRESS.md` — the loop's memory. The unit manifest in it is
   PRE-GENERATED and AUTHORITATIVE: 42 units across five plans. You may flip
   checkboxes and statuses ONLY. Never add, remove, merge, or renumber unit lines.
2. The plan you are currently inside (per PROGRESS), one of, in strict order:
   - `docs/plans/2026-06-10-001-feat-agent-families-phase0-library-core-plan.md`
   - `docs/plans/2026-06-10-002-feat-agent-families-phase1-pipeline-skeleton-plan.md`
   - `docs/plans/2026-06-10-003-feat-agent-families-phase2-explorer-grader-plan.md`
   - `docs/plans/2026-06-10-004-feat-agent-families-phase3a-learning-loop-plan.md`
   - `docs/plans/2026-06-10-005-feat-agent-families-phase3b-training-at-scale-plan.md`
3. `docs/agent-families/DESIGN.md` — consult for rationale when a plan is ambiguous.
   The plans are authoritative for WHAT; the design for WHY.

## Your task this iteration
1. From PROGRESS.md, find the first unchecked unit, respecting each unit's declared
   Dependencies in its plan. Units execute in plan order 001→005, U-ID order within
   a plan unless dependencies say otherwise.
2. Implement that ONE unit exactly as specified: its Files, Approach, and EVERY listed
   Test scenario. Do not start a second unit. Do not redesign — if the plan and reality
   conflict, prefer the smallest faithful adaptation and record it under `## Deviations`.
3. Verify per the unit's Verification field. Run `uv run pytest` in `agent-families/` —
   the full offline suite must be green before a unit is `done`. Docker-required tests
   run only if Docker is available; otherwise mark the unit `pending-docker` in
   PROGRESS.md — never delete or skip-mark them in code.
4. Commit: one commit for the unit, message
   `feat(agent-families): <plan-nnn> <U-ID> — <unit name>`, ending with
   `Co-Authored-By: Claude <noreply@anthropic.com>`. Commit ONLY files under
   `agent-families/` and `docs/plans/` (status flips). Never touch `packages/`,
   `infra/`, `wrapper/`, `catalog/`, or `scripts/`.
5. Update PROGRESS.md (checkbox, current-plan/unit header, any deviations/probe
   findings/blockers) and exit cleanly.

## Hard rules (from the plans — do not relax)
- Python 3.12 + uv, src layout, everything self-contained under `agent-families/`
  (the `wrapper/` precedent: zero references from root package.json).
- LLM calls: headless `claude -p` on the logged-in subscription ONLY. Never use or
  request an API key. Judge/test discipline: record/replay fixtures and scripted
  fakes — the offline suite passes with zero quota and no `claude` on PATH.
- The empirical probes the plans name (`--bare` auth check; quota-envelope shape) are
  real units of work where specified — run them, record findings under
  `## Probe findings`, honor the plans' documented fallbacks.
- Windows is the platform: utf-8 subprocess encoding, node-entrypoint resolution for
  the claude shim, no bind-mounted SQLite for targets, `newline='\n'` on writes that
  feed byte-stability tests.
- Thresholds live in `thresholds.toml` with provenance comments — never hardcode a tunable.
- When a plan marks something a Phase-N seam or deferral, build the seam, never the
  deferred machinery.

## Stuck / failure handling
- If you cannot make the suite green this iteration: revert to the last green state,
  mark the unit `blocked(<reason>)` with your diagnosis, and exit. Never leave the
  suite red, never leave uncommitted changes.
- If the same unit is `blocked` with the same reason twice in a row: write the blocker
  under `## Blockers`, set the first line to `# Status: blocked`, exit. A human decides.
- Quota exhausted: commit green state, note it, exit — the loop resumes later.

## Completion (only after all 42 units are checked)
1. AUDIT: run a per-plan audit and write `agent-families/AUDIT.md` — for every unit:
   its Files exist, each listed Test scenario maps to an actual test (name them), its
   Verification re-confirmed. Any gap found → uncheck that unit, fix it in subsequent
   iterations. COMPLETE is unreachable while AUDIT.md records gaps.
2. LIVE: execute the documented live procedure — one full episode against linkding;
   build the clone (`npm run build && npm run preview` in the episode workspace) and
   verify core flows (login, bookmark CRUD, search) with Playwright. This is "the new
   website up and going." Record score, cost, and the clone's start command in
   PROGRESS.md.
3. Flip each plan's frontmatter `status: active` → `completed`; check the three Final
   milestone boxes; set the first line of PROGRESS.md to `# Status: COMPLETE`.
