"""plan-005 U6: Enforcement activation and annealing (R18-R20).

Fully offline (the default suite): the threshold is derived from a fixture
similarity distribution (or store rows inserted through the public API), the
suspect re-verification is an injected fake, and every other input is typed data.
Zero quota, no ``claude`` on PATH.

## Conformance (plan-005 U6 test scenarios / Verification -> test, 1:1)

- tripwire fires only above the derived threshold (fixture distributions):
  ``test_tripwire_fires_only_above_derived_threshold``
- killed loop escalates with the existing typed record (no new failure shape):
  ``test_killed_loop_escalates_with_existing_typed_record``
- suspect verifier's ticket re-verified before fitness lands:
  ``test_suspect_ticket_reverified_before_fitness``
- suspect episode scores excluded from chart recompute:
  ``test_suspect_episode_scores_excluded_from_chart_recompute``
- annealing schedule steps the budget per epoch:
  ``test_annealing_schedule_steps_budget_per_epoch``
- rotation trigger fires only on the plateau condition:
  ``test_rotation_trigger_fires_only_on_plateau``
- Verification — shadow-vs-enforce A/B shows identical detection, differing only
  in action: ``test_shadow_vs_enforce_ab_identical_detection``

Supporting invariants beyond the named scenarios: the small-N derivation guard,
threshold/mode/floor range validation, NULL-baseline exclusion from the
derivation, the detector-priority order, the non-suspect fast path, and the
small-N plateau guard.
"""

from __future__ import annotations

import pytest

from agent_families.pipeline import enforcement as e
from agent_families.pipeline.tripwires import (
    DETECTOR_DIFF_SIMILARITY,
    DETECTOR_NO_PROGRESS,
)
from agent_families.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


# A logged shadow distribution: most iterations make real progress (low
# consecutive similarity), a tail are near-duplicates (the worker stuck).
FIXTURE_SIMILARITIES = (
    [0.10, 0.15, 0.20, 0.22, 0.25, 0.30, 0.33, 0.35, 0.38, 0.40]
    + [0.42, 0.45, 0.48, 0.50, 0.52, 0.55, 0.58, 0.60, 0.62, 0.65]
    + [0.68, 0.70, 0.72, 0.74, 0.76, 0.78, 0.80, 0.85, 0.90, 0.97]
)


# --- R18: threshold derivation + fire-only-above ---------------------------------


def test_tripwire_fires_only_above_derived_threshold():
    """The threshold is the high quantile of the LOGGED distribution, and a kill
    fires only at or above it — never below (R18)."""
    derived = e.derive_similarity_threshold_from_values(
        FIXTURE_SIMILARITIES, quantile=0.95, min_observations=30
    )
    assert derived.detector_kind == DETECTOR_DIFF_SIMILARITY
    assert derived.n_observations == 30
    # 0.95-quantile of the 30-point fixture sits in the upper tail: well above the
    # median (~0.5) and strictly below the stuck-iteration max (0.97).
    median = sorted(FIXTURE_SIMILARITIES)[15]
    assert median < derived.threshold < max(FIXTURE_SIMILARITIES)
    assert "0.95-quantile of 30" in derived.provenance

    enforcer = e.TripwireEnforcer(
        mode=e.MODE_ENFORCE, similarity_threshold=derived.threshold
    )
    above = enforcer.evaluate(ralph_iteration=2, similarity=derived.threshold + 0.01)
    assert above.would_kill and above.action == e.ACTION_KILL
    below = enforcer.evaluate(ralph_iteration=2, similarity=derived.threshold - 0.01)
    assert not below.would_kill and below.action == e.ACTION_CONTINUE
    # Exactly at the threshold fires (the same >= predicate the shadow log uses).
    at = enforcer.evaluate(ralph_iteration=2, similarity=derived.threshold)
    assert at.would_kill


def test_derivation_refuses_thin_sample():
    """Enforcement on a sample below min_observations is the §17-forbidden move."""
    with pytest.raises(e.EnforcementError, match="shadow-first"):
        e.derive_similarity_threshold_from_values(
            [0.5, 0.9, 0.95], quantile=0.95, min_observations=30
        )


