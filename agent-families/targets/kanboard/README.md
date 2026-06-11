# Kanboard registry pre-research (FEAT + DEC)

**plan-007 U13a.** Kanboard is the held-out micro-benchmark target (plan-004 U4)
and the parent target of the greenfield episode benchmark (plan-007 U13b, KTD8).
This directory carries Kanboard's registry pre-research apparatus: the FEAT+DEC
tables the greenfield benchmark's founder model is generated from.

The registry pre-research **machinery is target-generic** — U13a ported it off
its linkding pins rather than forking it ("port, don't fork"). The per-target
pins live in code as `KANBOARD_REGISTRY_CONFIG`
(`grading/registry.py`), mirroring `LINKDING_REGISTRY_CONFIG`:

| Pin | Kanboard value | Provenance |
| --- | --- | --- |
| `source_repo_url` | `https://github.com/kanboard/kanboard.git` | upstream repo |
| `source_tag` | `v1.2.46` (= `benchmark.KANBOARD_IMAGE_TAG`) | tag half of the image digest pin (R9) |
| image digest | `benchmark.KANBOARD_IMAGE_DIGEST` | **placeholder** pending live resolution (see below) |
| `expected_area_count` | 26 (estimate) | KTD8: materially larger than linkding's 18 |
| `expected_behavior_range` | (55, 95) (estimate) | KTD8/Open-Question-7 |

The area-count and behavior-range numbers are **estimates** pending the live
hand-check; they are protocol constants for the live procedure, not behaviour
tunables (so they live in code with provenance, not `thresholds.toml`).

## What is committed here vs. produced live

- **Committed:** `.gitignore` (excludes the source checkout + evidence captures)
  and this procedure. The per-target *config* is code in `grading/registry.py`.
- **Produced live (pending-docker):** the minted FEAT+DEC rows in the store and
  their digest-stamped a11y-snapshot evidence under `evidence/`. Minting needs a
  running Kanboard (Docker), a logged-in `claude` CLI for the pre-research
  session, and a Playwright-backed browse executor for runtime confirmation —
  none available in CI. The offline suite exercises the *machinery* on scripted
  fakes (`tests/test_kanboard_registry.py`); the live mint is a documented
  procedure, hand-checked per the Phase 2 discipline.

## Before the first live run

1. **Resolve and re-pin the image digest.** `benchmark.KANBOARD_IMAGE_DIGEST`
   (and `docker-compose.yml`) carry a placeholder. Resolve the real multi-arch
   manifest digest for `kanboard/kanboard:v1.2.46` from Docker Hub and re-pin
   both. Re-pinning is a deliberate apparatus migration that re-runs
   pre-research (R9) — the source tag is re-pinned with it.

## Live procedure (documented, NOT in CI)

```text
# 1. Bring Kanboard up + seed it (plan-004 U4 / benchmark.KanboardTarget)
from agent_families.grading.benchmark import KanboardConfig, KanboardTarget
t = KanboardTarget(KanboardConfig(
    compose_file=Path('targets/kanboard/docker-compose.yml'),
    seed_manifest=Path('targets/kanboard/seed_manifest.json'),
    readiness_timeout_s=180, poll_interval_s=2))
t.up(); t.seed()

# 2. Clone the pinned source (depth-1, tag v1.2.46), verified against the pin
from agent_families.grading.registry import (
    KANBOARD_REGISTRY_CONFIG as C, ensure_source_checkout,
    grader_profile, run_pre_research_full, refresh_registry, refresh_decisions,
    registry_in_expected_range)
ensure_source_checkout('targets/kanboard/source',
                       repo_url=C.source_repo_url, tag=C.source_tag)

# 3. Run the pre-research session LIVE (grader profile, source-scoped read tools)
pre = run_pre_research_full(
    grader_profile(model=..., max_turns=..., timeout_s=...),
    target='kanboard', source_root='targets/kanboard/source',
    transcript_path='targets/kanboard/transcripts/pre-research.jsonl',
    max_retries=...)

# 4. Confirm-on-runtime + mint both registries (digest = the resolved image pin)
refresh_registry(store, pre.candidates, live_browse,
                 target='kanboard', digest=DIGEST,
                 evidence_dir='targets/kanboard/evidence')
refresh_decisions(store, pre.decisions, live_browse,
                  target='kanboard', digest=DIGEST,
                  evidence_dir='targets/kanboard/evidence')

# 5. Verify the surface lands in the expected band + hand-check the tables
assert registry_in_expected_range(store, 'kanboard')
```

## Hand-check (sign-off gate — U13a Verification)

Eyeball the minted Kanboard FEAT+DEC tables per the Phase 2 discipline (the
table is materially larger than linkding's — budget the check). If FEAT+DEC
pre-research lands well above the qualification band, scope the benchmark's
founder model to a module slice rather than the full app (Open Question 7),
rather than re-stretching `expected_behavior_range`. Record the sign-off and the
re-pinned digest before U13b freezes the founder-model artifact.
