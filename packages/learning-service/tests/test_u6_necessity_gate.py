"""MAT-145 (U6) — Necessity gate over golden cases.

Acceptance checklist (all 6 items from Linear MAT-145):
  [x] test_unrelated_golden_case_is_no_signal
  [x] test_necessity_is_with_vs_without
  [x] test_rare_but_necessary_kept
  [x] test_misretrieval_is_scope_signal_not_unnecessary
  [x] test_triviality_dedup_filter_at_fold
  [x] test_scheduled_scan_demotes_folded_useless_within_one_cycle

Additional correctness tests:
  [x] test_authored_idea_is_exempt_from_necessity
  [x] test_no_signal_when_no_golden_case
  [x] test_advisory_gate_logs_but_does_not_demote
  [x] test_soft_block_gate_demotes_when_open
  [x] test_triviality_empty_body_blocked
  [x] test_triviality_no_anchors_blocked_for_inferred
  [x] test_triviality_authored_passes_without_anchors
  [x] test_triviality_near_duplicate_blocked
  [x] test_triviality_distinct_idea_passes
  [x] test_necessity_scan_skips_open_ideas

All tests are OFFLINE (no boto3, no model load, no network).
The judge is always injected as a mock callable.
"""
from __future__ import annotations

import pytest

from learning_service.db.store import (
    InMemoryLearningStore,
    ProcessedPrRecord,
    VersionConflictError,
)
from learning_service.schema.generated.py_types import (
    AnchorRecord,
    GoldenCaseRecord,
    IdeaRecord,
    IdeaSourceRecord,
)
from learning_service.necessity import (
    NEAR_DUPLICATE_THRESHOLD,
    NECESSITY_SCORE_DIFF_THRESHOLD,
    NecessityFpGate,
    NecessityVerdict,
    ScanResult,
    TrivialityResult,
    assess_necessity,
    run_necessity_scan,
    triviality_dedup_filter,
)

# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

ORG = "acme"
SKILL = "error-handling"


def _make_store() -> InMemoryLearningStore:
    return InMemoryLearningStore()


def _make_idea(
    idea_id: str = "idea-1",
    body: str = "Always validate input at the API boundary.",
    status: str = "folded",
    authority: str = "merged",
    authored: bool | None = None,
) -> IdeaRecord:
    return IdeaRecord(
        ideaId=idea_id,
        skillBaseName=SKILL,
        org=ORG,
        body=body,
        status=status,
        corroborationVersion=2,
        authorityKind=authority,
        authored=authored,
    )


def _make_golden(
    idea_id: str = "idea-1",
    idea_body: str = "Always validate input at the API boundary.",
) -> GoldenCaseRecord:
    return GoldenCaseRecord(
        caseId=idea_id,
        skillBaseName=SKILL,
        org=ORG,
        before="def handle_request(data): process(data)",
        after="def handle_request(data):\n    validate(data)\n    process(data)",
        ideaBody=idea_body,
    )


def _make_anchor(idea_id: str = "idea-1") -> AnchorRecord:
    return AnchorRecord(
        ideaId=idea_id,
        ownerRepo="acme/backend",
        file="src/auth.py",
        symbol="handle_request",
        org=ORG,
        active=True,
    )


def _seed_idea(store: InMemoryLearningStore, idea: IdeaRecord) -> None:
    """Seed an idea (version -1 first write)."""
    store.put_idea_conditional(idea, expected_version=-1)


def _seed_golden(store: InMemoryLearningStore, gc: GoldenCaseRecord) -> None:
    store.put_golden_case(gc)


# Judge mock helpers

def _judge_satisfied(**kw) -> object:
    """Mock run_judge that always returns satisfied=True.

    Accepts keyword args matching the real run_judge signature:
    (prompt, schema, model, max_retries).
    """
    class _R:
        output = {"satisfied": True, "reason": "mock"}
    return _R()


def _judge_unsatisfied(**kw) -> object:
    """Mock run_judge that always returns satisfied=False."""
    class _R:
        output = {"satisfied": False, "reason": "mock"}
    return _R()