def test_derivation_reads_store_excluding_null_baseline(store):
    """The store derivation reads logged rows and excludes the NULL-similarity
    first-iteration baselines (only real iteration distances count)."""
    for i, sim in enumerate(FIXTURE_SIMILARITIES):
        store.insert_tripwire_event(
            detector_kind=DETECTOR_DIFF_SIMILARITY,
            would_have_fired=False,
            ralph_iteration=i,
            similarity=sim,
            embedding_model="nomic",
            embedding_dim=768,
        )
    # A NULL-similarity baseline row (a ticket's first iteration) — excluded.
    store.insert_tripwire_event(
        detector_kind=DETECTOR_DIFF_SIMILARITY,
        would_have_fired=False,
        ralph_iteration=0,
        similarity=None,
    )
    # A no_progress row — wrong detector, excluded.
    store.insert_tripwire_event(
        detector_kind=DETECTOR_NO_PROGRESS,
        would_have_fired=True,
        ralph_iteration=3,
        failure_set_hash="abc",
    )
    assert e.logged_diff_similarities(store) == tuple(FIXTURE_SIMILARITIES)
    derived = e.derive_similarity_threshold(store, quantile=0.95, min_observations=30)
    assert derived.n_observations == 30


def test_killed_loop_escalates_with_existing_typed_record():
    """A fired tripwire in enforce mode ends the loop with the EXISTING detector
    kind — no new failure shape is invented (R18)."""
    enforcer = e.TripwireEnforcer(mode=e.MODE_ENFORCE, similarity_threshold=0.85)

    # diff_similarity kill carries the existing diff detector kind + threshold.
    diff = enforcer.evaluate(ralph_iteration=4, similarity=0.99)
    assert diff.action == e.ACTION_KILL
    assert diff.escalation is not None
    assert diff.escalation.detector_kind == DETECTOR_DIFF_SIMILARITY
    assert diff.escalation.detector_kind in e.EXISTING_TYPED_DETECTORS
    assert diff.escalation.threshold == 0.85
    assert diff.escalation.value == 0.99

    # no_progress kill carries the existing no-progress detector kind.
    stall = enforcer.evaluate(ralph_iteration=5, no_progress_fired=True)
    assert stall.action == e.ACTION_KILL
    assert stall.escalation.detector_kind == DETECTOR_NO_PROGRESS
    assert stall.escalation.detector_kind in e.EXISTING_TYPED_DETECTORS

    # The typed-record invariant is structural: a non-detector kind is rejected.
    with pytest.raises(e.EnforcementError, match="no new failure shape"):
        e.TripwireEscalation(
            detector_kind="some_new_kind",
            ralph_iteration=1,
            value=None,
            threshold=None,
            reason="",
        )


def test_no_progress_checked_before_diff_when_both_fire():
    """Deterministic detector priority: a confirmed stall is reported first."""
    enforcer = e.TripwireEnforcer(mode=e.MODE_ENFORCE, similarity_threshold=0.85)
    decision = enforcer.evaluate(
        ralph_iteration=6, similarity=0.99, no_progress_fired=True
    )
    assert decision.detector_kind == DETECTOR_NO_PROGRESS


def test_enforcer_rejects_bad_mode_and_threshold():
    with pytest.raises(e.EnforcementError, match="mode must be"):
        e.TripwireEnforcer(mode="kill", similarity_threshold=0.5)
    with pytest.raises(e.EnforcementError, match="cosine in"):
        e.TripwireEnforcer(mode=e.MODE_SHADOW, similarity_threshold=1.5)


# --- Verification: shadow-vs-enforce A/B -----------------------------------------


