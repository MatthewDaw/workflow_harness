"""MAT-149 (F2) — Persistence layer: DynamoDB single-table + S3 Vectors + Python key/record builders.

Acceptance checklist (from Linear MAT-149):
  [x] CRUD + conditional writes for each record type (offline test against InMemoryLearningStore)
  [x] S3 Vectors upsert/query (offline test against InMemoryVectorStore)
  [x] test_python_fold_requires_non_blank_org  (org guard)
  [x] current-set reads exclude invalidAt, keep history
  [x] key/record builders match the U10 generated schema

All tests are OFFLINE — no DynamoDB, no S3, no network, no quota.
The InMemoryLearningStore and InMemoryVectorStore carry the full test surface.
"""
from __future__ import annotations

import math

import pytest

from learning_service.db.store import (
    AnchorRecord,
    InMemoryLearningStore,
    InMemoryVectorStore,
    OrgGuardError,
    ProcessedPrRecord,
    QueryHit,
    S3VectorStore,
    VectorItem,
    VersionConflictError,
    VerifyEventRecord,
    skill_vector_key,
)
from learning_service.schema.generated.py_types import (
    GoldenCaseRecord,
    IdeaRecord,
    IdeaSourceRecord,
    anchor_key,
    golden_case_key,
    idea_key,
    idea_source_key,
    processed_pr_key,
    verify_event_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_idea(
    org: str = "acme",
    skill_base_name: str = "my-skill",
    idea_id: str = "idea-001",
    status: str = "open",
    version: int = 0,
    invalid_at: int | None = None,
    authority_kind: str | None = None,
) -> IdeaRecord:
    return IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill_base_name,
        org=org,
        body=f"Insight body for {idea_id}.",
        status=status,
        corroborationVersion=version,
        invalidAt=invalid_at,
        authorityKind=authority_kind,
    )


def _unit_vector(dim: int, idx: int = 0) -> list[float]:
    """A unit vector in dimension `dim` with a 1.0 at position `idx`."""
    v = [0.0] * dim
    v[idx] = 1.0
    return v


# ===========================================================================
# 1. Key/record builders match the U10 generated schema
# ===========================================================================


def test_idea_key_format_matches_schema():
    """idea_key must produce the documented IDEA# SK shape."""
    k = idea_key("acme", "my-skill", "idea-001")
    assert k["PK"] == "SCOPE#org#acme"
    assert k["SK"] == "IDEA#my-skill#idea-001"


def test_anchor_key_format_matches_schema():
    """anchor_key must produce the documented ANCHOR# SK shape."""
    k = anchor_key("acme", "acme/backend", "src/foo.ts", "fooFn", "idea-001")
    assert k["PK"] == "SCOPE#org#acme"
    assert k["SK"] == "ANCHOR#acme/backend#src/foo.ts#fooFn#idea-001"


def test_processed_pr_key_format_matches_schema():
    """processed_pr_key must produce the documented PROCESSED# SK shape."""
    k = processed_pr_key("acme", "acme/backend", 42)
    assert k["PK"] == "SCOPE#org#acme"
    assert k["SK"] == "PROCESSED#acme/backend#42"


def test_verify_event_key_format_matches_schema():
    """verify_event_key must produce the documented VERIFY# SK shape (zero-padded seq)."""
    k = verify_event_key("acme", "idea-001", 3)
    assert k["PK"] == "SCOPE#org#acme"
    assert k["SK"] == "VERIFY#idea-001#000000000003"


def test_golden_case_key_format_matches_schema():
    """golden_case_key must produce the documented IDEAGOLD# SK shape."""
    k = golden_case_key("acme", "my-skill", "idea-001")
    assert k["PK"] == "SCOPE#org#acme"
    assert k["SK"] == "IDEAGOLD#my-skill#idea-001"


def test_skill_vector_key_format():
    """skill_vector_key must produce <org>#<skillBaseName> (mirrors s3vectors.ts)."""
    assert skill_vector_key("acme", "my-skill") == "acme#my-skill"


# ===========================================================================
# 2. test_python_fold_requires_non_blank_org  (the acceptance checklist test)
# ===========================================================================