# For ablation: a judge that returns satisfied=True when the idea body is
# present in the candidate prompt (simulating the "with" case benefiting).
def _judge_context_sensitive(**kw) -> object:
    prompt = kw.get("prompt", "")
    class _R:
        output = {
            "satisfied": "[Relevant insight:" in prompt,
            "reason": "mock context-sensitive",
        }
    return _R()


# ===========================================================================
# 1. test_unrelated_golden_case_is_no_signal
#    An absent or unrelated golden case yields no_signal (never demotes).
# ===========================================================================


def test_unrelated_golden_case_is_no_signal():
    """When no golden case exists for the idea, the verdict is no_signal — never demotes.

    The plan: "absent such a PR the gate yields no_signal, never a false
    unnecessary verdict."
    """
    store = _make_store()
    idea = _make_idea()
    _seed_idea(store, idea)
    # Deliberately do NOT write a golden case.

    verdict = assess_necessity(idea, store, run_judge_fn=_judge_satisfied)

    assert verdict.outcome == "no_signal", (
        f"Expected no_signal when no golden case exists, got {verdict.outcome!r}"
    )
    assert verdict.demoted is False, "no_signal must never demote"
    assert "no_golden_case" in verdict.reason


# ===========================================================================
# 2. test_necessity_is_with_vs_without
#    Ablation = judge(with idea) − judge(without idea).
# ===========================================================================


def test_necessity_is_with_vs_without():
    """The necessity assessment uses a with-vs-without ablation design.

    The plan: "Necessity = does including the idea improve the case's outcome
    (with vs. without)."  When the judge is sensitive to the idea's presence,
    score_with > score_without.
    """
    store = _make_store()
    idea = _make_idea()
    gc = _make_golden()
    _seed_idea(store, idea)
    _seed_golden(store, gc)

    # Context-sensitive judge: satisfied=True iff idea body is in the prompt.
    verdict = assess_necessity(
        idea,
        store,
        run_judge_fn=_judge_context_sensitive,
        score_diff_threshold=0.0,  # any positive diff → necessary
    )

    assert verdict.score_with is not None and verdict.score_without is not None, (
        "assess_necessity must populate score_with and score_without"
    )
    assert verdict.score_with >= verdict.score_without, (
        "score_with must be >= score_without when the idea aids the case"
    )
    assert verdict.outcome == "necessary", (
        f"Expected 'necessary' when idea improves outcome, got {verdict.outcome!r}"
    )


# ===========================================================================
# 3. test_rare_but_necessary_kept
#    A rare-but-necessary idea is NOT demoted.
# ===========================================================================


def test_rare_but_necessary_kept():
    """A rare-but-necessary idea remains active (not demoted) after the scan.

    The plan: "keep rare-but-necessary ones."  When the context-sensitive judge
    shows the idea matters, the outcome is 'necessary' and the idea stays live.
    """
    store = _make_store()
    idea = _make_idea(idea_id="rare-idea")
    gc = _make_golden(idea_id="rare-idea", idea_body=idea.body)
    _seed_idea(store, idea)
    _seed_golden(store, gc)

    # Use a calibrated gate (soft_block) — this makes the test check that even
    # in enforce mode a necessary idea is NOT demoted.
    gate = NecessityFpGate()
    # Seed enough TP spot-checks to open the gate.
    for i in range(50):
        gate.record_verdict(f"v{i}", is_false_positive=False)  # all TPs

    verdict = assess_necessity(
        idea,
        store,
        run_judge_fn=_judge_context_sensitive,
        fp_gate=gate,
        score_diff_threshold=0.0,
    )

    assert verdict.outcome == "necessary", (
        f"Rare-but-necessary idea should remain necessary, got {verdict.outcome!r}"
    )
    assert verdict.demoted is False, "A necessary idea must never be demoted"

    # The idea must still be live in the store.
    live = store.get_idea(ORG, SKILL, "rare-idea")
    assert live is not None
    assert live.invalidAt is None, "Rare-but-necessary idea must not have invalidAt set"


# ===========================================================================
# 4. test_misretrieval_is_scope_signal_not_unnecessary
#    A mis-retrieved golden case → scope_miss, never 'unnecessary'.
# ===========================================================================


