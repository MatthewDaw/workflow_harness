"""plan-002 U7: trace capture, shadow tripwires, accounting (R16/R17).

Fully offline: the embedding service runs with an injected scripted encoder
(the Phase 0 ``EmbeddingService(config, encoder=...)`` seam) that also asserts
the ``search_document:`` prefix discipline, and the orchestrator-driven tests
use scripted stub stages exactly like U4's suite. No model download, no
``claude`` on PATH, zero quota.

## Conformance (plan-002 U7 test scenarios -> tests, 1:1)

- two identical scripted iterations log similarity ~1.0 and
  ``would_have_fired: true`` (shadow only — loop continues):
  ``test_identical_iterations_log_similarity_one_and_would_have_fired`` (the
  direct observation) and
  ``test_shadow_mode_never_kills_loop_completes_despite_fired_flags`` (the
  loop continuing through fired flags).
- distinct iterations log lower similarity:
  ``test_distinct_iterations_log_lower_similarity_and_do_not_fire``
- failure-set hash stable under failure-record reordering:
  ``test_failure_set_hash_stable_under_reordering``
- tripwire events carry model+dim:
  ``test_tripwire_events_carry_embedder_model_and_dim``
- run settlement totals equal the sum of span costs:
  ``test_run_settlement_totals_equal_sum_of_span_costs``
- nothing is ever killed by a detector in Phase 1 (loop completion despite
  fired flags):
  ``test_shadow_mode_never_kills_loop_completes_despite_fired_flags``

Unit Verification — "a fake run produces a queryable tripwire dataset with
both detectors represented" — is
``test_shadow_mode_never_kills_loop_completes_despite_fired_flags``, which
queries ``tripwire_events`` for both detector kinds after a settled fake run.

Supporting invariants beyond the named scenarios: NULL-similarity baseline on
a ticket's first iteration, per-ticket detector state, the empty-failure-set
rule for no-progress, threshold range validation, and the per-ticket
accounting breakdown summing to the run totals.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from agent_families.config import EmbeddingConfig
from agent_families.embedding import DOCUMENT_PREFIX, EmbeddingService
from agent_families.pipeline.orchestrator import (
    GateResult,
    Orchestrator,
    TicketSpec,
    VerifierResult,
)
from agent_families.pipeline.tripwires import (
    DETECTOR_DIFF_SIMILARITY,
    DETECTOR_NO_PROGRESS,
    ShadowTripwires,
    TripwireError,
    canonical_failure_set_hash,
    run_totals,
    ticket_totals,
)
from agent_families.pipeline.workspace import Workspace
from agent_families.store import Store

# --- scaffolding -----------------------------------------------------------------

FAKE_MODEL = "fake-nomic-pin"
DIM = 4

# Orthogonal scripted vectors: identical summaries -> cosine 1.0, distinct
# summaries -> cosine 0.0; "near" mixes axes for a mid-range similarity.
VECTORS = {
    "edited src/index.ts": [1.0, 0.0, 0.0, 0.0],
    "edited src/router.ts": [0.0, 1.0, 0.0, 0.0],
    "edited src/index.ts and src/util.ts": [0.8, 0.6, 0.0, 0.0],
}


class ScriptedEncoder:
    """Deterministic text->vector table that enforces the prefix discipline."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = dict(table)

    def encode(self, text: str) -> list[float]:
        assert text.startswith(DOCUMENT_PREFIX), (
            "diff summaries are indexing-side content and must be embedded"
            " with the search_document: prefix (plan-002 U7 approach)"
        )
        return self.table[text[len(DOCUMENT_PREFIX):]]


def make_store(base: Path) -> Store:
    base.mkdir(parents=True, exist_ok=True)
    store = Store(base / "library.db")
    store.migrate()
    return store


def make_embedder(table: dict[str, list[float]] = VECTORS) -> EmbeddingService:
    config = EmbeddingConfig(model=FAKE_MODEL, dim=DIM, device="cpu")
    return EmbeddingService(config, encoder=ScriptedEncoder(table))


def make_tripwires(store: Store, *, threshold: float = 0.9) -> ShadowTripwires:
    return ShadowTripwires(
        store, make_embedder(), similarity_threshold=threshold
    )