def test_python_fold_requires_non_blank_org():
    """Every write method must reject a blank org with OrgGuardError."""
    store = InMemoryLearningStore()

    # put_idea
    bad_idea = _make_idea(org="")
    with pytest.raises(OrgGuardError):
        store.put_idea(bad_idea)

    # put_idea_conditional
    with pytest.raises(OrgGuardError):
        store.put_idea_conditional(bad_idea, expected_version=0)

    # put_golden_case
    bad_gc = GoldenCaseRecord(
        caseId="c1", skillBaseName="sk", org="", before="b", after="a", ideaBody="i"
    )
    with pytest.raises(OrgGuardError):
        store.put_golden_case(bad_gc)

    # put_anchor
    bad_anchor = AnchorRecord(
        ideaId="i1", ownerRepo="r/r", file="f.ts", symbol="fn", org="", active=True
    )
    with pytest.raises(OrgGuardError):
        store.put_anchor(bad_anchor)

    # mark_pr_processed
    bad_pr = ProcessedPrRecord(org="", owner_repo="r/r", pr_number=1, processed_at=0)
    with pytest.raises(OrgGuardError):
        store.mark_pr_processed(bad_pr)

    # append_verify_event
    bad_evt = VerifyEventRecord(
        org="", idea_id="i1", seq=1, verdict="corroborate",
        pr_ref=None, authority=None, recorded_at=0
    )
    with pytest.raises(OrgGuardError):
        store.append_verify_event(bad_evt)


def test_org_guard_rejects_whitespace_only_org():
    """A whitespace-only org string must also be rejected."""
    store = InMemoryLearningStore()
    bad = _make_idea(org="   ")
    with pytest.raises(OrgGuardError):
        store.put_idea(bad)


# ===========================================================================
# 3. CRUD for each record type
# ===========================================================================


class TestIdeaCRUD:
    def test_put_and_get_idea(self):
        store = InMemoryLearningStore()
        idea = _make_idea()
        store.put_idea(idea)
        got = store.get_idea("acme", "my-skill", "idea-001")
        assert got is not None
        assert got.ideaId == "idea-001"
        assert got.body == idea.body

    def test_get_idea_returns_none_for_missing(self):
        store = InMemoryLearningStore()
        assert store.get_idea("acme", "my-skill", "idea-999") is None

    def test_put_idea_overwrites_on_second_call(self):
        store = InMemoryLearningStore()
        store.put_idea(_make_idea(status="open"))
        store.put_idea(_make_idea(status="folded", version=1))
        got = store.get_idea("acme", "my-skill", "idea-001")
        assert got is not None
        assert got.status == "folded"

    def test_put_idea_conditional_succeeds_with_correct_version(self):
        store = InMemoryLearningStore()
        idea = _make_idea(version=0)
        store.put_idea(idea)
        updated = _make_idea(status="folded", version=1)
        store.put_idea_conditional(updated, expected_version=0)
        got = store.get_idea("acme", "my-skill", "idea-001")
        assert got is not None
        assert got.status == "folded"

    def test_put_idea_conditional_fails_with_wrong_version(self):
        store = InMemoryLearningStore()
        idea = _make_idea(version=0)
        store.put_idea(idea)
        bad = _make_idea(version=5)
        with pytest.raises(VersionConflictError) as exc_info:
            store.put_idea_conditional(bad, expected_version=5)
        assert exc_info.value.idea_id == "idea-001"
        assert exc_info.value.expected == 5

    def test_conditional_write_with_no_existing_record_uses_sentinel(self):
        """First-write conditional: store sees version=-1 for missing records."""
        store = InMemoryLearningStore()
        idea = _make_idea(version=0)
        # expected_version=-1 matches the sentinel for a missing record.
        store.put_idea_conditional(idea, expected_version=-1)
        got = store.get_idea("acme", "my-skill", "idea-001")
        assert got is not None

    def test_conditional_write_rejects_stale_first_write(self):
        """expected_version=0 fails when the record does not exist (sentinel=-1)."""
        store = InMemoryLearningStore()
        idea = _make_idea(version=0)
        with pytest.raises(VersionConflictError):
            store.put_idea_conditional(idea, expected_version=0)