def test_misretrieval_is_scope_signal_not_unnecessary():
    """A mis-retrieved golden case (wrong idea body) yields scope_miss, not unnecessary.

    The plan: "Mis-retrieval = a scope-miss signal not an unnecessary verdict."

    We construct a golden case whose ideaBody is about a completely different topic
    than the idea's body — low word-Jaccard overlap → mis-retrieval detected.
    """
    store = _make_store()
    # Idea is about Python naming conventions.
    idea = _make_idea(idea_id="my-idea", body="Use snake_case naming conventions for Python identifiers.")
    # Golden case's ideaBody is about Java Spring DI — completely different topic.
    gc = GoldenCaseRecord(
        caseId="my-idea",
        skillBaseName=SKILL,
        org=ORG,
        before="old code",
        after="new code",
        # Very low word overlap with idea.body → Jaccard < 0.20 → mis-retrieval.
        ideaBody="Always configure Spring beans via constructor injection in enterprise Java applications.",
    )
    _seed_idea(store, idea)
    _seed_golden(store, gc)

    verdict = assess_necessity(idea, store, run_judge_fn=_judge_satisfied)

    assert verdict.outcome == "scope_miss", (
        f"Expected scope_miss for a mis-retrieved golden case, got {verdict.outcome!r}"
    )
    assert verdict.demoted is False, "A scope_miss must never trigger a demote"
    assert "unrelated" in verdict.reason or "scope" in verdict.reason


# ===========================================================================
# 5. test_triviality_dedup_filter_at_fold
#    Fold-time triviality/dedup filter blocks empty, anchorless, and near-dup ideas.
# ===========================================================================


def test_triviality_dedup_filter_at_fold():
    """The fold-time triviality/dedup filter runs in-process before folding.

    Sub-tests:
    (a) Empty body → blocked (reason: empty_body)
    (b) No anchors (inferred idea) → blocked (reason: no_anchors)
    (c) Near-duplicate of a live idea → blocked (reason: near_duplicate)
    (d) Valid, distinct idea with anchors → passes (reason: ok)
    """
    # (a) Empty body.
    empty_idea = _make_idea(body="   ")  # whitespace-only
    result_a = triviality_dedup_filter(empty_idea, [], [])
    assert result_a.passed is False, "Empty body must be blocked"
    assert result_a.reason == "empty_body"

    # (b) No anchors for an inferred (merged) idea.
    inferred_idea = _make_idea(body="Use dependency injection for testability.", authority="merged")
    result_b = triviality_dedup_filter(inferred_idea, [], [])  # no anchors
    assert result_b.passed is False, "Inferred idea with no anchors must be blocked"
    assert result_b.reason == "no_anchors"

    # (c) Near-duplicate of a live idea.
    incumbent = _make_idea(
        idea_id="live-1",
        body="Always validate user inputs to prevent injection attacks.",
    )
    near_dup = _make_idea(
        idea_id="candidate",
        body="Always validate user inputs to prevent injection attacks and XSS.",
    )
    anchors = [_make_anchor("candidate")]
    result_c = triviality_dedup_filter(near_dup, anchors, [incumbent])
    assert result_c.passed is False, "Near-duplicate must be blocked"
    assert result_c.reason == "near_duplicate"
    assert result_c.near_duplicate_of == "live-1"

    # (d) Valid, distinct, with anchor → passes.
    valid_idea = _make_idea(
        idea_id="valid-1",
        body="Use snake_case for Python identifiers.",  # low overlap with incumbent
    )
    valid_anchors = [_make_anchor("valid-1")]
    live_ideas = [
        _make_idea(idea_id="existing-1", body="Always handle errors explicitly.")
    ]
    result_d = triviality_dedup_filter(valid_idea, valid_anchors, live_ideas)
    assert result_d.passed is True, (
        f"A valid, distinct idea with anchors should pass the filter; got reason={result_d.reason!r}"
    )
    assert result_d.reason == "ok"


# ===========================================================================
# 6. test_scheduled_scan_demotes_folded_useless_within_one_cycle
#    The scheduled scan demotes a useless idea within one scan cycle.
# ===========================================================================


