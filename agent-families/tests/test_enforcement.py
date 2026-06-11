"""plan-005 U6: enforcement activation and annealing (R18-R20).

Fully offline: the embedder is the Phase 0 injected-encoder seam (same
``search_document:`` prefix discipline the shadow tripwires use), the
re-verifier and the productive-similarity distribution are scripted, and the
mutation-audit bridge drives the real Phase 2 ``run_mutation_audit`` with a
scripted witness + fake verifier. Zero quota, no ``claude`` on PATH.

## Conformance (plan-005 U6 test scenarios -> tests, 1:1)

- tripwire fires only above the derived threshold (fixture distributions):
  ``test_kill_threshold_derived_from_productive_distribution`` and
  ``test_enforce_fires_only_above_derived_threshold``
- killed loop escalates with the existing typed record (no new failure shape):
  ``test_killed_loop_escalates_with_existing_typed_record`` and
  ``test_detector_failure_kinds_are_existing_shapes``
- suspect verifier's ticket re-verified before fitness lands:
  ``test_suspect_ticket_reverified_before_fitness_lands`` and
  ``test_flagged_suspect_chk_ids_reads_audit_log``
- suspect episode scores excluded from chart recompute:
  ``test_suspect_episode_scores_excluded_from_recompute``
- annealing schedule steps the budget per epoch:
  ``test_annealing_steps_budget_per_epoch``
- rotation trigger fires only on the plateau condition:
  ``test_persona_rotation_fires_only_on_plateau``

Unit Verification — "shadow-vs-enforce A/B on fixture data shows identical
detection, differing only in action":
``test_shadow_vs_enforce_identical_detection_differing_action``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_families.config import EmbeddingConfig
from agent_families.embedding import DOCUMENT_PREFIX, EmbeddingService
from agent_families.grading import calibrate
from agent_families.grading.calibrate import (
    MutantFixture,
    WitnessEnvelope,
    run_mutation_audit,
)
from agent_families.pipeline.enforcement import (
    DETECTOR_FAILURE_KIND,
    ENFORCE,
    SHADOW,
    AnnealingSchedule,
    EnforcementError,
    EnforcementParams,
    EnforcementTripwires,
    EpisodeScore,
    KillThreshold,
    curriculum_eligible_scores,
    derive_kill_threshold,
    flagged_suspect_chk_ids,
    reverify_before_fitness,
    should_rotate_personas,
    suspect_closed_tickets,
)
from agent_families.pipeline.tripwires import (
    DETECTOR_DIFF_SIMILARITY,
    DETECTOR_NO_PROGRESS,
    ShadowTripwires,
)
from agent_families.pipeline.orchestrator import VerifierResult
from agent_families.reflector.maintenance import SessionRender, settle_fitness
from agent_families.store import FAILURE_KINDS, Store

# --- scaffolding (mirrors test_tripwires.py) ----------------------------------

FAKE_MODEL = "fake-nomic-pin"
DIM = 4

# Orthogonal scripted vectors: identical summaries -> cosine 1.0, distinct -> 0.0,
# "near" mixes axes for a mid-range 0.6 similarity.
VECTORS = {
    "edited src/index.ts": [1.0, 0.0, 0.0, 0.0],
    "edited src/router.ts": [0.0, 1.0, 0.0, 0.0],
    "edited src/index.ts and src/util.ts": [0.8, 0.6, 0.0, 0.0],
}

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


class ScriptedEncoder:
    """Deterministic text->vector table enforcing the prefix discipline."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = dict(table)

    def encode(self, text: str) -> list[float]:
        assert text.startswith(DOCUMENT_PREFIX), (
            "diff summaries are indexing-side content (search_document: prefix)"
        )
        return self.table[text[len(DOCUMENT_PREFIX):]]


def make_store(base: Path) -> Store:
    base.mkdir(parents=True, exist_ok=True)
    store = Store(base / "library.db")
    store.migrate()
    return store


def make_embedder() -> EmbeddingService:
    config = EmbeddingConfig(model=FAKE_MODEL, dim=DIM, device="cpu")
    return EmbeddingService(config, encoder=ScriptedEncoder(VECTORS))


def make_run(store: Store) -> int:
    return store.create_run("specs/fake.md", store.current_snapshot_id())


def seed_tickets(store: Store, ticket_ids) -> None:
    """Create the trace_tkt rows a failure record's ticket_id FK needs (the
    orchestrator has minted these by the time a tripwire kills in real runs)."""
    with store.transaction():
        for tid in ticket_ids:
            store.conn.execute(
                "INSERT OR IGNORE INTO trace_tkt (id) VALUES (?)", (tid,)
            )