class TestGoldenCaseCRUD:
    def test_put_and_get_golden_case(self):
        store = InMemoryLearningStore()
        gc = GoldenCaseRecord(
            caseId="idea-001",
            skillBaseName="my-skill",
            org="acme",
            before="old body",
            after="new body",
            ideaBody="the insight",
            createdAt=1234567890000,
        )
        store.put_golden_case(gc)
        got = store.get_golden_case("acme", "my-skill", "idea-001")
        assert got is not None
        assert got.before == "old body"
        assert got.after == "new body"

    def test_get_golden_case_returns_none_for_missing(self):
        store = InMemoryLearningStore()
        assert store.get_golden_case("acme", "my-skill", "nope") is None

    def test_put_golden_case_overwrites_on_refold(self):
        """Re-folding the same idea must overwrite the golden case (not duplicate)."""
        store = InMemoryLearningStore()
        gc1 = GoldenCaseRecord(
            caseId="idea-001", skillBaseName="sk", org="acme",
            before="b1", after="a1", ideaBody="i1"
        )
        gc2 = GoldenCaseRecord(
            caseId="idea-001", skillBaseName="sk", org="acme",
            before="b2", after="a2", ideaBody="i2"
        )
        store.put_golden_case(gc1)
        store.put_golden_case(gc2)
        got = store.get_golden_case("acme", "sk", "idea-001")
        assert got is not None
        assert got.before == "b2"


class TestAnchorCRUD:
    def test_put_anchor_and_lookup_by_anchor(self):
        store = InMemoryLearningStore()
        anchor = AnchorRecord(
            ideaId="idea-001",
            ownerRepo="acme/backend",
            file="src/foo.ts",
            symbol="fooFn",
            org="acme",
            active=True,
        )
        store.put_anchor(anchor)
        hits = store.get_ideas_by_anchor("acme", "acme/backend", "src/foo.ts", "fooFn")
        assert "idea-001" in hits

    def test_inactive_anchor_excluded_from_locality_lookup(self):
        """Retired anchors (active=False) must not appear in get_ideas_by_anchor."""
        store = InMemoryLearningStore()
        anchor = AnchorRecord(
            ideaId="idea-001",
            ownerRepo="acme/backend",
            file="src/foo.ts",
            symbol="fooFn",
            org="acme",
            active=False,
        )
        store.put_anchor(anchor)
        hits = store.get_ideas_by_anchor("acme", "acme/backend", "src/foo.ts", "fooFn")
        assert "idea-001" not in hits

    def test_get_anchors_for_idea(self):
        store = InMemoryLearningStore()
        a1 = AnchorRecord(
            ideaId="idea-001", ownerRepo="acme/backend",
            file="src/a.ts", symbol="aFn", org="acme", active=True
        )
        a2 = AnchorRecord(
            ideaId="idea-001", ownerRepo="acme/backend",
            file="src/b.ts", symbol="bFn", org="acme", active=True
        )
        a3 = AnchorRecord(
            ideaId="idea-002", ownerRepo="acme/backend",
            file="src/c.ts", symbol="cFn", org="acme", active=True
        )
        for a in [a1, a2, a3]:
            store.put_anchor(a)
        anchors = store.get_anchors_for_idea("acme", "acme/backend", "idea-001")
        assert len(anchors) == 2
        files = {a.file for a in anchors}
        assert "src/a.ts" in files and "src/b.ts" in files

    def test_anchor_org_scoped_no_cross_org_collision(self):
        """Anchors from different orgs must not collide in locality lookups."""
        store = InMemoryLearningStore()
        store.put_anchor(AnchorRecord(
            ideaId="idea-001", ownerRepo="acme/backend",
            file="src/foo.ts", symbol="fooFn", org="acme", active=True
        ))
        # Different org — must not appear in the acme lookup.
        store.put_anchor(AnchorRecord(
            ideaId="idea-evil", ownerRepo="acme/backend",
            file="src/foo.ts", symbol="fooFn", org="rival", active=True
        ))
        hits = store.get_ideas_by_anchor("acme", "acme/backend", "src/foo.ts", "fooFn")
        assert "idea-001" in hits
        assert "idea-evil" not in hits