def test_scheduled_scan_demotes_folded_useless_within_one_cycle():
    """A folded-but-useless idea is demoted within one scheduled scan cycle.

    The plan: "full necessity ablation runs on a scheduled scan
    (EventBridge rate(1 day) → the Python necessity handler …), demoting a
    folded-but-useless idea within one cycle."

    Setup: plant one useless idea (judge insensitive to its presence — same
    score with or without) with a golden case, in a calibrated-open fp_gate.
    Run one scan; the idea must be demoted (invalidAt stamped).
    """
    store = _make_store()
    idea = _make_idea(idea_id="useless-idea", body="This idea adds no value.")
    gc = _make_golden(idea_id="useless-idea", idea_body=idea.body)
    _seed_idea(store, idea)
    _seed_golden(store, gc)

    # Judge always unsatisfied (score 0) → score_with == score_without == 0
    # → score_diff == 0 → unnecessary.
    # Open the gate so the scan will actually demote.
    gate = NecessityFpGate()
    for i in range(50):
        gate.record_verdict(f"v{i}", is_false_positive=False)  # all TPs → FP rate 0%
    assert gate.may_demote(), "Test setup: gate must be open"

    scan = run_necessity_scan(
        ORG,
        SKILL,
        store,
        run_judge_fn=_judge_unsatisfied,
        fp_gate=gate,
        score_diff_threshold=NECESSITY_SCORE_DIFF_THRESHOLD,
    )

    assert scan.ideas_scanned >= 1, "Scan should have scanned at least the useless idea"
    assert scan.ideas_demoted >= 1, (
        f"Expected at least one idea demoted, got ideas_demoted={scan.ideas_demoted}"
    )

    # Verify the idea was actually retired in the store.
    retired = store.get_idea(ORG, SKILL, "useless-idea")
    assert retired is not None, "Demoted idea must still exist as history"
    assert retired.invalidAt is not None, (
        "Useless idea must have invalidAt set after one scan cycle"
    )


# ===========================================================================
# Additional correctness tests
# ===========================================================================


def test_authored_idea_is_exempt_from_necessity():
    """An authored idea (user_directive) is never demoted by necessity."""
    store = _make_store()
    for authority in ("user_directive", "authored_import"):
        idea = _make_idea(idea_id=f"authored-{authority}", authority=authority)
        _seed_idea(store, idea)
        # No golden case needed — exemption fires before the golden-case lookup.

        verdict = assess_necessity(idea, store, run_judge_fn=_judge_unsatisfied)

        assert verdict.outcome == "exempt", (
            f"Authored idea (authority={authority!r}) must be exempt; got {verdict.outcome!r}"
        )
        assert verdict.demoted is False


def test_authored_idea_via_authored_flag_is_exempt():
    """An idea with authored=True is also exempt, regardless of authorityKind."""
    store = _make_store()
    idea = _make_idea(idea_id="authored-flag", authority="merged", authored=True)
    _seed_idea(store, idea)

    verdict = assess_necessity(idea, store, run_judge_fn=_judge_unsatisfied)
    assert verdict.outcome == "exempt"
    assert verdict.demoted is False


def test_no_signal_when_no_golden_case():
    """assess_necessity returns no_signal when the golden case is absent."""
    store = _make_store()
    idea = _make_idea()
    _seed_idea(store, idea)
    # No golden case.

    verdict = assess_necessity(idea, store, run_judge_fn=_judge_satisfied)
    assert verdict.outcome == "no_signal"
    assert verdict.demoted is False


def test_advisory_gate_logs_but_does_not_demote():
    """In advisory mode (gate not yet calibrated) a useless idea is logged but not demoted."""
    store = _make_store()
    idea = _make_idea()
    gc = _make_golden()
    _seed_idea(store, idea)
    _seed_golden(store, gc)

    # Empty gate → advisory mode.
    gate = NecessityFpGate()
    assert gate.stage == "advisory", "Fresh gate should be advisory"
    assert not gate.may_demote()

    verdict = assess_necessity(
        idea,
        store,
        run_judge_fn=_judge_unsatisfied,
        fp_gate=gate,
        score_diff_threshold=NECESSITY_SCORE_DIFF_THRESHOLD,
    )

    assert verdict.outcome == "unnecessary"
    assert verdict.demoted is False, (
        "Advisory mode must not actually demote — only log"
    )

    # Idea must still be live.
    live = store.get_idea(ORG, SKILL, idea.ideaId)
    assert live is not None
    assert live.invalidAt is None