def make_run(store: Store, spec_ref: str = "specs/fake.md") -> int:
    return store.create_run(spec_ref, store.current_snapshot_id())


def make_ws(root: Path) -> Workspace:
    """A small real git repo standing in for a U2-instantiated workspace."""
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "index.ts").write_text(
        "export const answer = 42\n", encoding="utf-8", newline="\n"
    )
    for args in (
        ("init", "--initial-branch=main"),
        ("config", "user.name", "t"),
        ("config", "user.email", "t@localhost"),
        ("config", "commit.gpgsign", "false"),
        ("config", "core.autocrlf", "false"),
        ("add", "-A"),
        ("commit", "-m", "init"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        )
    return Workspace(root=root)


def finalized_span(
    ctx, *, cost=0.05, inp=100, out=50, turns=2, dur=1500, cost_partial=False
) -> str:
    """Simulate one completed session: span registered running, finalized with
    costs (what run_session does for real, U3)."""
    span_id = f"SPAN-{uuid.uuid4().hex}"
    ctx.store.insert_span(
        span_id,
        run_id=ctx.run_id,
        ticket_id=ctx.ticket_id,
        ralph_iteration=ctx.ralph_iteration,
        agent="worker",
    )
    ctx.store.finalize_span(
        span_id,
        "completed",
        num_turns=turns,
        duration_ms=dur,
        cost_usd=cost,
        input_tokens=inp,
        output_tokens=out,
        cost_partial=cost_partial,
    )
    return span_id


def events(store: Store, run_id: int, kind: str) -> list:
    return store.conn.execute(
        "SELECT * FROM tripwire_events WHERE run_id = ? AND detector_kind = ?"
        " ORDER BY ralph_iteration, id",
        (run_id, kind),
    ).fetchall()


# Typed failure records in their R13 shape (dicts here; sqlite3.Row also works).
F_TSC = {
    "failure_kind": "gate_typecheck",
    "location": "src/index.ts:3",
    "expected": "tsc exits 0",
    "observed": "TS2322: type mismatch",
    "repro_command": "npm run typecheck",
}
F_TEST = {
    "failure_kind": "gate_test",
    "location": "src/util.test.ts",
    "expected": "vitest exits 0",
    "observed": "1 failed",
    "repro_command": "npx vitest run",
}


# --- diff-similarity detector (R17) -------------------------------------------------


def test_identical_iterations_log_similarity_one_and_would_have_fired(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    trip = make_tripwires(store, threshold=0.9)
    first = trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        diff_summary="edited src/index.ts",
    )
    # Shadow mode: observe returns data and raises nothing — the second,
    # identical iteration fires the flag and the caller keeps looping.
    second = trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=2,
        diff_summary="edited src/index.ts",
    )
    assert second.similarity == pytest.approx(1.0)
    assert second.diff_would_have_fired is True
    assert first.similarity is None  # baseline, see dedicated test below
    rows = events(store, run_id, DETECTOR_DIFF_SIMILARITY)
    assert len(rows) == 2
    assert rows[1]["similarity"] == pytest.approx(1.0)
    assert rows[1]["would_have_fired"] == 1


def test_distinct_iterations_log_lower_similarity_and_do_not_fire(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    trip = make_tripwires(store, threshold=0.9)
    trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        diff_summary="edited src/index.ts",
    )
    obs = trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=2,
        diff_summary="edited src/router.ts",
    )
    assert obs.similarity == pytest.approx(0.0)
    assert obs.similarity < 0.9
    assert obs.diff_would_have_fired is False
    # mid-range similarity stays below the threshold too: 0.6 < 0.9
    near = trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=3,
        diff_summary="edited src/index.ts and src/util.ts",
    )
    assert near.similarity == pytest.approx(0.6)  # vs router.ts
    assert near.diff_would_have_fired is False
    rows = events(store, run_id, DETECTOR_DIFF_SIMILARITY)
    assert [r["would_have_fired"] for r in rows] == [0, 0, 0]


def test_first_iteration_logs_null_similarity_baseline(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    trip = make_tripwires(store)
    obs = trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        diff_summary="edited src/index.ts",
    )
    assert obs.similarity is None
    assert obs.diff_would_have_fired is False
    row = events(store, run_id, DETECTOR_DIFF_SIMILARITY)[0]
    assert row["similarity"] is None
    assert row["would_have_fired"] == 0