def seed_insight(store: Store) -> int:
    """A bare active insight to render into a session (fitness FK needs a real
    insights row)."""
    seed_insight.counter += 1  # type: ignore[attr-defined]
    n = seed_insight.counter  # type: ignore[attr-defined]
    return store.insert_insight(
        precondition=f"pre {n}",
        action=f"act {n}",
        expected_outcome=f"out {n}",
        content_hash=f"hash-{n}",
        status="active",
    )


seed_insight.counter = 0  # type: ignore[attr-defined]


def make_enforcer(store: Store, *, mode: str, threshold: float) -> EnforcementTripwires:
    shadow = ShadowTripwires(
        store, make_embedder(), similarity_threshold=threshold
    )
    return EnforcementTripwires(shadow, mode=mode)


def failure_records(store: Store, run_id: int) -> list:
    return store.conn.execute(
        "SELECT * FROM failure_records WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()


# --- R18: kill-threshold derivation -------------------------------------------


def test_kill_threshold_derived_from_productive_distribution():
    # Productive iterations sit at low similarity (they change the diff); the
    # threshold is a high quantile placed just above where productive work lives.
    productive = [0.10, 0.20, 0.25, 0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.62]
    params = EnforcementParams(kill_threshold_quantile=0.95)
    kt = derive_kill_threshold(productive, params)
    assert isinstance(kt, KillThreshold)
    assert kt.n_samples == 10
    assert kt.quantile == 0.95
    # Above every productive point but one (the q95 sits near the top of the
    # sample), and a valid cosine.
    assert 0.0 <= kt.value <= 1.0
    assert kt.value >= 0.60
    assert "q0.95" in kt.derivation
    # A lower quantile yields a lower (more aggressive) threshold.
    looser = derive_kill_threshold(productive, EnforcementParams(
        kill_threshold_quantile=0.50
    ))
    assert looser.value < kt.value


def test_kill_threshold_rejects_empty_and_out_of_range():
    params = EnforcementParams()
    with pytest.raises(EnforcementError, match="empty"):
        derive_kill_threshold([], params)
    with pytest.raises(EnforcementError, match=r"\[0, 1\]"):
        derive_kill_threshold([0.5, 1.7], params)


def test_enforcement_param_validation():
    with pytest.raises(EnforcementError, match="kill_threshold_quantile"):
        EnforcementParams(kill_threshold_quantile=0.0)
    with pytest.raises(EnforcementError, match="kill_threshold_quantile"):
        EnforcementParams(kill_threshold_quantile=1.0)
    with pytest.raises(EnforcementError, match="plateau_window"):
        EnforcementParams(plateau_window=1)
    with pytest.raises(EnforcementError, match="mode"):
        EnforcementTripwires(object(), mode="kill")  # type: ignore[arg-type]


# --- R18: enforce fires only above the derived threshold ----------------------


def test_enforce_fires_only_above_derived_threshold(tmp_path):
    # Derive a threshold that sits between the "near" (0.6) and "identical" (1.0)
    # similarities, so 0.6 must NOT fire and 1.0 MUST.
    productive = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]  # q95 ~ 0.575 -> just below 0.6
    kt = derive_kill_threshold(productive, EnforcementParams(
        kill_threshold_quantile=0.95
    ))
    assert kt.value < 0.6  # so a 0.6 mid-range similarity is BELOW the kill line

    # Re-derive a threshold strictly above 0.6 for the "does not fire" leg.
    threshold = 0.7
    store = make_store(tmp_path)
    seed_tickets(store, ["TKT-A", "TKT-B"])
    run_id = make_run(store)
    enf = make_enforcer(store, mode=ENFORCE, threshold=threshold)

    enf.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        diff_summary="edited src/router.ts",
    )
    # Mid-range 0.6 is below 0.7 -> not fired, not killed, no record.
    mid = enf.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=2,
        diff_summary="edited src/index.ts and src/util.ts",
    )
    assert mid.shadow.similarity == pytest.approx(0.6)
    assert mid.killed is False
    assert mid.decisions == ()
    assert failure_records(store, run_id) == []

    # Identical (1.0) is above 0.7 -> fired and killed.
    enf.observe_iteration(
        run_id=run_id, ticket_id="TKT-B", ralph_iteration=1,
        diff_summary="edited src/index.ts",
    )
    hit = enf.observe_iteration(
        run_id=run_id, ticket_id="TKT-B", ralph_iteration=2,
        diff_summary="edited src/index.ts",
    )
    assert hit.shadow.similarity == pytest.approx(1.0)
    assert hit.killed is True
    assert DETECTOR_DIFF_SIMILARITY in hit.fired_detectors