def test_shadow_vs_enforce_ab_identical_detection():
    """Same signal stream, both modes: identical DETECTION, only the ACTION
    differs — shadow logs every firing and continues; enforce kills at the first
    (Unit Verification)."""
    signals = [
        {"ralph_iteration": 0, "similarity": None},          # baseline, no fire
        {"ralph_iteration": 1, "similarity": 0.20},          # progress
        {"ralph_iteration": 2, "similarity": 0.50},          # progress
        {"ralph_iteration": 3, "similarity": 0.97},          # FIRES (stuck)
        {"ralph_iteration": 4, "similarity": 0.99},          # would fire again
        {"ralph_iteration": 5, "no_progress_fired": True},   # would fire again
    ]
    report = e.shadow_vs_enforce(signals, similarity_threshold=0.85)

    assert report.detection_identical
    assert report.action_differs
    assert report.first_firing_iteration == 3

    # Shadow processes ALL six and never kills; it flags three firings.
    assert len(report.shadow.decisions) == 6
    assert report.shadow.killed_at is None
    assert all(d.action == e.ACTION_CONTINUE for d in report.shadow.decisions)
    assert [d.would_kill for d in report.shadow.decisions] == [
        False, False, False, True, True, True
    ]

    # Enforce stops at the first firing (iteration 3) with a KILL action.
    assert len(report.enforce.decisions) == 4
    assert report.enforce.killed_at == 3
    assert report.enforce.decisions[-1].action == e.ACTION_KILL
    # Over the shared prefix, the would_kill detection is byte-identical.
    assert [d.would_kill for d in report.enforce.decisions] == [
        False, False, False, True
    ]


# --- R19: suspect-verdict re-verification before fitness -------------------------


def test_suspect_ticket_reverified_before_fitness():
    """A ticket closed by a flagged verifier is re-verified before its fitness
    counts; a non-suspect ticket counts with no re-verification (R19)."""
    tickets = [
        e.SuspectTicket(ticket_id="T-clean", closing_chk_ids=("CHK-a", "CHK-b")),
        e.SuspectTicket(ticket_id="T-suspect-ok", closing_chk_ids=("CHK-x",)),
        e.SuspectTicket(ticket_id="T-suspect-bad", closing_chk_ids=("CHK-y",)),
    ]
    suspect_chk_ids = ("CHK-x", "CHK-y")

    reverified_tickets: list[str] = []

    def reverify(ticket_id: str) -> bool:
        reverified_tickets.append(ticket_id)
        return ticket_id == "T-suspect-ok"  # the clean re-run passes; the other fails

    results = {
        r.ticket_id: r
        for r in e.gate_suspect_fitness(tickets, suspect_chk_ids, reverify)
    }

    # The non-suspect ticket counts immediately and is NEVER re-verified.
    assert results["T-clean"].fitness_counts is True
    assert results["T-clean"].suspect is False
    assert results["T-clean"].reverified is None
    assert "T-clean" not in reverified_tickets

    # Suspect tickets are re-verified BEFORE fitness; only the clean re-run counts.
    assert set(reverified_tickets) == {"T-suspect-ok", "T-suspect-bad"}
    assert results["T-suspect-ok"].suspect is True
    assert results["T-suspect-ok"].reverified is True
    assert results["T-suspect-ok"].fitness_counts is True
    assert results["T-suspect-bad"].reverified is False
    assert results["T-suspect-bad"].fitness_counts is False


# --- R19: instrument_suspect episode exclusion from curriculum -------------------


def test_suspect_episode_scores_excluded_from_chart_recompute():
    """instrument_suspect episode scores are excluded from the curriculum's chart
    recompute — the recomputed series and its mean change when a suspect point is
    dropped (R19)."""
    scores = [
        e.EpisodeScore(episode_id=1, target="linkding", epoch=0, score=0.90),
        e.EpisodeScore(episode_id=2, target="linkding", epoch=0, score=0.91),
        e.EpisodeScore(
            episode_id=3, target="linkding", epoch=0, score=0.20,
            instrument_suspect=True,  # a grader-drift point that would tank the chart
        ),
        e.EpisodeScore(episode_id=4, target="linkding", epoch=1, score=0.92),
    ]
    eligible = e.curriculum_eligible_scores(scores)
    assert [s.episode_id for s in eligible] == [1, 2, 4]

    values = e.eligible_score_values(scores)
    assert values == (0.90, 0.91, 0.92)
    # The suspect 0.20 never reaches the recompute: the clean mean stays ~0.91.
    assert abs(sum(values) / len(values) - 0.91) < 1e-9
    with_suspect = tuple(s.score for s in scores)
    assert sum(values) / len(values) != sum(with_suspect) / len(with_suspect)


