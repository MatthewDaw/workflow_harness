# agent-families — Phase 0: Library Core

A self-improving skill library for agent training (DESIGN: `docs/agent-families/DESIGN.md`).
Phase 0 is the standalone heart: atomic insights register through an embedding +
LLM-judge routing pipeline into skills owned by seeded agent families, move through a
quarantine → promote/revert → retire/revive lifecycle, render byte-stably, and export
as Claude Code `SKILL.md` files. No pipeline, explorer, grader, or reflector yet —
those are Plans 002–005.

Self-contained on purpose (the `wrapper/` precedent): Python 3.12 + uv, src layout,
zero references from the repo root tooling.

## Setup

```powershell
cd agent-families
uv sync                # installs the package + dev deps into .venv
uv run pytest          # the offline suite — green with zero Claude quota
uv run af init         # cold start: schema, taxonomy seed, thresholds.toml, model prefetch
```

`af init` (safe to re-run; a second init is a no-op):

1. writes `thresholds.toml` — the provenance-commented tunables template (the loader
   is fail-fast: unknown keys and out-of-range values are hard errors);
2. applies schema migrations to `library.db` (relational + sqlite-vec vec0 tables);
3. seeds the four DESIGN §3 families — planner, worker, verifier, context-retriever —
   each with one generic specialist agent;
4. records the embedding pins and pre-fetches `nomic-ai/nomic-embed-text-v1.5`
   (~0.5 GB on first run; honors `HF_HOME` — keep it a short path on Windows).

## The nine commands

Every command takes `--dir` (the library directory, default `.`) and exits non-zero
with an actionable message on failure.

| Command | What it does |
|---|---|
| `af init` | Cold start (above). |
| `af add-idea --precondition .. --action .. --expected-outcome .. [--scope-tag ..] [--batch ..]` | Register one idea: content-hash dedup → embed → cosine merge prefilter (judged) → ANN → placement/taxonomy judge → one atomic write, status `quarantined`. `--accept-rewrite` re-enters with a judge-proposed rewrite; `--override-retired` admits an idea whose near-duplicate is retired. |
| `af promote --batch B` | Quarantined members of B → `active` (mints one snapshot). |
| `af revert --batch B` | B's members → `retired`; closes their contradiction flags; removes batch-created skills with no surviving members. |
| `af retire --insight N \| --skill N` | Live insight(s) → `retired`. |
| `af revive --insight N \| --skill N` | Retired insight(s) → `active`. |
| `af render --skill N [--snapshot S] [--include-quarantined] [--compile]` | Concat rendering (bytes on stdout, byte-stable per (skill, snapshot, filter)); `--compile` produces the judged delta-patch document with per-section insight provenance under `<dir>/compiled/`. |
| `af export --out DIR [--skill N ...]` | Claude Code `SKILL.md` trees: slugified `name`, `description`, body = compiled doc if present else concat. Empty skills are skipped with a notice. |
| `af status` | Snapshot ID, embedder pins, taxonomy, counts by status, pending batches, open contradiction flags, empty/flagged skills, config summary. |

Lifecycle decisions stay human in Phase 0: contradictions and supersede links are
*flags* surfaced by `status`; retiring a superseded insight is an explicit `retire`.

## Judge record/replay (R23)

All LLM traffic flows through one seam, `judge.run_judge`, which shells out to
headless `claude -p` on the logged-in **subscription** (never an API key). Two env
vars control the seam:

- `AF_JUDGE_MODE` — `replay` (default) | `record` | `passthrough`
- `AF_JUDGE_FIXTURES` — fixture directory (default `tests/fixtures/judge`)

In `replay`, responses come from JSON fixtures keyed by
`sha256(sorted-json(prompt, schema, model))`; a missing fixture is a hard failure
naming the hash, and **zero subprocesses run** — this is what `uv run pytest` rides,
so the full suite passes offline with no quota and no `claude` on PATH. In `record`,
the real CLI runs and the full response envelope is written to a fixture for
deliberate, reviewed refresh. Judge prompts carry no volatile data (no timestamps,
absolute paths, or full-precision floats) so fixture keys are stable across machines.

### Manual `record` smoke procedure (not in CI)

With the claude CLI installed and logged in on the subscription:

```powershell
cd agent-families
$env:AF_JUDGE_MODE = "record"
$env:AF_JUDGE_FIXTURES = "tests/fixtures/judge"
# any real judge call works; e.g. register one idea against a scratch library:
uv run af init --dir ..\scratch-lib
uv run af add-idea --dir ..\scratch-lib --batch smoke `
  --precondition "A web target is being probed for requirements" `
  --action "Ask about role-gated admin areas during elicitation" `
  --expected-outcome "Hidden admin features surface as explicit requirements"