class TestProcessedPrCursor:
    def test_mark_and_check_processed(self):
        store = InMemoryLearningStore()
        rec = ProcessedPrRecord(
            org="acme", owner_repo="acme/backend", pr_number=42, processed_at=1000
        )
        assert not store.is_pr_processed("acme", "acme/backend", 42)
        store.mark_pr_processed(rec)
        assert store.is_pr_processed("acme", "acme/backend", 42)

    def test_different_pr_numbers_are_independent(self):
        store = InMemoryLearningStore()
        store.mark_pr_processed(ProcessedPrRecord(
            org="acme", owner_repo="acme/backend", pr_number=10, processed_at=0
        ))
        assert store.is_pr_processed("acme", "acme/backend", 10)
        assert not store.is_pr_processed("acme", "acme/backend", 11)

    def test_cursor_is_org_scoped(self):
        """A PR processed under org A must not appear as processed under org B."""
        store = InMemoryLearningStore()
        store.mark_pr_processed(ProcessedPrRecord(
            org="acme", owner_repo="acme/backend", pr_number=42, processed_at=0
        ))
        assert not store.is_pr_processed("rival", "acme/backend", 42)


class TestVerifyEventAudit:
    def test_append_and_list_events(self):
        store = InMemoryLearningStore()
        e1 = VerifyEventRecord(
            org="acme", idea_id="idea-001", seq=1,
            verdict="corroborate", pr_ref="acme/backend#10",
            authority="merged", recorded_at=1000
        )
        e2 = VerifyEventRecord(
            org="acme", idea_id="idea-001", seq=2,
            verdict="supersede", pr_ref="acme/backend#20",
            authority="merged", recorded_at=2000
        )
        store.append_verify_event(e1)
        store.append_verify_event(e2)
        events = store.list_verify_events("acme", "idea-001")
        assert len(events) == 2
        assert events[0].verdict == "corroborate"
        assert events[1].verdict == "supersede"

    def test_events_returned_in_seq_order(self):
        store = InMemoryLearningStore()
        for seq in [3, 1, 2]:
            store.append_verify_event(VerifyEventRecord(
                org="acme", idea_id="idea-001", seq=seq,
                verdict="corroborate", pr_ref=None, authority=None, recorded_at=seq * 1000
            ))
        events = store.list_verify_events("acme", "idea-001")
        assert [e.seq for e in events] == [1, 2, 3]

    def test_list_events_returns_empty_for_no_events(self):
        store = InMemoryLearningStore()
        assert store.list_verify_events("acme", "idea-999") == []


# ===========================================================================
# 4. current-set reads exclude invalidAt, keep history
# ===========================================================================


def test_list_current_ideas_excludes_invalid_at():
    """list_current_ideas must exclude ideas where invalidAt is set."""
    store = InMemoryLearningStore()
    active = _make_idea(idea_id="active-idea", invalid_at=None)
    retired = _make_idea(idea_id="retired-idea", invalid_at=1234567890000)
    store.put_idea(active)
    store.put_idea(retired)

    current = store.list_current_ideas("acme", "my-skill")
    ids = {r.ideaId for r in current}
    assert "active-idea" in ids
    assert "retired-idea" not in ids


def test_get_idea_returns_retired_idea_for_history():
    """get_idea must return ideas even when invalidAt is set (full history read)."""
    store = InMemoryLearningStore()
    retired = _make_idea(idea_id="retired-idea", invalid_at=1234567890000)
    store.put_idea(retired)

    got = store.get_idea("acme", "my-skill", "retired-idea")
    assert got is not None
    assert got.invalidAt == 1234567890000


def test_list_all_ideas_for_org_includes_retired():
    """list_all_ideas_for_org must include all ideas regardless of invalidAt."""
    store = InMemoryLearningStore()
    store.put_idea(_make_idea(idea_id="a", skill_base_name="sk1"))
    store.put_idea(_make_idea(idea_id="b", skill_base_name="sk1", invalid_at=999))
    store.put_idea(_make_idea(idea_id="c", skill_base_name="sk2"))

    all_ideas = store.list_all_ideas_for_org("acme")
    ids = {r.ideaId for r in all_ideas}
    assert ids == {"a", "b", "c"}


def test_list_current_ideas_scoped_to_skill():
    """list_current_ideas must only return ideas for the named skill."""
    store = InMemoryLearningStore()
    store.put_idea(_make_idea(idea_id="i1", skill_base_name="skill-a"))
    store.put_idea(_make_idea(idea_id="i2", skill_base_name="skill-b"))

    result = store.list_current_ideas("acme", "skill-a")
    ids = {r.ideaId for r in result}
    assert "i1" in ids
    assert "i2" not in ids


