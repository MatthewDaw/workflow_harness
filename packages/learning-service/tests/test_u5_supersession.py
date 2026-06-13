"""MAT-144 (U5) — Supersession + temporal validity + un-fold.

Acceptance checklist (all 9 items from Linear MAT-144):
  [x] test_verified_contradiction_supersedes_and_stamps_invalid_at
  [x] test_superseded_idea_queryable_as_history_not_deleted
  [x] test_unfold_authors_new_revision_without_corrupting_siblings
  [x] test_inferred_cannot_supersede_authored
  [x] test_more_hits_then_recency_wins_between_inferred
  [x] test_revive_only_on_new_verified_pr
  [x] test_anti_thrash_bounds_oscillation
  [x] test_supersede_fp_calibration_gate_blocks_enforce
  [x] test_deleted_symbol_cleans_stale_anchor

All tests are OFFLINE (no boto3, no model load, no network).
"""
from __future__ import annotations

import time
import pytest

from learning_service.db.store import (
    InMemoryLearningStore,
    OrgGuardError,
    ProcessedPrRecord,
    VerifyEventRecord,
)
from learning_service.schema.generated.py_types import (
    AnchorRecord,
    IdeaRecord,
    IdeaSourceRecord,
)
from learning_service.skills_write import InMemorySkillStore, write_revision, RevisionRequest
from learning_service.supersession import (
    AntiThrashTracker,
    FpCalibrationGate,
    ReviveRequest,
    SupersedeRequest,
    SymbolDeletionRequest,
    _remove_lesson_from_body,
    clean_deleted_symbol_anchors,
    decide_winner,
    revive,
    supersede,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ORG = "test-org"
REPO = "acme/backend"
SKILL = "code-style"


def _make_idea(
    idea_id: str,
    body: str,
    status: str = "open",
    authority: str = "merged",
    folded_into_rev: int | None = None,
    invalid_at: int | None = None,
    superseded_by: str | None = None,
    version: int = 0,
) -> IdeaRecord:
    return IdeaRecord(
        ideaId=idea_id,
        skillBaseName=SKILL,
        org=ORG,
        body=body,
        status=status,
        corroborationVersion=version,
        foldedIntoRev=folded_into_rev,
        invalidAt=invalid_at,
        supersededBy=superseded_by,
        authorityKind=authority,
    )


def _make_source(idea_id: str, pr_number: int, rung: str = "normal") -> IdeaSourceRecord:
    return IdeaSourceRecord(
        ideaId=idea_id,
        sourceId=f"pr#{pr_number}",
        org=ORG,
        prRef=f"{REPO}#{pr_number}",
        authorityKind="merged",
        verificationRung=rung,
        authorId="dev1",
    )


def _store_with_idea(idea: IdeaRecord) -> InMemoryLearningStore:
    s = InMemoryLearningStore()
    s.put_idea(idea)
    return s


def _supersede_request(
    incumbent_id: str,
    challenger_authority: str = "merged",
    pr_number: int = 200,
    challenger_id: str | None = "challenger-001",
    idea_body_to_remove: str | None = None,
) -> SupersedeRequest:
    return SupersedeRequest(
        org=ORG,
        incumbent_idea_id=incumbent_id,
        incumbent_skill_base_name=SKILL,
        challenger_idea_id=challenger_id,
        challenger_skill_base_name=SKILL,
        challenger_pr_number=pr_number,
        challenger_owner_repo=REPO,
        challenger_authority_kind=challenger_authority,
        idea_body_to_remove=idea_body_to_remove,
    )


# ---------------------------------------------------------------------------
# 1. test_verified_contradiction_supersedes_and_stamps_invalid_at
# ---------------------------------------------------------------------------


def test_verified_contradiction_supersedes_and_stamps_invalid_at():
    """A verified PR contradiction: incumbent gets invalidAt stamped."""
    incumbent = _make_idea("idea-001", "Use snake_case for identifiers.")
    store = _store_with_idea(incumbent)

    NOW = 1_700_000_000_000
    req = _supersede_request("idea-001")
    result = supersede(
        req,
        store,
        unfold_mode="enforce",
        fp_gate=_open_gate(),
        now_ms=NOW,
    )

    assert result.action == "superseded"
    assert result.invalid_at == NOW
    assert result.incumbent_idea_id == "idea-001"

    # Idea now has invalidAt stamped.
    updated = store.get_idea(ORG, SKILL, "idea-001")
    assert updated is not None
    assert updated.invalidAt == NOW
    assert updated.supersededBy == "challenger-001"


def _open_gate() -> FpCalibrationGate:
    """An FP gate that is already open (sufficient samples, low FP rate)."""
    gate = FpCalibrationGate(fp_ceiling=0.10, min_samples=5)
    for i in range(6):
        gate.record_verdict(f"v{i}", is_false_positive=False)  # all TP
    return gate


# ---------------------------------------------------------------------------
# 2. test_superseded_idea_queryable_as_history_not_deleted
# ---------------------------------------------------------------------------


def test_superseded_idea_queryable_as_history_not_deleted():
    """Superseded idea is excluded from current set but queryable as history."""
    incumbent = _make_idea("idea-002", "Use tabs for indentation.")
    store = _store_with_idea(incumbent)
    # Also put a second, non-superseded idea.
    other = _make_idea("idea-003", "Always write docstrings.")
    store.put_idea(other)

    req = _supersede_request("idea-002")
    supersede(req, store, unfold_mode="enforce", fp_gate=_open_gate(), now_ms=1)

    # list_current_ideas excludes the retired one.
    current = store.list_current_ideas(ORG, SKILL)
    current_ids = [i.ideaId for i in current]
    assert "idea-002" not in current_ids
    assert "idea-003" in current_ids

    # get_idea returns it (full history).
    retired = store.get_idea(ORG, SKILL, "idea-002")
    assert retired is not None
    assert retired.invalidAt == 1

    # list_all_ideas_for_org includes it.
    all_ideas = store.list_all_ideas_for_org(ORG)
    all_ids = [i.ideaId for i in all_ideas]
    assert "idea-002" in all_ids


# ---------------------------------------------------------------------------
# 3. test_unfold_authors_new_revision_without_corrupting_siblings
# ---------------------------------------------------------------------------


def test_unfold_authors_new_revision_without_corrupting_siblings():
    """Un-fold: new revision drops the superseded lesson; siblings survive."""
    incumbent = _make_idea(
        "idea-fold-001",
        "Use snake_case.",
        status="folded",
        folded_into_rev=1,
    )
    store = _store_with_idea(incumbent)

    # Skill body contains the superseded lesson + a sibling lesson.
    sibling_lesson = "Write unit tests for every public function."
    superseded_lesson = "Use snake_case."
    full_body = f"{superseded_lesson}\n\n{sibling_lesson}"

    skill_store = InMemorySkillStore()
    # Seed rev 1 (the one foldedIntoRev points to).
    from learning_service.schema.generated.py_types import RevisionRecord, TruePointerRecord
    skill_store._revisions[(ORG, "", 1)] = RevisionRecord(
        baseName=SKILL,
        variantId="",
        rev=1,
        body=full_body,
        org=ORG,
    )
    skill_store._pointers[(ORG, SKILL)] = TruePointerRecord(
        baseName=SKILL,
        variantId="",
        rev=1,
        org=ORG,
    )

    req = SupersedeRequest(
        org=ORG,
        incumbent_idea_id="idea-fold-001",
        incumbent_skill_base_name=SKILL,
        challenger_idea_id="challenger-002",
        challenger_skill_base_name=SKILL,
        challenger_pr_number=300,
        challenger_owner_repo=REPO,
        challenger_authority_kind="merged",
        idea_body_to_remove=superseded_lesson,
    )

    result = supersede(
        req,
        store,
        unfold_mode="enforce",
        fp_gate=_open_gate(),
        skill_store=skill_store,
        now_ms=2_000,
    )

    assert result.action == "superseded"
    assert result.unfold_rev is not None
    new_rev = result.unfold_rev

    # The new revision must contain the sibling but NOT the superseded lesson.
    new_body = skill_store.get_revision_body(ORG, "", new_rev)
    assert new_body is not None
    assert sibling_lesson in new_body
    assert "snake_case" not in new_body  # superseded lesson removed

    # The old revision (rev 1) is untouched — immutable history.
    old_body = skill_store.get_revision_body(ORG, "", 1)
    assert old_body is not None
    assert "snake_case" in old_body


# ---------------------------------------------------------------------------
# 4. test_inferred_cannot_supersede_authored
# ---------------------------------------------------------------------------


def test_inferred_cannot_supersede_authored():
    """Authority safety invariant: merged (inferred) cannot supersede authored."""
    # Incumbent is authored (user_directive — top authority).
    incumbent = _make_idea("idea-authored-001", "Never use tabs.", authority="user_directive")
    store = _store_with_idea(incumbent)

    # Challenger is inferred (merged).
    req = _supersede_request("idea-authored-001", challenger_authority="merged")
    result = supersede(req, store, unfold_mode="enforce", fp_gate=_open_gate(), now_ms=1)

    assert result.action == "blocked_authority"

    # The idea is NOT retired.
    unchanged = store.get_idea(ORG, SKILL, "idea-authored-001")
    assert unchanged is not None
    assert unchanged.invalidAt is None
    assert unchanged.supersededBy is None


def test_authored_import_cannot_be_superseded_by_inferred():
    """authored_import is also top authority — inferred cannot supersede it."""
    incumbent = _make_idea("idea-import-001", "Use 4-space indentation.", authority="authored_import")
    store = _store_with_idea(incumbent)

    req = _supersede_request("idea-import-001", challenger_authority="merged")
    result = supersede(req, store, unfold_mode="enforce", fp_gate=_open_gate(), now_ms=1)

    assert result.action == "blocked_authority"
    idea = store.get_idea(ORG, SKILL, "idea-import-001")
    assert idea.invalidAt is None


def test_authored_can_supersede_inferred():
    """A user_directive challenger CAN supersede a merged incumbent."""
    incumbent = _make_idea("idea-inf-001", "Use camelCase.", authority="merged")
    store = _store_with_idea(incumbent)

    req = _supersede_request("idea-inf-001", challenger_authority="user_directive")
    result = supersede(req, store, unfold_mode="enforce", fp_gate=_open_gate(), now_ms=1)

    assert result.action == "superseded"
    idea = store.get_idea(ORG, SKILL, "idea-inf-001")
    assert idea.invalidAt == 1


# ---------------------------------------------------------------------------
# 5. test_more_hits_then_recency_wins_between_inferred
# ---------------------------------------------------------------------------


def test_more_hits_then_recency_wins_between_inferred():
    """Between two inferred ideas, more evidence (hits) wins."""
    # Incumbent has 3 PR sources — more evidence than the single new challenger.
    incumbent = _make_idea("idea-heavy-001", "Use async/await.", authority="merged")
    store = _store_with_idea(incumbent)
    for pr in [10, 11, 12]:
        store.put_idea_source(_make_source("idea-heavy-001", pr))

    # Challenger is one new PR (#200) — less evidence.
    req = _supersede_request("idea-heavy-001", challenger_authority="merged", pr_number=200)
    result = supersede(req, store, unfold_mode="enforce", fp_gate=_open_gate(), now_ms=1)

    # Incumbent wins (more evidence) — blocked.
    assert result.action == "blocked_authority"
    idea = store.get_idea(ORG, SKILL, "idea-heavy-001")
    assert idea.invalidAt is None


def test_recency_wins_between_equal_evidence_inferred():
    """Equal evidence: newer PR (higher number) is more recent and wins."""
    # Incumbent has 1 source (PR #50). Challenger is PR #200 (more recent).
    incumbent = _make_idea("idea-recency-001", "Use sync callbacks.", authority="merged")
    store = _store_with_idea(incumbent)
    store.put_idea_source(_make_source("idea-recency-001", 50))

    req = _supersede_request("idea-recency-001", challenger_authority="merged", pr_number=200)
    result = supersede(req, store, unfold_mode="enforce", fp_gate=_open_gate(), now_ms=1)

    # Challenger wins (equal evidence, recency breaks tie in challenger's favour).
    assert result.action == "superseded"


# ---------------------------------------------------------------------------
# 6. test_revive_only_on_new_verified_pr
# ---------------------------------------------------------------------------


def test_revive_only_on_new_verified_pr():
    """A superseded idea is revived only on a NEW verified PR."""
    retired = _make_idea(
        "idea-revive-001",
        "Use PEP-8 style.",
        status="folded",
        invalid_at=1_000,
        superseded_by="newer-idea",
    )
    store = _store_with_idea(retired)

    req = ReviveRequest(
        org=ORG,
        idea_id="idea-revive-001",
        skill_base_name=SKILL,
        new_pr_number=400,
        new_pr_owner_repo=REPO,
    )
    result = revive(req, store, now_ms=2_000)

    assert result.action == "revived"
    assert result.revived_at == 2_000

    # Idea is now active again.
    revived_idea = store.get_idea(ORG, SKILL, "idea-revive-001")
    assert revived_idea.invalidAt is None
    assert revived_idea.revivedAt == 2_000
    assert revived_idea.status == "open"

    # Audit event was written.
    events = store.list_verify_events(ORG, "idea-revive-001")
    assert any(e.verdict == "revive" for e in events)


def test_revive_no_op_when_idea_already_active():
    """Reviving an already-active idea is a no-op (not double-revived)."""
    active = _make_idea("idea-active-001", "Use type hints.")
    store = _store_with_idea(active)

    req = ReviveRequest(
        org=ORG,
        idea_id="idea-active-001",
        skill_base_name=SKILL,
        new_pr_number=500,
        new_pr_owner_repo=REPO,
    )
    result = revive(req, store)
    assert result.action == "already_active"


# ---------------------------------------------------------------------------
# 7. test_anti_thrash_bounds_oscillation
# ---------------------------------------------------------------------------


def test_anti_thrash_bounds_oscillation():
    """Anti-thrash: after max_flips in the window, further supersede/revive is blocked."""
    tracker = AntiThrashTracker(thrash_window_secs=3600, max_flips=3)
    idea_id = "idea-thrash-001"

    now = 1_700_000_000_000

    # Record 3 flips (at the ceiling).
    tracker.record_flip(idea_id, now)
    tracker.record_flip(idea_id, now + 1)
    tracker.record_flip(idea_id, now + 2)

    # One more flip should trigger thrash detection.
    assert tracker.is_thrashing(idea_id, now + 3) is True

    # Below the ceiling: not thrashing.
    tracker2 = AntiThrashTracker(thrash_window_secs=3600, max_flips=3)
    tracker2.record_flip(idea_id, now)
    tracker2.record_flip(idea_id, now + 1)
    assert tracker2.is_thrashing(idea_id, now + 2) is False


def test_anti_thrash_blocks_supersede():
    """A thrashing idea cannot be superseded."""
    incumbent = _make_idea("idea-t-001", "Use lazy loading.")
    store = _store_with_idea(incumbent)

    tracker = AntiThrashTracker(thrash_window_secs=3600, max_flips=2)
    now = 1_000_000

    # Pre-fill the tracker to the ceiling.
    tracker.record_flip("idea-t-001", now)
    tracker.record_flip("idea-t-001", now + 1)

    req = _supersede_request("idea-t-001")
    result = supersede(
        req, store,
        unfold_mode="enforce",
        fp_gate=_open_gate(),
        thrash_tracker=tracker,
        now_ms=now + 2,
    )
    assert result.action == "blocked_thrash"
    # Idea unchanged.
    idea = store.get_idea(ORG, SKILL, "idea-t-001")
    assert idea.invalidAt is None


def test_anti_thrash_outside_window_allows():
    """Flips outside the window do not count toward thrash detection."""
    tracker = AntiThrashTracker(thrash_window_secs=3600, max_flips=2)
    idea_id = "idea-window-001"
    # Two flips very far in the past (outside the 1h window).
    old_time = 0
    tracker.record_flip(idea_id, old_time)
    tracker.record_flip(idea_id, old_time + 1)

    # New time is 2 hours later.
    now = old_time + 2 * 3600 * 1000
    assert tracker.is_thrashing(idea_id, now) is False


def test_anti_thrash_blocks_revive():
    """A thrashing idea cannot be revived either."""
    retired = _make_idea("idea-rt-001", "Use eager loading.", invalid_at=1_000)
    store = _store_with_idea(retired)

    tracker = AntiThrashTracker(thrash_window_secs=3600, max_flips=2)
    now = 2_000
    tracker.record_flip("idea-rt-001", now)
    tracker.record_flip("idea-rt-001", now + 1)

    req = ReviveRequest(
        org=ORG,
        idea_id="idea-rt-001",
        skill_base_name=SKILL,
        new_pr_number=600,
        new_pr_owner_repo=REPO,
    )
    result = revive(req, store, thrash_tracker=tracker, now_ms=now + 2)
    assert result.action == "blocked_thrash"


# ---------------------------------------------------------------------------
# 8. test_supersede_fp_calibration_gate_blocks_enforce
# ---------------------------------------------------------------------------


def test_supersede_fp_calibration_gate_blocks_enforce():
    """FP calibration gate blocks enforce when FP rate is too high."""
    incumbent = _make_idea("idea-gate-001", "Use ORM instead of raw SQL.")
    store = _store_with_idea(incumbent)

    # Gate has 10 samples but 3 are FPs → FP rate 0.3 > ceiling 0.1.
    gate = FpCalibrationGate(fp_ceiling=0.10, min_samples=5)
    for i in range(7):
        gate.record_verdict(f"tp{i}", is_false_positive=False)
    for i in range(3):
        gate.record_verdict(f"fp{i}", is_false_positive=True)

    assert not gate.is_open()

    req = _supersede_request("idea-gate-001")
    result = supersede(req, store, unfold_mode="enforce", fp_gate=gate, now_ms=1)
    assert result.action == "blocked_fp_gate"

    # Idea is unchanged.
    idea = store.get_idea(ORG, SKILL, "idea-gate-001")
    assert idea.invalidAt is None


def test_fp_gate_blocks_when_insufficient_samples():
    """FP gate blocks when sample count < min_samples, even if FP rate is 0."""
    gate = FpCalibrationGate(fp_ceiling=0.10, min_samples=30)
    for i in range(5):  # only 5 samples, need 30
        gate.record_verdict(f"tp{i}", is_false_positive=False)

    assert not gate.is_open()

    incumbent = _make_idea("idea-fewsamples-001", "Use dependency injection.")
    store = _store_with_idea(incumbent)
    req = _supersede_request("idea-fewsamples-001")
    result = supersede(req, store, unfold_mode="enforce", fp_gate=gate, now_ms=1)
    assert result.action == "blocked_fp_gate"


def test_fp_gate_allows_when_calibrated():
    """FP gate allows enforce when FP rate < ceiling and >= min_samples."""
    gate = FpCalibrationGate(fp_ceiling=0.10, min_samples=5)
    for i in range(6):
        gate.record_verdict(f"v{i}", is_false_positive=False)

    assert gate.is_open()

    incumbent = _make_idea("idea-ok-001", "Use interfaces over concrete types.")
    store = _store_with_idea(incumbent)
    req = _supersede_request("idea-ok-001")
    result = supersede(req, store, unfold_mode="enforce", fp_gate=gate, now_ms=1)
    assert result.action == "superseded"


def test_shadow_mode_does_not_write():
    """Shadow mode logs the decision but writes nothing."""
    incumbent = _make_idea("idea-shadow-001", "Avoid global state.")
    store = _store_with_idea(incumbent)

    req = _supersede_request("idea-shadow-001")
    result = supersede(req, store, unfold_mode="shadow", now_ms=1)
    assert result.action == "shadow"

    # Idea is unchanged.
    idea = store.get_idea(ORG, SKILL, "idea-shadow-001")
    assert idea.invalidAt is None
    assert idea.supersededBy is None


# ---------------------------------------------------------------------------
# 9. test_deleted_symbol_cleans_stale_anchor
# ---------------------------------------------------------------------------


def test_deleted_symbol_cleans_stale_anchor():
    """A PR that deletes an anchored symbol cleans the stale anchor."""
    store = InMemoryLearningStore()
    # Seed an anchor for file=src/utils.py, symbol=helper_func.
    store.put_anchor(AnchorRecord(
        ideaId="idea-anchor-001",
        ownerRepo=REPO,
        file="src/utils.py",
        symbol="helper_func",
        org=ORG,
        active=True,
    ))

    # Verify it's active.
    active_before = store.get_ideas_by_anchor(ORG, REPO, "src/utils.py", "helper_func")
    assert "idea-anchor-001" in active_before

    req = SymbolDeletionRequest(
        org=ORG,
        owner_repo=REPO,
        deleted_pairs=[("src/utils.py", "helper_func")],
    )
    cleaned = clean_deleted_symbol_anchors(req, store)
    assert "idea-anchor-001" in cleaned

    # Anchor is now inactive — no longer in the locality join.
    active_after = store.get_ideas_by_anchor(ORG, REPO, "src/utils.py", "helper_func")
    assert len(active_after) == 0

    # Anchor record still exists in history (get_anchors_for_idea).
    all_anchors = store.get_anchors_for_idea(ORG, REPO, "idea-anchor-001")
    assert len(all_anchors) == 1
    assert all_anchors[0].active is False


def test_deleted_symbol_no_op_when_no_anchors():
    """Cleaning deleted symbols for a file with no anchors is a no-op."""
    store = InMemoryLearningStore()
    req = SymbolDeletionRequest(
        org=ORG,
        owner_repo=REPO,
        deleted_pairs=[("src/nonexistent.py", "ghost_func")],
    )
    cleaned = clean_deleted_symbol_anchors(req, store)
    assert cleaned == []


def test_deleted_symbol_org_guard():
    """clean_deleted_symbol_anchors raises OrgGuardError on blank org."""
    store = InMemoryLearningStore()
    req = SymbolDeletionRequest(
        org="",
        owner_repo=REPO,
        deleted_pairs=[("src/utils.py", "fn")],
    )
    with pytest.raises(OrgGuardError):
        clean_deleted_symbol_anchors(req, store)


# ---------------------------------------------------------------------------
# Additional coverage: body removal / sibling corruption guard
# ---------------------------------------------------------------------------


def test_remove_lesson_from_body_exact_match():
    """Lesson removal: exact paragraph match is removed cleanly."""
    sibling = "Always add type hints to function signatures."
    lesson = "Use camelCase for all variables."
    body = f"{lesson}\n\n{sibling}"
    result = _remove_lesson_from_body(body, lesson)
    assert sibling in result
    assert "camelCase" not in result


def test_remove_lesson_from_body_jaccard_fallback():
    """Lesson removal: high-overlap paragraph removed when no exact match."""
    sibling = "Write unit tests for public APIs."
    lesson = "Use snake_case identifiers throughout."
    # Slightly rephrased version in the skill body.
    body_lesson = "Use snake_case identifiers in all Python code."
    body = f"{body_lesson}\n\n{sibling}"
    result = _remove_lesson_from_body(body, lesson)
    # The rephrased lesson should be removed (high Jaccard with the original).
    assert sibling in result
    # The rephrased lesson should be gone.
    assert "snake_case" not in result


def test_remove_lesson_leaves_body_unchanged_when_no_match():
    """No match: body returned unchanged (conservative; no corruption)."""
    sibling = "Document all public APIs."
    unrelated_lesson = "Use TypeScript strict mode."
    body = f"Always write docstrings.\n\n{sibling}"
    result = _remove_lesson_from_body(body, unrelated_lesson)
    assert result == body


def test_remove_lesson_handles_empty_inputs():
    """Edge cases: empty body or empty lesson."""
    assert _remove_lesson_from_body("", "lesson") == ""
    assert _remove_lesson_from_body("body", "") == "body"


# ---------------------------------------------------------------------------
# Authority decision unit tests
# ---------------------------------------------------------------------------


def test_decide_winner_authored_over_inferred():
    """authored_import challenger always beats merged incumbent."""
    idea = _make_idea("x", "body", authority="merged")
    winner = decide_winner(idea, "authored_import", 999, incumbent_sources_count=10)
    assert winner == "challenger"


def test_decide_winner_incumbent_higher_authority():
    """user_directive incumbent cannot be beaten by merged challenger."""
    idea = _make_idea("x", "body", authority="user_directive")
    winner = decide_winner(idea, "merged", 999, incumbent_sources_count=1)
    assert winner == "incumbent"


def test_decide_winner_evidence_breaks_tie():
    """Same tier, more evidence: incumbent (3 sources) beats challenger (1)."""
    idea = _make_idea("x", "body", authority="merged")
    winner = decide_winner(idea, "merged", 200, incumbent_sources_count=3)
    assert winner == "incumbent"


def test_decide_winner_recency_breaks_equal_evidence_tie():
    """Same tier, same evidence: challenger (newer PR) wins on recency."""
    idea = _make_idea("x", "body", authority="merged")
    winner = decide_winner(idea, "merged", 200, incumbent_sources_count=1)
    assert winner == "challenger"


# ---------------------------------------------------------------------------
# Verify-event audit log
# ---------------------------------------------------------------------------


def test_supersede_writes_verify_event():
    """supersede() appends a verify event to the audit log."""
    incumbent = _make_idea("idea-audit-001", "Use immutable data structures.")
    store = _store_with_idea(incumbent)

    req = _supersede_request("idea-audit-001", pr_number=777)
    supersede(req, store, unfold_mode="enforce", fp_gate=_open_gate(), now_ms=100)

    events = store.list_verify_events(ORG, "idea-audit-001")
    assert len(events) == 1
    assert events[0].verdict == "supersede"
    assert "777" in (events[0].pr_ref or "")
    assert events[0].authority == "merged"


def test_revive_writes_verify_event():
    """revive() appends a verify event to the audit log."""
    retired = _make_idea("idea-audit-002", "Use eager evaluation.", invalid_at=1_000)
    store = _store_with_idea(retired)

    req = ReviveRequest(
        org=ORG,
        idea_id="idea-audit-002",
        skill_base_name=SKILL,
        new_pr_number=888,
        new_pr_owner_repo=REPO,
    )
    revive(req, store, now_ms=2_000)

    events = store.list_verify_events(ORG, "idea-audit-002")
    assert any(e.verdict == "revive" for e in events)


# ---------------------------------------------------------------------------
# Org guard tests
# ---------------------------------------------------------------------------


def test_supersede_org_guard():
    """supersede() raises OrgGuardError when org is blank."""
    req = SupersedeRequest(
        org="",
        incumbent_idea_id="x",
        incumbent_skill_base_name=SKILL,
        challenger_idea_id=None,
        challenger_skill_base_name=None,
        challenger_pr_number=1,
        challenger_owner_repo=REPO,
        challenger_authority_kind="merged",
    )
    store = InMemoryLearningStore()
    with pytest.raises(OrgGuardError):
        supersede(req, store, unfold_mode="shadow")


def test_revive_org_guard():
    """revive() raises OrgGuardError when org is blank."""
    req = ReviveRequest(
        org="",
        idea_id="x",
        skill_base_name=SKILL,
        new_pr_number=1,
        new_pr_owner_repo=REPO,
    )
    store = InMemoryLearningStore()
    with pytest.raises(OrgGuardError):
        revive(req, store)


# ---------------------------------------------------------------------------
# Full supersede → revive lifecycle
# ---------------------------------------------------------------------------


def test_full_supersede_revive_lifecycle():
    """Full lifecycle: supersede an idea, then revive it on re-corroboration."""
    idea = _make_idea("idea-lifecycle-001", "Use memoization for expensive calls.")
    store = _store_with_idea(idea)

    # Step 1: supersede.
    req = _supersede_request("idea-lifecycle-001", pr_number=100)
    s_result = supersede(req, store, unfold_mode="enforce", fp_gate=_open_gate(), now_ms=1_000)
    assert s_result.action == "superseded"
    assert store.get_idea(ORG, SKILL, "idea-lifecycle-001").invalidAt is not None

    # Step 2: revive.
    rev_req = ReviveRequest(
        org=ORG,
        idea_id="idea-lifecycle-001",
        skill_base_name=SKILL,
        new_pr_number=200,
        new_pr_owner_repo=REPO,
    )
    r_result = revive(rev_req, store, now_ms=2_000)
    assert r_result.action == "revived"

    revived_idea = store.get_idea(ORG, SKILL, "idea-lifecycle-001")
    assert revived_idea.invalidAt is None
    assert revived_idea.revivedAt == 2_000
    assert revived_idea.status == "open"

    # Audit log should show both events.
    events = store.list_verify_events(ORG, "idea-lifecycle-001")
    verdicts = [e.verdict for e in events]
    assert "supersede" in verdicts
    assert "revive" in verdicts