```

Inspect the new fixture under `tests/fixtures/judge/`, then commit it deliberately.
Unset `AF_JUDGE_MODE` afterwards — replay is the default for a reason.

## The e2e fixture chain

`tests/test_e2e.py` drives the nine commands over a curated idea set covering every
judge outcome (exact duplicate, near-duplicate merge, contradiction supersede/flag,
lint reject, rewrite-then-accept, no-placement) plus promote/render/export and the
byte-identical revert probe.

**Fixture-chain property:** placement prompts embed prior registrations — neighbor
lists, skill memberships, statuses — so the e2e fixtures form a chain in which each
fixture is a function of the library state left by the previous step. The chain
therefore re-records **as a unit** whenever a prompt template or the curated idea set
changes; never patch one mid-chain fixture by hand. The e2e test enforces this
structurally: it recomputes every prompt from the live library immediately before
recording the fixture for that step, so a template change regenerates the entire
chain on the next run.

Embeddings in tests come from injected fake encoders (`cli._embedding_service` is the
seam); integration tests that load the real model are marked `slow` and opt in via
`uv run pytest --run-slow`.

## Phase 1: the pipeline (plan 002)

`src/agent_families/pipeline/` is the plan → work → verify pipeline: a checkpointable
orchestrator (`orchestrator.py`) drives planner/worker/verifier `claude -p` sessions
(`sessions.py` — the `run_session` seam beside Phase 0's `run_judge`) through Ralph
loops over a git workspace instantiated from the pinned stack template
(`workspace.py`, `template/`, pinned in `template.lock`), with deterministic plan
lints (`planning.py`), the harness gate (`gate.py`), the per-ticket loop with
evidence-complete verdicts (`ticket_loop.py`), an orchestrator-managed dev server
(`devserver.py`), and shadow-mode tripwires (`tripwires.py`). The orchestrator is the
sole store writer; agents communicate only via validated structured output.

### Toy-spec corpus and offline e2e

`specs/` is the committed corpus (plan-002 R19); each spec carries a machine-readable
`Expected outcome` json block that `tests/test_pipeline_e2e.py` asserts against:

| Spec | Exercises | Terminal |
|---|---|---|
| `01-trivial.md` | single ticket, full run→ticket→spans→CHK trace chain | `success` |
| `02-multi-ticket.md` | DAG dependency ordering; also the kill-mid-flight resume e2e | `success` |
| `03-ambiguous.md` | planner `assumptions[]` recorded and surfaced in the run report | `success` |
| `04-escalation.md` | persistent gate failure → escalation, dependent blocked | `partial` |

The e2e suite runs the **full** pipeline offline with the scripted session fake
(zero quota, no `claude` on PATH): real MSG synthesis, plan lints, canonical plan
persistence, real gate subprocesses, real git workspaces. The resume scenario kills
a real child orchestrator process mid-run (the session script's durable cursor
survives) and a fresh orchestrator resumes to `success` without double-charging the
Ralph cap. Run assembly follows the run lifecycle: `create_run` → `run_planning`
(persists the canonical plan and traceability) → the post-planning checkpoint →
`Orchestrator.resume` executes — `run_planning` owns ticket registration, so the
orchestrator's own planning arm (which inserts fresh ticket rows itself) is for
stub-planner wiring only; assembled runs always enter via the checkpoint.

### Live smoke procedure (manual, not CI)

One real run of `specs/01-trivial.md` against the actual CLI. Prerequisites: the
claude CLI installed and logged in on the **subscription**; the template primed —
one online `npm ci` in `template/` (template maintenance, like Phase 0's model-cache
priming), then re-pin if the template changed: `uv run python -c "from
agent_families.pipeline.workspace import write_template_lock; print(write_template_lock())"`.

Save as `live_smoke.py` beside `pyproject.toml` and run `uv run python live_smoke.py`
(on Windows use `npm.cmd`/`npx.cmd` in the commands below):