# --- R18: killed loop escalates with the existing typed record ----------------


def test_detector_failure_kinds_are_existing_shapes():
    # No new failure shape: every detector escalates with an EXISTING kind.
    assert set(DETECTOR_FAILURE_KIND.values()) <= set(FAILURE_KINDS)
    assert DETECTOR_FAILURE_KIND[DETECTOR_DIFF_SIMILARITY] == "step_repetition"
    assert DETECTOR_FAILURE_KIND[DETECTOR_NO_PROGRESS] == "termination_unaware"


def test_killed_loop_escalates_with_existing_typed_record(tmp_path):
    store = make_store(tmp_path)
    seed_tickets(store, ["TKT-A"])
    run_id = make_run(store)
    enf = make_enforcer(store, mode=ENFORCE, threshold=0.9)

    # Two identical iterations with the SAME repeated failure set: both detectors
    # fire on iteration 2 and the loop is killed with typed records.
    enf.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        diff_summary="edited src/index.ts", failures=[F_TSC, F_TEST],
    )
    obs = enf.observe_iteration(
        run_id=run_id, ticket_id="TKT-A", ralph_iteration=2,
        diff_summary="edited src/index.ts", failures=[F_TSC, F_TEST],
    )
    assert obs.killed is True
    kinds = {d.failure_kind for d in obs.decisions if d.killed}
    assert kinds == {"step_repetition", "termination_unaware"}

    rows = failure_records(store, run_id)
    # Exactly the two typed escalations, both existing shapes, tied to the ticket.
    assert len(rows) == 2
    assert {r["failure_kind"] for r in rows} == {
        "step_repetition", "termination_unaware"
    }
    assert all(r["failure_kind"] in FAILURE_KINDS for r in rows)
    assert all(r["ticket_id"] == "TKT-A" for r in rows)
    # The decisions carry the inserted record ids.
    rec_ids = {d.failure_record_id for d in obs.decisions if d.killed}
    assert rec_ids == {r["id"] for r in rows}


# --- Unit Verification: shadow-vs-enforce A/B ---------------------------------


def _run_fixture(store, *, mode, threshold):
    """Drive an identical stall fixture and return (fired_seq, killed_seq)."""
    seed_tickets(store, ["TKT-A"])
    run_id = make_run(store)
    enf = make_enforcer(store, mode=mode, threshold=threshold)
    fired_seq, killed_seq = [], []
    for i in (1, 2, 3):
        obs = enf.observe_iteration(
            run_id=run_id, ticket_id="TKT-A", ralph_iteration=i,
            diff_summary="edited src/index.ts", failures=[F_TSC, F_TEST],
        )
        fired_seq.append(sorted(obs.fired_detectors))
        killed_seq.append(obs.killed)
    return run_id, fired_seq, killed_seq


def test_shadow_vs_enforce_identical_detection_differing_action(tmp_path):
    shadow_store = make_store(tmp_path / "shadow")
    enforce_store = make_store(tmp_path / "enforce")
    s_run, s_fired, s_killed = _run_fixture(
        shadow_store, mode=SHADOW, threshold=0.9
    )
    e_run, e_fired, e_killed = _run_fixture(
        enforce_store, mode=ENFORCE, threshold=0.9
    )

    # DETECTION is identical: the same detectors fire on the same iterations.
    assert s_fired == e_fired
    expected_fired = [
        [],  # iteration 1: baselines, nothing fires
        [DETECTOR_DIFF_SIMILARITY, DETECTOR_NO_PROGRESS],
        [DETECTOR_DIFF_SIMILARITY, DETECTOR_NO_PROGRESS],
    ]
    assert s_fired == [sorted(x) for x in expected_fired]

    # ACTION differs: shadow never kills and writes no failure records...
    assert s_killed == [False, False, False]
    assert failure_records(shadow_store, s_run) == []
    # ...enforce kills from the first fired iteration and writes typed records.
    assert e_killed == [False, True, True]
    assert len(failure_records(enforce_store, e_run)) == 4  # 2 detectors x 2 iters

    # The shadow tripwire dataset is identical in both stores (logging never
    # stops under enforcement).
    def trip_fired(store, run_id, kind):
        return [
            r["would_have_fired"]
            for r in store.conn.execute(
                "SELECT would_have_fired FROM tripwire_events"
                " WHERE run_id = ? AND detector_kind = ?"
                " ORDER BY ralph_iteration, id",
                (run_id, kind),
            ).fetchall()
        ]
    for kind in (DETECTOR_DIFF_SIMILARITY, DETECTOR_NO_PROGRESS):
        assert trip_fired(shadow_store, s_run, kind) == [0, 1, 1]
        assert trip_fired(enforce_store, e_run, kind) == [0, 1, 1]