def test_soft_block_gate_demotes_when_open():
    """Once the fp_gate is open (soft_block stage), unnecessary ideas are actually demoted."""
    store = _make_store()
    idea = _make_idea()
    gc = _make_golden()
    _seed_idea(store, idea)
    _seed_golden(store, gc)

    gate = NecessityFpGate()
    for i in range(50):
        gate.record_verdict(f"v{i}", is_false_positive=False)
    assert gate.stage == "soft_block"
    assert gate.may_demote()

    verdict = assess_necessity(
        idea,
        store,
        run_judge_fn=_judge_unsatisfied,
        fp_gate=gate,
        score_diff_threshold=NECESSITY_SCORE_DIFF_THRESHOLD,
    )

    assert verdict.outcome == "unnecessary"
    assert verdict.demoted is True, "Soft-block gate must execute the demote"

    retired = store.get_idea(ORG, SKILL, idea.ideaId)
    assert retired is not None
    assert retired.invalidAt is not None


def test_triviality_empty_body_blocked():
    """Triviality filter blocks ideas with empty body."""
    idea = _make_idea(body="")
    result = triviality_dedup_filter(idea, [_make_anchor()], [])
    assert result.passed is False
    assert result.reason == "empty_body"


def test_triviality_no_anchors_blocked_for_inferred():
    """Triviality filter blocks inferred (merged) ideas with no anchors."""
    idea = _make_idea(authority="merged")
    result = triviality_dedup_filter(idea, [], [])
    assert result.passed is False
    assert result.reason == "no_anchors"


def test_triviality_authored_passes_without_anchors():
    """Triviality filter does NOT require anchors for authored ideas."""
    for authority in ("user_directive", "authored_import"):
        idea = _make_idea(authority=authority)
        result = triviality_dedup_filter(idea, [], [])  # no anchors
        assert result.passed is True, (
            f"Authored idea (authority={authority!r}) must pass even without anchors"
        )


def test_triviality_authored_flag_passes_without_anchors():
    """Triviality filter does NOT require anchors when authored=True."""
    idea = _make_idea(authority="merged", authored=True)
    result = triviality_dedup_filter(idea, [], [])
    assert result.passed is True


def test_triviality_near_duplicate_blocked():
    """Triviality filter blocks a near-duplicate of a live idea."""
    incumbent = _make_idea(
        idea_id="existing",
        body="Always validate user inputs to prevent injection attacks.",
    )
    candidate = _make_idea(
        idea_id="new",
        body="Always validate user inputs to prevent injection attacks in all APIs.",
    )
    anchors = [_make_anchor("new")]
    result = triviality_dedup_filter(candidate, anchors, [incumbent])
    assert result.passed is False
    assert result.reason == "near_duplicate"
    assert result.near_duplicate_of == "existing"


def test_triviality_distinct_idea_passes():
    """Triviality filter passes a semantically distinct idea with anchors."""
    existing = _make_idea(
        idea_id="existing",
        body="Use dependency injection for testability.",
    )
    candidate = _make_idea(
        idea_id="new",
        body="Never commit secrets to version control.",
    )
    anchors = [_make_anchor("new")]
    result = triviality_dedup_filter(candidate, anchors, [existing])
    assert result.passed is True
    assert result.reason == "ok"