def test_detector_state_is_keyed_per_ticket(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    trip = make_tripwires(store)
    trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        diff_summary="edited src/index.ts",
    )
    # Same summary on a DIFFERENT ticket is that ticket's baseline, not a repeat.
    other = trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-B", ralph_iteration=1,
        diff_summary="edited src/index.ts",
    )
    assert other.similarity is None
    assert other.diff_would_have_fired is False


def test_threshold_out_of_range_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(TripwireError, match=r"\[0\.0, 1\.0\]"):
        ShadowTripwires(store, make_embedder(), similarity_threshold=1.5)
    with pytest.raises(TripwireError, match=r"\[0\.0, 1\.0\]"):
        ShadowTripwires(store, make_embedder(), similarity_threshold=-0.1)


# --- no-progress detector: canonical failure-set hashing (R17) ----------------------


def test_failure_set_hash_stable_under_reordering():
    forward = canonical_failure_set_hash([F_TSC, F_TEST])
    reordered = canonical_failure_set_hash([F_TEST, F_TSC])
    assert forward == reordered
    # ...and sensitive to the set actually changing
    assert canonical_failure_set_hash([F_TSC]) != forward
    changed = dict(F_TSC, observed="TS2741: missing property")
    assert canonical_failure_set_hash([changed, F_TEST]) != forward


def test_no_progress_fires_on_repeated_identical_failure_set(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    trip = make_tripwires(store)
    first = trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        diff_summary="edited src/index.ts", failures=[F_TSC, F_TEST],
    )
    assert first.no_progress_would_have_fired is False  # baseline
    # The same failures, reordered: canonically identical -> fires (shadow).
    repeat = trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=2,
        diff_summary="edited src/router.ts", failures=[F_TEST, F_TSC],
    )
    assert repeat.no_progress_would_have_fired is True
    assert repeat.failure_set_hash == first.failure_set_hash
    # Progress (one failure cleared) -> does not fire.
    progressed = trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=3,
        diff_summary="edited src/index.ts", failures=[F_TEST],
    )
    assert progressed.no_progress_would_have_fired is False
    rows = events(store, run_id, DETECTOR_NO_PROGRESS)
    assert [r["would_have_fired"] for r in rows] == [0, 1, 0]
    # The canonical hash is logged on every iteration's event (R17).
    assert rows[0]["failure_set_hash"] == first.failure_set_hash
    assert all(r["failure_set_hash"] for r in rows)


