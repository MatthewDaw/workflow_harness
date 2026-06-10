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

## Layout

```text
agent-families/
├── thresholds.toml          # tunables template, provenance-commented (written by init)
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
│   └── export.py            # SKILL.md export
└── tests/                   # offline suite; fixtures under tests/fixtures/judge/
```