# --- R20: annealing schedule ------------------------------------------------------


def test_annealing_schedule_steps_budget_per_epoch():
    """The question budget tightens one step per epoch and clamps at the §17 floor
    (R20)."""
    schedule = e.AnnealingSchedule(start=8, floor=4, step=1)
    assert [schedule.budget_for_epoch(epoch) for epoch in range(7)] == [
        8, 7, 6, 5, 4, 4, 4
    ]
    # The floor lands inside the §17 3-5 target band by construction.
    lo, hi = e.QUESTION_BUDGET_TARGET_BAND
    assert lo <= schedule.floor <= hi


def test_annealing_floor_must_land_in_target_band():
    with pytest.raises(e.EnforcementError, match="target band"):
        e.AnnealingSchedule(start=10, floor=8, step=1)
    with pytest.raises(e.EnforcementError, match="target band"):
        e.AnnealingSchedule(start=10, floor=1, step=1)


def test_annealing_rejects_start_below_floor_and_negative_epoch():
    with pytest.raises(e.EnforcementError, match="must be >= floor"):
        e.AnnealingSchedule(start=3, floor=4, step=1)
    with pytest.raises(e.EnforcementError, match="epoch must be >= 0"):
        e.AnnealingSchedule().budget_for_epoch(-1)


# --- R20: persona-rotation plateau trigger ---------------------------------------


def test_rotation_trigger_fires_only_on_plateau():
    """Rotation fires only when the planner scores plateau; a still-improving
    series never rotates (R20 / §9)."""
    rule = e.PersonaRotationRule(window=3, min_improvement=0.01)

    # A flat (plateaued) recent window: improvement 0 <= tolerance -> rotate.
    assert e.should_rotate_personas([0.70, 0.71, 0.71, 0.71], rule) is True
    # A declining window is also a plateau (no improvement) -> rotate.
    assert e.should_rotate_personas([0.71, 0.70, 0.69], rule) is True
    # A clearly improving recent window -> do NOT rotate.
    assert e.should_rotate_personas([0.70, 0.75, 0.82], rule) is False

    # The <= boundary, with cleanly representable floats: improvement exactly at
    # the tolerance counts as a plateau; just above it does not.
    boundary = e.PersonaRotationRule(window=3, min_improvement=0.25)
    assert e.should_rotate_personas([0.50, 0.60, 0.75], boundary) is True   # 0.25
    assert e.should_rotate_personas([0.50, 0.60, 1.00], boundary) is False  # 0.50


def test_plateau_needs_enough_points():
    """Fewer than `window` planner scores is not a plateau (small-N guard)."""
    rule = e.PersonaRotationRule(window=3, min_improvement=0.01)
    assert e.should_rotate_personas([0.70, 0.70], rule) is False
    assert e.should_rotate_personas([], rule) is False


def test_plateau_late_dip_inside_improving_window_is_not_a_plateau():
    """A late dip inside a window that still reached a new high is not a plateau
    (improvement measured against the best the planner reached)."""
    rule = e.PersonaRotationRule(window=3, min_improvement=0.01)
    # window [0.70, 0.90, 0.80]: best 0.90 - first 0.70 = 0.20 improvement.
    assert e.should_rotate_personas([0.70, 0.90, 0.80], rule) is False


def test_rotation_rule_validation():
    with pytest.raises(e.EnforcementError, match="window must be >= 2"):
        e.PersonaRotationRule(window=1)
    with pytest.raises(e.EnforcementError, match="min_improvement must be >= 0"):
        e.PersonaRotationRule(min_improvement=-0.1)