def test_superseded_idea_queryable_as_history_not_deleted():
    """A superseded idea (invalidAt stamped) survives as history, not deleted."""
    store = InMemoryLearningStore()
    idea = _make_idea(idea_id="old-idea")
    store.put_idea(idea)

    # Simulate supersession: stamp invalidAt.
    superseded = _make_idea(idea_id="old-idea", invalid_at=9999999999999, version=1)
    store.put_idea(superseded)

    # Must not appear in the current set.
    current = store.list_current_ideas("acme", "my-skill")
    assert all(r.ideaId != "old-idea" for r in current)

    # Must still be accessible as history.
    hist = store.get_idea("acme", "my-skill", "old-idea")
    assert hist is not None
    assert hist.invalidAt == 9999999999999


# ===========================================================================
# 5. S3 Vectors upsert/query
# ===========================================================================


class TestInMemoryVectorStore:
    def test_put_and_query_returns_correct_hit(self):
        vs = InMemoryVectorStore()
        item = VectorItem(
            key="acme#my-skill",
            vector=_unit_vector(4, idx=0),
            metadata={"org": "acme", "skillBaseName": "my-skill"},
        )
        vs.put_vectors(IDEA_VECTOR_INDEX := "ideas", [item])
        hits = vs.query_top_k("ideas", _unit_vector(4, idx=0), k=5, org_filter="acme")
        assert len(hits) == 1
        assert hits[0].key == "acme#my-skill"
        assert math.isclose(hits[0].score, 1.0, abs_tol=1e-6)

    def test_org_filter_isolates_results(self):
        """Querying with org_filter='acme' must not return vectors for 'rival'."""
        vs = InMemoryVectorStore()
        vs.put_vectors("ideas", [
            VectorItem(key="acme#sk1", vector=_unit_vector(4, 0), metadata={"org": "acme"}),
            VectorItem(key="rival#sk1", vector=_unit_vector(4, 0), metadata={"org": "rival"}),
        ])
        hits = vs.query_top_k("ideas", _unit_vector(4, 0), k=10, org_filter="acme")
        keys = {h.key for h in hits}
        assert "acme#sk1" in keys
        assert "rival#sk1" not in keys

    def test_floor_filters_low_score_hits(self):
        """Hits below the floor must be excluded."""
        vs = InMemoryVectorStore()
        # Two vectors: one aligned (score=1.0), one orthogonal (score=0.0).
        vs.put_vectors("ideas", [
            VectorItem(key="aligned", vector=_unit_vector(4, 0), metadata={"org": "acme"}),
            VectorItem(key="orthogonal", vector=_unit_vector(4, 1), metadata={"org": "acme"}),
        ])
        hits = vs.query_top_k(
            "ideas", _unit_vector(4, 0), k=10,
            org_filter="acme", floor=0.5
        )
        keys = {h.key for h in hits}
        assert "aligned" in keys
        assert "orthogonal" not in keys

    def test_delete_vectors_removes_entry(self):
        vs = InMemoryVectorStore()
        vs.put_vectors("ideas", [
            VectorItem(key="to-delete", vector=_unit_vector(4, 0), metadata={}),
        ])
        vs.delete_vectors("ideas", ["to-delete"])
        hits = vs.query_top_k("ideas", _unit_vector(4, 0), k=5)
        assert all(h.key != "to-delete" for h in hits)

    def test_delete_nonexistent_key_is_noop(self):
        """Deleting a key that does not exist must not raise."""
        vs = InMemoryVectorStore()
        vs.delete_vectors("ideas", ["does-not-exist"])  # must not raise

    def test_get_vectors_returns_metadata(self):
        vs = InMemoryVectorStore()
        vs.put_vectors("skills", [
            VectorItem(key="acme#sk", vector=_unit_vector(4, 0), metadata={"org": "acme", "v": 2}),
        ])
        result = vs.get_vectors("skills", ["acme#sk"])
        assert "acme#sk" in result
        assert result["acme#sk"]["org"] == "acme"

    def test_get_vectors_absent_key_not_in_result(self):
        vs = InMemoryVectorStore()
        result = vs.get_vectors("skills", ["nonexistent"])
        assert "nonexistent" not in result

    def test_top_k_cap_respected(self):
        """query_top_k must return at most k results."""
        vs = InMemoryVectorStore()
        for i in range(10):
            vs.put_vectors("ideas", [
                VectorItem(key=f"vec-{i}", vector=_unit_vector(10, i), metadata={"org": "acme"}),
            ])
        hits = vs.query_top_k("ideas", _unit_vector(10, 0), k=3, org_filter="acme")
        assert len(hits) <= 3

    def test_results_sorted_by_score_descending(self):
        """Hits must be sorted with the highest-score hit first."""
        vs = InMemoryVectorStore()
        # Perfect match and a near-miss.
        q = [0.8, 0.6, 0.0, 0.0]  # not unit, but dot product is deterministic
        vs.put_vectors("ideas", [
            VectorItem(key="exact", vector=[1.0, 0.0, 0.0, 0.0], metadata={"org": "a"}),
            VectorItem(key="near", vector=[0.0, 1.0, 0.0, 0.0], metadata={"org": "a"}),
        ])
        hits = vs.query_top_k("ideas", q, k=10, org_filter="a")
        # exact dot q = 0.8, near dot q = 0.6 → exact must come first.
        assert hits[0].key == "exact"

    def test_put_vectors_upserts(self):
        """A second put_vectors with the same key must overwrite the vector."""
        vs = InMemoryVectorStore()
        vs.put_vectors("ideas", [
            VectorItem(key="k", vector=_unit_vector(4, 0), metadata={"v": 1}),
        ])
        vs.put_vectors("ideas", [
            VectorItem(key="k", vector=_unit_vector(4, 1), metadata={"v": 2}),
        ])
        result = vs.get_vectors("ideas", ["k"])
        assert result["k"]["v"] == 2