def test_empty_failure_set_never_fires_no_progress(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    trip = make_tripwires(store)
    for iteration in (1, 2):
        obs = trip.observe_iteration(
            run_id=run_id, ticket_id="TKT-A", ralph_iteration=iteration,
            diff_summary="edited src/index.ts", failures=[],
        )
        # Two consecutive empty sets repeat exactly, but no failures IS
        # progress — the detector must not flag a healthy loop.
        assert obs.no_progress_would_have_fired is False


# --- event provenance (R17: model+dim per event) -------------------------------------


def test_tripwire_events_carry_embedder_model_and_dim(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    trip = make_tripwires(store)
    trip.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        diff_summary="edited src/index.ts", failures=[F_TSC],
        span_id=None,
    )
    for row in events(store, run_id, DETECTOR_DIFF_SIMILARITY):
        assert row["embedding_model"] == FAKE_MODEL
        assert row["embedding_dim"] == DIM


# --- accounting (R16) ------------------------------------------------------------------


def test_run_settlement_totals_equal_sum_of_span_costs(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    tickets = (TicketSpec("TKT-A"), TicketSpec("TKT-B", ("TKT-A",)))

    def worker(ctx):
        # TKT-B's session is "killed": its usage was accumulated best-effort
        # from the captured stream and flagged cost_partial (R16).
        finalized_span(ctx, cost_partial=(ctx.ticket_id == "TKT-B"))

    orch = Orchestrator(
        store,
        planner_fn=lambda ctx: list(tickets),
        worker_fn=worker,
        gate_fn=lambda ctx: GateResult(passed=True),
        verifier_fn=lambda ctx: VerifierResult(passed=True),
        ralph_cap=3,
    )
    result = orch.run("specs/two.md", ws)
    assert result.status == "success"

    spans = store.conn.execute(
        "SELECT * FROM trace_span WHERE run_id = ?", (result.run_id,)
    ).fetchall()
    assert len(spans) == 2
    run_row = store.get_run(result.run_id)
    # Settlement wrote exactly the sum of span costs into the run row —
    # including the cost_partial span, whose count is reported (R16).
    assert run_row["total_cost_usd"] == pytest.approx(
        sum(s["cost_usd"] for s in spans)
    ) == pytest.approx(0.10)
    assert run_row["total_input_tokens"] == sum(s["input_tokens"] for s in spans)
    assert run_row["total_output_tokens"] == sum(s["output_tokens"] for s in spans)
    assert run_row["total_turns"] == sum(s["num_turns"] for s in spans)
    assert run_row["total_duration_ms"] == sum(s["duration_ms"] for s in spans)
    assert run_row["cost_partial_spans"] == 1

    # The aggregation helpers agree with the settled row (per run)...
    totals = run_totals(store, result.run_id)
    assert totals.cost_usd == pytest.approx(run_row["total_cost_usd"])
    assert totals.input_tokens == run_row["total_input_tokens"]
    assert totals.output_tokens == run_row["total_output_tokens"]
    assert totals.num_turns == run_row["total_turns"]
    assert totals.duration_ms == run_row["total_duration_ms"]
    assert totals.cost_partial_spans == run_row["cost_partial_spans"]
    # ...and per ticket the breakdown sums back to the run (R16 "per ticket").
    per_ticket = ticket_totals(store, result.run_id)
    assert set(per_ticket) == {"TKT-A", "TKT-B"}
    assert sum(t.cost_usd for t in per_ticket.values()) == pytest.approx(
        totals.cost_usd
    )
    assert per_ticket["TKT-A"].cost_partial_spans == 0
    assert per_ticket["TKT-B"].cost_partial_spans == 1


# --- shadow discipline: nothing kills, dataset is the deliverable (R17) ---------------


def test_shadow_mode_never_kills_loop_completes_despite_fired_flags(tmp_path):
    """A fake run whose every iteration repeats the same diff AND the same
    failures: both detectors fire from iteration 2 on, and the loop still runs
    to settlement — the only output is the queryable threshold-setting dataset
    with both detectors represented (unit Verification)."""
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    trip = make_tripwires(store, threshold=0.9)
    verdicts = iter([False, False, True])  # verifier passes on iteration 3

    def worker(ctx):
        finalized_span(ctx)

    def gate(ctx):
        # U6 wires this for real; here the stub gate observes exactly what the
        # orchestrator will: an orchestrator-derived diff summary plus the
        # iteration's typed failures.
        trip.observe_iteration(
            run_id=ctx.run_id,
            ticket_id=ctx.ticket_id,
            ralph_iteration=ctx.ralph_iteration,
            diff_summary="edited src/index.ts",
            failures=[F_TSC, F_TEST],
        )
        return GateResult(passed=True)

    orch = Orchestrator(
        store,
        planner_fn=lambda ctx: [TicketSpec("TKT-A")],
        worker_fn=worker,
        gate_fn=gate,
        verifier_fn=lambda ctx: VerifierResult(passed=next(verdicts)),
        ralph_cap=5,
    )
    result = orch.run("specs/stall.md", ws)

    # Nothing was killed: the ticket completed and the run settled normally
    # even though detectors "would have fired" on iterations 2 and 3.
    assert result.status == "success"
    assert result.ticket_statuses == {"TKT-A": "done"}
    diff_rows = events(store, result.run_id, DETECTOR_DIFF_SIMILARITY)
    stall_rows = events(store, result.run_id, DETECTOR_NO_PROGRESS)
    assert [r["would_have_fired"] for r in diff_rows] == [0, 1, 1]
    assert [r["would_have_fired"] for r in stall_rows] == [0, 1, 1]
    # Both detectors are represented in the queryable dataset, with their
    # respective provenance fields populated.
    assert all(r["embedding_model"] == FAKE_MODEL for r in diff_rows)
    assert all(r["embedding_dim"] == DIM for r in diff_rows)
    assert all(r["failure_set_hash"] for r in stall_rows)
    assert len({r["failure_set_hash"] for r in stall_rows}) == 1