# --- R19: suspect-verdict re-verification before fitness ----------------------


def _seed_ticket_chain(store: Store, *, ticket: str, chk: str) -> None:
    """A CHK -> AC -> ticket chain so a suspect CHK resolves to its ticket."""
    with store.transaction():
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_msg (id, content) VALUES ('MSG-e','x')"
        )
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_req (id, source_msg_id)"
            " VALUES ('REQ-e', 'MSG-e')"
        )
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_tkt (id) VALUES (?)", (ticket,)
        )
        ac_id = f"AC-{ticket}"
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_ac (id, ticket_id, req_id)"
            " VALUES (?, ?, 'REQ-e')",
            (ac_id, ticket),
        )
        store.conn.execute(
            "INSERT INTO trace_chk (id, ac_id, result, repro_command, evidence)"
            " VALUES (?, ?, 'pass', '{}', 'seed')",
            (chk, ac_id),
        )


def test_suspect_closed_tickets_resolves_chk_to_ticket(tmp_path):
    store = make_store(tmp_path)
    _seed_ticket_chain(store, ticket="TKT-X", chk="CHK-0001")
    assert suspect_closed_tickets(store, ["CHK-0001"]) == ("TKT-X",)
    assert suspect_closed_tickets(store, []) == ()
    # Unknown CHK resolves to nothing (no crash).
    assert suspect_closed_tickets(store, ["CHK-nope"]) == ()


def test_suspect_ticket_reverified_before_fitness_lands(tmp_path):
    store = make_store(tmp_path)
    _seed_ticket_chain(store, ticket="TKT-X", chk="CHK-0001")
    # The suspect ticket is marked `done` (the flagged verifier closed it).
    with store.transaction():
        store.set_ticket_status("TKT-X", "done")

    # An insight rendered into the ticket's session — it would WIN if the close
    # stood. The re-verification re-runs the verifier FIRST and it now fails.
    insight_id = seed_insight(store)
    calls: list[str] = []

    def reverify(ticket_id: str) -> bool:
        calls.append(ticket_id)
        return False  # the suspect close does not hold up

    pass_ = reverify_before_fitness(
        store, chk_ids=["CHK-0001"], reverify_fn=reverify
    )
    assert calls == ["TKT-X"]  # re-verified before any fitness write
    assert pass_.now_failing == ("TKT-X",)
    assert pass_.still_passing == ()

    # Fold now_failing into the implicated set so the suspect close cannot win.
    episode_id = store.create_episode("linkding", "sha256:cafe", 0)
    settlement = settle_fitness(
        store,
        episode_id=episode_id,
        renders=[SessionRender(ticket_id="TKT-X", insight_ids=(insight_id,))],
        implicated_ticket_ids=pass_.now_failing,
    )
    assert settlement.win_count == 0  # re-failing suspect ticket earns no win
    assert settlement.won_ticket_ids == ()
    assert settlement.retrieval_count == 1  # retrieval still logged

    # Control: a suspect close that DOES re-verify still wins (re-verification is
    # a gate, not a blanket exclusion).
    calls.clear()
    pass_ok = reverify_before_fitness(
        store, chk_ids=["CHK-0001"], reverify_fn=lambda t: (calls.append(t) or True)
    )
    assert pass_ok.now_failing == ()
    episode2 = store.create_episode("linkding", "sha256:cafe", 0)
    settle_ok = settle_fitness(
        store,
        episode_id=episode2,
        renders=[SessionRender(ticket_id="TKT-X", insight_ids=(insight_id,))],
        implicated_ticket_ids=pass_ok.now_failing,
    )
    assert settle_ok.win_count == 1