# ===========================================================================
# 6. Cross-org isolation for ideas
# ===========================================================================


def test_list_current_ideas_does_not_leak_across_orgs():
    """Ideas from org B must not appear in org A's current set."""
    store = InMemoryLearningStore()
    store.put_idea(_make_idea(org="acme", idea_id="acme-idea"))
    store.put_idea(_make_idea(org="rival", idea_id="rival-idea"))

    acme_ideas = store.list_current_ideas("acme", "my-skill")
    ids = {r.ideaId for r in acme_ideas}
    assert "acme-idea" in ids
    assert "rival-idea" not in ids


def test_get_idea_scoped_to_org():
    """get_idea for org A must not return org B's idea even with the same idea_id."""
    store = InMemoryLearningStore()
    store.put_idea(_make_idea(org="acme", idea_id="shared-id"))
    # rival does NOT have this idea.
    got = store.get_idea("rival", "my-skill", "shared-id")
    assert got is None


# ===========================================================================
# 7. Multi-anchor fan-out (locality join correctness)
# ===========================================================================


def test_multiple_anchors_for_same_idea_all_queryable():
    """An idea with 3 anchors must surface from all 3 anchor lookups."""
    store = InMemoryLearningStore()
    for sym in ["fnA", "fnB", "fnC"]:
        store.put_anchor(AnchorRecord(
            ideaId="idea-multi", ownerRepo="acme/backend",
            file="src/foo.ts", symbol=sym, org="acme", active=True
        ))

    for sym in ["fnA", "fnB", "fnC"]:
        hits = store.get_ideas_by_anchor("acme", "acme/backend", "src/foo.ts", sym)
        assert "idea-multi" in hits, f"Expected idea-multi at symbol {sym}"


def test_deactivated_anchor_no_longer_surfaces_in_locality_join():
    """An anchor retired with active=False must be excluded from the locality join.

    put_anchor behaves as a DynamoDB-style upsert: a second write with the same
    (org, ownerRepo, file, symbol, ideaId) key REPLACES the first record — it
    does not append alongside it.  So after writing active=False, the record is
    inactive and must not appear in the active-only locality join.
    """
    store = InMemoryLearningStore()
    # Write an active anchor.
    store.put_anchor(AnchorRecord(
        ideaId="idea-001", ownerRepo="acme/backend",
        file="src/foo.ts", symbol="fn", org="acme", active=True
    ))
    # Verify it's active before retirement.
    assert "idea-001" in store.get_ideas_by_anchor("acme", "acme/backend", "src/foo.ts", "fn")

    # Retire by writing the same key with active=False (upsert — replaces the record).
    store.put_anchor(AnchorRecord(
        ideaId="idea-001", ownerRepo="acme/backend",
        file="src/foo.ts", symbol="fn", org="acme", active=False
    ))

    # After retirement the locality join must return empty (upsert replaced the
    # active record — only one record exists in the store, and it is inactive).
    result_ids = store.get_ideas_by_anchor("acme", "acme/backend", "src/foo.ts", "fn")
    assert "idea-001" not in result_ids, (
        "Retired anchor (active=False) must not appear in the active locality join"
    )

    # The record still exists in history (get_anchors_for_idea returns it).
    history = store.get_anchors_for_idea("acme", "acme/backend", "idea-001")
    assert len(history) == 1 and history[0].active is False