```python
import json
from pathlib import Path
from agent_families.pipeline.gate import GateCommand
from agent_families.pipeline.orchestrator import Orchestrator, checkpoint_key
from agent_families.pipeline.planning import plan_report, run_planning
from agent_families.pipeline.sessions import planner_profile, verifier_profile, worker_profile
from agent_families.pipeline.ticket_loop import TicketLoop, TicketLoopConfig
from agent_families.pipeline.workspace import DEFAULT_LOCK_PATH, instantiate_workspace
from agent_families.store import Store

base = Path("../af-live-smoke"); base.mkdir(exist_ok=True)
store = Store(base / "library.db"); store.migrate()
ws = instantiate_workspace(dest=base / "ws-trivial", lock_path=DEFAULT_LOCK_PATH)
run_id = store.create_run("specs/01-trivial.md", store.current_snapshot_id())
run_planning(store, run_id, Path("specs/01-trivial.md"),
             planner_profile(model="sonnet", max_turns=8, timeout_s=600.0),
             transcript_dir=base / "transcripts", cap=3, max_retries=2,
             size_budget=6, mode="live")
doc = plan_report(store, run_id)
store.set_meta(checkpoint_key(run_id), json.dumps({
    "workspace_root": str(ws.root), "planned": True,
    "tickets": [{"id": t["id"], "depends_on": t["depends_on"]} for t in doc["tickets"]],
    "iterations_used": {}, "gate_passed": {}}, sort_keys=True))
loop = TicketLoop(TicketLoopConfig(
    worker_profile=worker_profile(model="sonnet", max_turns=40, timeout_s=1200.0,
                                  test_commands=("npm.cmd test", "npm.cmd run typecheck")),
    verifier_profile=verifier_profile(model="sonnet", max_turns=20, timeout_s=900.0,
                                      check_commands=("npm.cmd run build", "npm.cmd test")),
    gate_commands=(
        GateCommand("typecheck", ("npm.cmd", "run", "typecheck"), 300.0, "gate_typecheck"),
        GateCommand("lint", ("npm.cmd", "run", "lint"), 300.0, "gate_lint"),
        GateCommand("vitest", ("npm.cmd", "test"), 600.0, "gate_test"),
    ),
    transcript_dir=base / "transcripts", max_retries=2, mode="live"))
orch = Orchestrator(store, planner_fn=lambda c: (), worker_fn=loop.worker,
                    gate_fn=loop.gate, verifier_fn=loop.verifier, ralph_cap=4)
print(orch.resume(run_id))
print("cost_usd:", store.get_run(run_id)["total_cost_usd"])
```

A live run asserts only the terminal state (`success`) — trajectories vary; the
corpus expectations are written against the scripted fakes. Expected cost on the
sonnet profiles: very roughly $0.5–2; record the observed `total_cost_usd` under
`## Probe findings` in PROGRESS.md after running.

### The two empirical probes (plan-002 KTD Q8, pending)

1. **Quota-exhaustion envelope shape** — the June 15, 2026 billing change lands
   mid-Phase-1; detection currently classifies on the fallback markers in
   `sessions.py` (`QUOTA_SUBTYPES` / `QUOTA_TEXT_MARKERS`). When a real exhaustion
   is observed, follow the probe procedure in the `sessions.py` module docstring
   (capture the envelope into `tests/fixtures/sessions/quota-envelope.json`, extend
   the markers, record under `## Probe findings`).
2. **Windows `--allowedTools` write containment** — whether directory scoping
   actually confines a live worker's writes on Windows. Probe: run a live
   worker-profile session in a scratch workspace prompting an outside-workspace
   write, inspect the transcript for whether the tool call was permitted. Either
   way the post-iteration containment assert (R8, `workspace.assert_containment`)
   is the enforced guard; record findings under `## Probe findings`.

## Layout

```text
agent-families/
├── thresholds.toml          # tunables template, provenance-commented (written by init)
├── template/                # pinned output-stack template (primed via one online npm ci)
├── template.lock            # the template's pinned content hash (plan-002 R18)
├── specs/                   # toy-spec corpus with expected-outcome blocks (plan-002 R19)
├── src/agent_families/
│   ├── cli.py               # the nine commands
│   ├── config.py            # fail-fast TOML loader
│   ├── store.py             # schema, transactions, snapshots, promotion queue
│   ├── embedding.py         # pinned local embedder + prefix discipline
│   ├── vecindex.py          # sqlite-vec vec0 KNN with status-visibility joins
│   ├── judge.py             # the claude -p seam with record/replay
│   ├── pipeline.py          # add_idea decision spine
│   ├── lifecycle.py         # promote/revert/retire/revive (queue-serialized)
│   ├── rendering.py         # byte-stable concat + judged delta-patch compile
│   ├── export.py            # SKILL.md export
│   └── pipeline/            # Phase 1: orchestrator, sessions, planning, ticket loop,
│                            #   gate, devserver, workspace, tripwires (plan 002)
└── tests/                   # offline suite; fixtures under tests/fixtures/judge/
```