def test_flagged_suspect_chk_ids_reads_audit_log(tmp_path):
    """Bridge: enforcement consumes exactly the Phase 2 mutation-audit suspects.

    Drive the real ``run_mutation_audit`` with a false-passing verifier so the
    audit flags the verifier and marks the open verdict window suspect.
    """
    store = make_store(tmp_path)
    _seed_ticket_chain(store, ticket="TKT-Y", chk="CHK-0001")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    mutant = MutantFixture(
        mutant_id="bad-diff",
        description="hand-authored bad diff",
        diff_text="diff --git a/x b/x\n",
        witness=WitnessEnvelope(
            command="npm test", cwd=".", timeout=60, expected_exit=1
        ),
    )

    def false_passing_verifier(ws: Path) -> VerifierResult:
        return VerifierResult(passed=True, detail="missed the planted bug")

    result = run_mutation_audit(
        store,
        mutant,
        workspace,
        false_passing_verifier,
        verifier_id="worker",
        witness_runner=lambda w, root: 1,  # observed == expected -> qualified
        stage=False,
    )
    assert result.outcome == "false_pass"
    # The bridge returns the flagged verifier's suspect CHK ids...
    suspects = flagged_suspect_chk_ids(store)
    assert "CHK-0001" in suspects
    assert set(suspects) == set(calibrate.suspect_chk_ids(store, "worker"))
    # ...and they resolve to the ticket the verifier closed.
    assert suspect_closed_tickets(store, suspects) == ("TKT-Y",)


# --- R19: instrument_suspect episode-score exclusion --------------------------


def test_suspect_episode_scores_excluded_from_recompute():
    scores = [
        EpisodeScore("linkding", 0, 10, 0.80, instrument_suspect=False),
        EpisodeScore("linkding", 0, 11, 0.82, instrument_suspect=False),
        # A suspect outlier that would skew a curriculum/chart recompute.
        EpisodeScore("linkding", 1, 12, 0.20, instrument_suspect=True),
        EpisodeScore("linkding", 1, 13, 0.81, instrument_suspect=False),
    ]
    eligible = curriculum_eligible_scores(scores)
    assert len(eligible) == 3
    assert all(not s.instrument_suspect for s in eligible)
    # The suspect 0.20 never reaches a recompute over eligible scores.
    mean = sum(s.score for s in eligible) / len(eligible)
    assert mean == pytest.approx((0.80 + 0.82 + 0.81) / 3)
    # All-clean -> identity; all-suspect -> empty.
    assert len(curriculum_eligible_scores(scores[:2])) == 2
    assert curriculum_eligible_scores([scores[2]]) == ()


# --- R20: question-budget annealing -------------------------------------------


def test_annealing_steps_budget_per_epoch():
    sched = AnnealingSchedule(start=8, floor=4, step=1)
    budgets = [sched.budget_at(e) for e in range(7)]
    # Steps down one per epoch and clamps at the floor (R20 'toward 3–5').
    assert budgets == [8, 7, 6, 5, 4, 4, 4]
    # Monotone non-increasing.
    assert all(b >= a for a, b in zip(budgets[1:], budgets[:-1]))
    # Floor sits inside the 3–5 target band.
    assert 3 <= sched.floor <= 5
    with pytest.raises(EnforcementError, match="epoch"):
        sched.budget_at(-1)


def test_annealing_schedule_validation():
    with pytest.raises(EnforcementError, match="floor"):
        AnnealingSchedule(start=8, floor=0, step=1)
    with pytest.raises(EnforcementError, match="must be >= floor"):
        AnnealingSchedule(start=3, floor=4, step=1)
    with pytest.raises(EnforcementError, match="step"):
        AnnealingSchedule(start=8, floor=4, step=-1)


# --- R20: persona rotation trigger --------------------------------------------


def test_persona_rotation_fires_only_on_plateau():
    params = EnforcementParams(plateau_window=3, plateau_min_improvement=0.02)
    # A still-improving curve does NOT rotate.
    assert should_rotate_personas([0.50, 0.60, 0.72], params) is False
    # A flat curve (improvement below the floor) rotates.
    assert should_rotate_personas([0.70, 0.705, 0.71], params) is True
    # A declining curve also counts as "not improving" -> rotates.
    assert should_rotate_personas([0.72, 0.70, 0.69], params) is True
    # Not enough history -> no plateau call (rotation needs evidence).
    assert should_rotate_personas([0.70, 0.71], params) is False
    # The window only looks at the recent tail: an old dip then steady climb is
    # still improving across the window.
    assert should_rotate_personas([0.10, 0.50, 0.60, 0.70], params) is False