# ===========================================================================
# 8. IdeaRecord with authored/authority fields
# ===========================================================================


def test_idea_record_with_authority_kind():
    """IdeaRecord must store and retrieve authorityKind correctly."""
    store = InMemoryLearningStore()
    idea = _make_idea(authority_kind="user_directive")
    store.put_idea(idea)
    got = store.get_idea("acme", "my-skill", "idea-001")
    assert got is not None
    assert got.authorityKind == "user_directive"


def test_idea_record_round_trips_all_gap6_fields_in_memory():
    """The U10 (Gap-6) fields survive an in-memory put/get round-trip."""
    store = InMemoryLearningStore()
    rec = IdeaRecord(
        ideaId="rich",
        skillBaseName="my-skill",
        org="acme",
        body="b",
        status="folded",
        corroborationVersion=2,
        refines="parent",
        revivedAt=123,
        legacyRecurrenceFold=True,
        sourceRef='{"kind":"authored_import"}',
        scopeTag="repo",
        authorId="dev-7",
        verificationRung="test",
    )
    store.put_idea(rec)
    got = store.get_idea("acme", "my-skill", "rich")
    assert got is not None
    assert got.refines == "parent"
    assert got.revivedAt == 123
    assert got.legacyRecurrenceFold is True
    assert got.sourceRef == '{"kind":"authored_import"}'
    assert got.scopeTag == "repo"
    assert got.authorId == "dev-7"
    assert got.verificationRung == "test"


# ===========================================================================
# 9. IdeaSource record CRUD (the new per-PR contribution record)
# ===========================================================================


def test_idea_source_key_format_matches_schema():
    k = idea_source_key("acme", "idea-001", "acme/backend#10")
    assert k["PK"] == "SCOPE#org#acme"
    assert k["SK"] == "IDEASRC#idea-001#acme/backend#10"


class TestIdeaSourceCRUD:
    def test_put_get_and_list(self):
        store = InMemoryLearningStore()
        s1 = IdeaSourceRecord(
            ideaId="idea-001", sourceId="r#10", org="acme",
            prRef="r#10", authorityKind="merged", verificationRung="test",
        )
        s2 = IdeaSourceRecord(
            ideaId="idea-001", sourceId="r#20", org="acme", prRef="r#20",
        )
        store.put_idea_source(s1)
        store.put_idea_source(s2)
        got = store.get_idea_source("acme", "idea-001", "r#10")
        assert got is not None and got.verificationRung == "test"
        sources = store.list_idea_sources("acme", "idea-001")
        assert {s.sourceId for s in sources} == {"r#10", "r#20"}

    def test_org_guard(self):
        store = InMemoryLearningStore()
        with pytest.raises(OrgGuardError):
            store.put_idea_source(IdeaSourceRecord(ideaId="i", sourceId="s", org=""))

    def test_requires_identity(self):
        store = InMemoryLearningStore()
        with pytest.raises(ValueError):
            store.put_idea_source(IdeaSourceRecord(org="acme"))

    def test_scoped_per_idea(self):
        store = InMemoryLearningStore()
        store.put_idea_source(IdeaSourceRecord(ideaId="A", sourceId="r#1", org="acme"))
        store.put_idea_source(IdeaSourceRecord(ideaId="B", sourceId="r#2", org="acme"))
        assert {s.sourceId for s in store.list_idea_sources("acme", "A")} == {"r#1"}


def test_authored_idea_is_never_filtered_by_invalid_at_logic():
    """An authored idea with no invalidAt must appear in the current set."""
    store = InMemoryLearningStore()
    authored = IdeaRecord(
        ideaId="directive-001",
        skillBaseName="my-skill",
        org="acme",
        body="Always use snake_case.",
        status="open",
        corroborationVersion=0,
        authored=True,
        authorityKind="user_directive",
    )
    store.put_idea(authored)
    current = store.list_current_ideas("acme", "my-skill")
    ids = {r.ideaId for r in current}
    assert "directive-001" in ids