def test_necessity_scan_skips_open_ideas():
    """The scheduled scan only processes folded ideas, not open ones."""
    store = _make_store()

    # Seed two ideas: one folded (should be scanned), one open (should be skipped).
    folded_idea = _make_idea(idea_id="folded", status="folded")
    open_idea = _make_idea(idea_id="open", status="open")
    _seed_idea(store, folded_idea)
    _seed_idea(store, open_idea)

    # Only seed a golden case for the folded idea.
    gc = _make_golden(idea_id="folded", idea_body=folded_idea.body)
    _seed_golden(store, gc)

    # Open a calibrated gate.
    gate = NecessityFpGate()
    for i in range(50):
        gate.record_verdict(f"v{i}", is_false_positive=False)

    scan = run_necessity_scan(ORG, SKILL, store, run_judge_fn=_judge_unsatisfied, fp_gate=gate)

    # Only the folded idea should appear in the scan (open ideas are ignored).
    assert scan.ideas_scanned == 1, (
        f"Scan should only count folded ideas; got ideas_scanned={scan.ideas_scanned}"
    )
    scanned_ids = [v.idea_id for v in scan.verdicts]
    assert "open" not in scanned_ids, "Open ideas must not appear in scan results"
    assert "folded" in scanned_ids, "Folded idea must appear in scan results"


def test_necessary_idea_not_demoted_even_with_open_gate():
    """Even when the fp_gate is open, a necessary idea must NOT be demoted."""
    store = _make_store()
    idea = _make_idea()
    gc = _make_golden()
    _seed_idea(store, idea)
    _seed_golden(store, gc)

    # Context-sensitive judge: satisfied when idea body is in the prompt.
    gate = NecessityFpGate()
    for i in range(50):
        gate.record_verdict(f"v{i}", is_false_positive=False)

    verdict = assess_necessity(
        idea,
        store,
        run_judge_fn=_judge_context_sensitive,
        fp_gate=gate,
        score_diff_threshold=0.0,  # any positive diff → necessary
    )

    assert verdict.outcome == "necessary"
    assert verdict.demoted is False

    live = store.get_idea(ORG, SKILL, idea.ideaId)
    assert live is not None
    assert live.invalidAt is None


def test_scan_demoted_idea_is_queryable_as_history():
    """After a scan demote, the idea still exists in the store (invalidAt set, not deleted).

    The plan: "valid-until-reversal, never deleted."
    """
    store = _make_store()
    idea = _make_idea(idea_id="history-test")
    gc = _make_golden(idea_id="history-test", idea_body=idea.body)
    _seed_idea(store, idea)
    _seed_golden(store, gc)

    gate = NecessityFpGate()
    for i in range(50):
        gate.record_verdict(f"v{i}", is_false_positive=False)

    run_necessity_scan(ORG, SKILL, store, run_judge_fn=_judge_unsatisfied, fp_gate=gate)

    retired = store.get_idea(ORG, SKILL, "history-test")
    assert retired is not None, "Demoted idea must remain in the store as history"
    assert retired.invalidAt is not None, "Demoted idea must have invalidAt stamped"

    # list_current_ideas must exclude it (it's retired).
    current = store.list_current_ideas(ORG, SKILL)
    current_ids = [i.ideaId for i in current]
    assert "history-test" not in current_ids, (
        "Demoted idea must be excluded from the current idea set"
    )


def test_fp_gate_stage_transitions():
    """NecessityFpGate transitions advisory → soft_block when calibrated."""
    gate = NecessityFpGate()
    assert gate.stage == "advisory"
    assert not gate.may_demote()

    # Add 49 TPs — still not enough samples.
    for i in range(49):
        gate.record_verdict(f"v{i}", is_false_positive=False)
    assert gate.stage == "advisory"
    assert not gate.may_demote()

    # 50th TP → threshold met.
    gate.record_verdict("v49", is_false_positive=False)
    assert gate.stage == "soft_block"
    assert gate.may_demote()


def test_fp_gate_stays_advisory_with_high_fp_rate():
    """NecessityFpGate stays advisory when FP rate is too high."""
    gate = NecessityFpGate()
    # 50 verdicts, 10 FPs → FP rate = 20% > ceiling 15%.
    for i in range(40):
        gate.record_verdict(f"tp{i}", is_false_positive=False)
    for i in range(10):
        gate.record_verdict(f"fp{i}", is_false_positive=True)

    assert gate.sample_count == 50
    assert gate.fp_rate > gate.fp_ceiling
    assert gate.stage == "advisory"
    assert not gate.may_demote()
