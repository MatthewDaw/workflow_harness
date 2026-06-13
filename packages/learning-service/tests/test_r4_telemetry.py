"""test_r4_telemetry.py — MAT-153: Shadow→enforce calibration metrics (R4).

Tests for the telemetry.py module:
  - TelemetryAccumulator + TelemetrySnapshot
  - gate_status() enforce-gate logic
  - compute_org_metrics() from a real InMemoryLearningStore
  - to_json_dict() serialisation

All tests use in-memory stores — zero live AWS, zero NLI model load.

Acceptance checklist (MAT-153):
  [x] Each metric emitted per skill/org
  [x] unfold_mode/supersede_mode/verified_learning_mode enforce gates read these
  [x] dashboard/queries available (compute_org_metrics + to_json_dict)
"""
from __future__ import annotations

import pytest

from learning_service.telemetry import (
    AnchorResolutionMetrics,
    AuthoredInferredMetrics,
    CorroborationWeightMetrics,
    GateConfig,
    GateStatus,
    NliMoveMetrics,
    SupersedeMetrics,
    TelemetryAccumulator,
    TelemetrySnapshot,
    compute_org_metrics,
    gate_status,
    to_json_dict,
)
from learning_service.db.store import InMemoryLearningStore, VerifyEventRecord
from learning_service.schema.generated.py_types import IdeaRecord, IdeaSourceRecord
from learning_service.corroboration import AuthorCredibilityStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_idea(
    idea_id: str,
    org: str = "acme",
    skill: str = "style",
    authority_kind: str = "merged",
    *,
    authored: bool = False,
    invalid_at: int | None = None,
    superseded_by: str | None = None,
) -> IdeaRecord:
    return IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill,
        org=org,
        body=f"Body of {idea_id}",
        status="folded",
        corroborationVersion=0,
        authorityKind=authority_kind,
        authored=authored,
        invalidAt=invalid_at,
        supersededBy=superseded_by,
    )


def _make_source(
    idea_id: str,
    pr_number: int,
    org: str = "acme",
    rung: str = "normal",
    author_id: str = "alice",
    owner_repo: str = "acme/backend",
) -> IdeaSourceRecord:
    return IdeaSourceRecord(
        ideaId=idea_id,
        sourceId=f"pr#{pr_number}",
        org=org,
        prRef=f"{owner_repo}#{pr_number}",
        authorityKind="merged",
        verificationRung=rung,
        authorId=author_id,
    )


# ---------------------------------------------------------------------------
# TelemetryAccumulator — basic recording
# ---------------------------------------------------------------------------


class TestTelemetryAccumulator:
    def test_empty_snapshot_has_zero_counts(self):
        acc = TelemetryAccumulator()
        snap = acc.snapshot()
        assert snap.corroboration.weights == ()
        assert snap.anchor.symbol_count == 0
        assert snap.anchor.file_fallback_count == 0
        assert snap.supersede.supersede_event_count == 0
        assert snap.supersede.unfold_count == 0
        assert snap.revive_count == 0
        assert snap.necessity_demotion_count == 0
        assert snap.nli.total == 0
        assert snap.authored_inferred.total == 0

    def test_record_corroboration_weight_accumulates(self):
        acc = TelemetryAccumulator()
        acc.record_corroboration_weight(0.6, distinct_pr_count=1)
        acc.record_corroboration_weight(1.0, distinct_pr_count=2)
        snap = acc.snapshot()
        assert snap.corroboration.weights == (0.6, 1.0)
        assert snap.corroboration.distinct_pr_counts == (1, 2)
        assert pytest.approx(snap.corroboration.mean_weight) == 0.8
        assert snap.corroboration.max_weight == 1.0
        assert pytest.approx(snap.corroboration.mean_distinct_prs) == 1.5

    def test_record_author_credibility(self):
        acc = TelemetryAccumulator()
        acc.record_author_credibility("alice", 0.8)
        acc.record_author_credibility("bob", 0.5)
        snap = acc.snapshot()
        cmap = snap.author_credibility_map()
        assert cmap["alice"] == 0.8
        assert cmap["bob"] == 0.5

    def test_author_credibility_is_per_author(self):
        """Per-author credibility is emitted as a distinct key per coder."""
        acc = TelemetryAccumulator()
        acc.record_author_credibility("alice", 0.9)
        acc.record_author_credibility("carol", 0.4)
        snap = acc.snapshot()
        cmap = snap.author_credibility_map()
        assert len(cmap) == 2
        assert "alice" in cmap
        assert "carol" in cmap

    def test_record_anchor_resolution_symbol(self):
        acc = TelemetryAccumulator()
        acc.record_anchor_resolution(resolved_as_symbol=True)
        acc.record_anchor_resolution(resolved_as_symbol=True)
        acc.record_anchor_resolution(resolved_as_symbol=False)
        snap = acc.snapshot()
        assert snap.anchor.symbol_count == 2
        assert snap.anchor.file_fallback_count == 1
        assert snap.anchor.total == 3
        assert pytest.approx(snap.anchor.symbol_resolution_rate) == 2 / 3

    def test_symbol_vs_file_anchor_resolution_rate_zero_when_no_samples(self):
        acc = TelemetryAccumulator()
        snap = acc.snapshot()
        assert snap.anchor.symbol_resolution_rate == 0.0

    def test_record_supersede_event_with_unfold(self):
        acc = TelemetryAccumulator()
        acc.record_supersede_event(unfold_executed=True)
        acc.record_supersede_event(unfold_executed=False)
        snap = acc.snapshot()
        assert snap.supersede.supersede_event_count == 2
        assert snap.supersede.unfold_count == 1

    def test_supersede_fp_rate_zero_no_samples(self):
        acc = TelemetryAccumulator()
        snap = acc.snapshot()
        assert snap.supersede.supersede_fp_sample_count == 0
        assert snap.supersede.supersede_fp_rate == 0.0

    def test_supersede_fp_rate_computed_correctly(self):
        """supersede_fp_rate = confirmed FPs / total spot-checked verdicts."""
        acc = TelemetryAccumulator()
        acc.record_supersede_fp_check("v001", is_false_positive=False)
        acc.record_supersede_fp_check("v002", is_false_positive=False)
        acc.record_supersede_fp_check("v003", is_false_positive=True)  # FP
        snap = acc.snapshot()
        assert snap.supersede.supersede_fp_sample_count == 3
        assert pytest.approx(snap.supersede.supersede_fp_rate) == 1 / 3

    def test_supersede_fp_rate_all_true_positives(self):
        acc = TelemetryAccumulator()
        for i in range(5):
            acc.record_supersede_fp_check(f"v{i:03d}", is_false_positive=False)
        snap = acc.snapshot()
        assert snap.supersede.supersede_fp_rate == 0.0

    def test_record_revive_and_necessity_demotion(self):
        acc = TelemetryAccumulator()
        acc.record_revive()
        acc.record_revive()
        acc.record_necessity_demotion()
        snap = acc.snapshot()
        assert snap.revive_count == 2
        assert snap.necessity_demotion_count == 1

    def test_nli_move_distribution_all_verdicts(self):
        acc = TelemetryAccumulator()
        acc.record_nli_verdict("corroborate")
        acc.record_nli_verdict("corroborate")
        acc.record_nli_verdict("supersede")
        acc.record_nli_verdict("refine")
        acc.record_nli_verdict("neutral")
        snap = acc.snapshot()
        assert snap.nli.corroborate == 2
        assert snap.nli.supersede == 1
        assert snap.nli.refine == 1
        assert snap.nli.neutral == 1
        assert snap.nli.total == 5

    def test_nli_distribution_fractions(self):
        acc = TelemetryAccumulator()
        acc.record_nli_verdict("corroborate")
        acc.record_nli_verdict("supersede")
        acc.record_nli_verdict("neutral")
        acc.record_nli_verdict("neutral")
        snap = acc.snapshot()
        dist = snap.nli.distribution()
        assert pytest.approx(dist["corroborate"]) == 0.25
        assert pytest.approx(dist["supersede"]) == 0.25
        assert pytest.approx(dist["neutral"]) == 0.5
        assert pytest.approx(dist["refine"]) == 0.0

    def test_judge_fallback_rate_captured(self):
        """Judge-fallback count tracks low-confidence NLI calls routed to judge."""
        acc = TelemetryAccumulator()
        acc.record_nli_verdict("refine", low_confidence=True)   # → fallback
        acc.record_nli_verdict("corroborate", low_confidence=False)
        acc.record_nli_verdict("refine", low_confidence=True)   # → fallback
        snap = acc.snapshot()
        assert snap.nli.judge_fallback_count == 2
        assert pytest.approx(snap.nli.judge_fallback_rate) == 2 / 3

    def test_authored_inferred_ratio(self):
        acc = TelemetryAccumulator()
        acc.record_idea_authority("user_directive")
        acc.record_idea_authority("authored_import")
        acc.record_idea_authority("merged")
        acc.record_idea_authority("merged")
        snap = acc.snapshot()
        assert snap.authored_inferred.authored_count == 2
        assert snap.authored_inferred.inferred_count == 2
        assert snap.authored_inferred.total == 4
        assert pytest.approx(snap.authored_inferred.authored_inferred_ratio) == 0.5

    def test_authored_inferred_ratio_zero_when_empty(self):
        acc = TelemetryAccumulator()
        snap = acc.snapshot()
        assert snap.authored_inferred.authored_inferred_ratio == 0.0

    def test_reset_clears_all_counts(self):
        acc = TelemetryAccumulator()
        acc.record_corroboration_weight(1.0)
        acc.record_anchor_resolution(resolved_as_symbol=True)
        acc.record_supersede_event(unfold_executed=True)
        acc.record_revive()
        acc.record_nli_verdict("corroborate")
        acc.record_idea_authority("merged")
        acc.reset()
        snap = acc.snapshot()
        assert snap.corroboration.weights == ()
        assert snap.anchor.total == 0
        assert snap.supersede.supersede_event_count == 0
        assert snap.revive_count == 0
        assert snap.nli.total == 0


# ---------------------------------------------------------------------------
# gate_status() — enforce-gate logic reads the telemetry
# ---------------------------------------------------------------------------


class TestGateStatus:
    def _snap(
        self,
        *,
        symbol: int = 0,
        file_fb: int = 0,
        fp_samples: int = 0,
        fp_rate: float = 0.0,
    ) -> TelemetrySnapshot:
        """Build a minimal snapshot with the given anchor and FP values."""
        return TelemetrySnapshot(
            anchor=AnchorResolutionMetrics(
                symbol_count=symbol,
                file_fallback_count=file_fb,
            ),
            supersede=SupersedeMetrics(
                supersede_fp_sample_count=fp_samples,
                supersede_fp_rate=fp_rate,
            ),
        )

    def test_verified_learning_gate_open_when_enough_anchor_samples(self):
        cfg = GateConfig(min_anchor_samples=10)
        snap = self._snap(symbol=8, file_fb=4)   # total=12 >= 10
        gs = gate_status(snap, config=cfg)
        assert gs.verified_learning_gate_open is True
        assert "symbol_resolution_rate" in gs.verified_learning_reason

    def test_verified_learning_gate_blocked_insufficient_samples(self):
        cfg = GateConfig(min_anchor_samples=10)
        snap = self._snap(symbol=3, file_fb=2)   # total=5 < 10
        gs = gate_status(snap, config=cfg)
        assert gs.verified_learning_gate_open is False
        assert "need" in gs.verified_learning_reason.lower() or "5" in gs.verified_learning_reason

    def test_supersede_gate_open_low_fp_enough_samples(self):
        cfg = GateConfig(supersede_fp_ceiling=0.10, supersede_fp_min_samples=30)
        snap = self._snap(fp_samples=30, fp_rate=0.05)  # 5% < 10%
        gs = gate_status(snap, config=cfg)
        assert gs.supersede_gate_open is True

    def test_supersede_gate_blocked_fp_too_high(self):
        cfg = GateConfig(supersede_fp_ceiling=0.10, supersede_fp_min_samples=30)
        snap = self._snap(fp_samples=30, fp_rate=0.15)  # 15% >= 10%
        gs = gate_status(snap, config=cfg)
        assert gs.supersede_gate_open is False
        assert "too high" in gs.supersede_reason.lower() or "ceiling" in gs.supersede_reason.lower()

    def test_supersede_gate_blocked_insufficient_samples(self):
        cfg = GateConfig(supersede_fp_ceiling=0.10, supersede_fp_min_samples=30)
        snap = self._snap(fp_samples=10, fp_rate=0.0)   # only 10 < 30 samples
        gs = gate_status(snap, config=cfg)
        assert gs.supersede_gate_open is False

    def test_unfold_gate_mirrors_supersede_gate(self):
        """unfold_mode gate depends on the supersede gate passing."""
        cfg = GateConfig(supersede_fp_ceiling=0.10, supersede_fp_min_samples=30)

        # Both open when supersede gate open.
        snap_open = self._snap(fp_samples=35, fp_rate=0.04)
        gs_open = gate_status(snap_open, config=cfg)
        assert gs_open.supersede_gate_open is True
        assert gs_open.unfold_gate_open is True

        # Unfold blocked when supersede gate blocked.
        snap_blocked = self._snap(fp_samples=5, fp_rate=0.20)
        gs_blocked = gate_status(snap_blocked, config=cfg)
        assert gs_blocked.supersede_gate_open is False
        assert gs_blocked.unfold_gate_open is False
        assert "supersede gate not open" in gs_blocked.unfold_reason.lower()

    def test_supersede_fp_calibration_gate_blocks_enforce_below_ceiling(self):
        """Mirrors the plan: FP rate must be < ceiling before enforce is allowed."""
        cfg = GateConfig(supersede_fp_ceiling=0.10, supersede_fp_min_samples=30)

        # Exactly at ceiling (not strictly below) — gate blocked.
        snap_at = self._snap(fp_samples=30, fp_rate=0.10)
        gs_at = gate_status(snap_at, config=cfg)
        assert gs_at.supersede_gate_open is False

        # Strictly below ceiling — gate open.
        snap_below = self._snap(fp_samples=30, fp_rate=0.09)
        gs_below = gate_status(snap_below, config=cfg)
        assert gs_below.supersede_gate_open is True

    def test_gate_status_with_default_config(self):
        """gate_status works with no config arg (uses default GateConfig)."""
        snap = TelemetrySnapshot()
        gs = gate_status(snap)
        # With zero samples, both gates should be blocked.
        assert gs.verified_learning_gate_open is False
        assert gs.supersede_gate_open is False
        assert gs.unfold_gate_open is False

    def test_gate_reason_strings_are_populated(self):
        snap = TelemetrySnapshot()
        gs = gate_status(snap)
        assert gs.verified_learning_reason != ""
        assert gs.supersede_reason != ""
        assert gs.unfold_reason != ""


# ---------------------------------------------------------------------------
# compute_org_metrics() — derived from InMemoryLearningStore
# ---------------------------------------------------------------------------


class TestComputeOrgMetrics:
    def _store_with_ideas(self) -> tuple[InMemoryLearningStore, AuthorCredibilityStore]:
        store = InMemoryLearningStore()
        cred = AuthorCredibilityStore({"alice": 0.9, "bob": 0.5})

        # Idea 1: inferred, folded, not retired.
        i1 = _make_idea("idea-1", authority_kind="merged")
        store.put_idea(i1)
        store.put_idea_source(_make_source("idea-1", pr_number=10, rung="test", author_id="alice"))

        # Idea 2: inferred, open, not retired.
        i2 = _make_idea("idea-2", authority_kind="merged")
        store.put_idea(i2)
        store.put_idea_source(_make_source("idea-2", pr_number=20, rung="normal", author_id="bob"))

        # Idea 3: authored (user directive), never retired.
        i3 = _make_idea("idea-3", authority_kind="user_directive", authored=True)
        store.put_idea(i3)

        # Idea 4: inferred, superseded (invalidAt set, has supersededBy).
        i4 = _make_idea("idea-4", authority_kind="merged", invalid_at=1234, superseded_by="idea-2")
        store.put_idea(i4)
        store.put_idea_source(_make_source("idea-4", pr_number=5, rung="bare", author_id="bob"))

        return store, cred

    def test_idea_count_correct(self):
        store, cred = self._store_with_ideas()
        m = compute_org_metrics("acme", "style", store, credibility_store=cred)
        assert m["idea_count"] == 4

    def test_authored_inferred_ratio_correct(self):
        store, cred = self._store_with_ideas()
        m = compute_org_metrics("acme", "style", store, credibility_store=cred)
        # 1 authored (idea-3), 3 inferred (idea-1, idea-2, idea-4)
        assert m["authored_count"] == 1
        assert m["inferred_count"] == 3
        assert pytest.approx(m["authored_inferred_ratio"]) == 1 / 4

    def test_corroboration_weight_distribution_emitted(self):
        store, cred = self._store_with_ideas()
        m = compute_org_metrics("acme", "style", store, credibility_store=cred)
        assert "corroboration_weight_distribution" in m
        assert "corroboration_weight_mean" in m
        assert "corroboration_weight_max" in m
        # idea-1 has a test rung (1.0) × credibility(alice=0.9) = 0.9
        # idea-2 has normal rung (0.6) × credibility(bob=0.5)  = 0.3
        # idea-3 authored: no prRef → weight 0.0
        # idea-4 bare rung (0.4) × credibility(bob=0.5)       = 0.2
        dist = m["corroboration_weight_distribution"]
        assert len(dist) == 4

    def test_distinct_pr_distribution_emitted(self):
        store, cred = self._store_with_ideas()
        m = compute_org_metrics("acme", "style", store, credibility_store=cred)
        assert "distinct_pr_distribution" in m
        assert "distinct_pr_count_mean" in m
        dist = m["distinct_pr_distribution"]
        assert len(dist) == 4

    def test_per_author_credibility_emitted(self):
        store, cred = self._store_with_ideas()
        m = compute_org_metrics("acme", "style", store, credibility_store=cred)
        pac = m["per_author_credibility"]
        assert isinstance(pac, dict)
        # alice and bob should appear (they have sources)
        assert "alice" in pac
        assert "bob" in pac
        assert pac["alice"] == pytest.approx(0.9)
        assert pac["bob"] == pytest.approx(0.5)

    def test_supersede_event_count_from_verify_events(self):
        store, cred = self._store_with_ideas()
        # Append a supersede verify event for idea-4.
        store.append_verify_event(VerifyEventRecord(
            org="acme",
            idea_id="idea-4",
            seq=0,
            verdict="supersede",
            pr_ref="acme/backend#30",
            authority="merged",
            recorded_at=9999,
        ))
        m = compute_org_metrics("acme", "style", store, credibility_store=cred)
        assert m["supersede_event_count"] >= 1

    def test_revive_count_from_verify_events(self):
        store, cred = self._store_with_ideas()
        store.append_verify_event(VerifyEventRecord(
            org="acme",
            idea_id="idea-1",
            seq=0,
            verdict="revive",
            pr_ref="acme/backend#40",
            authority="merged",
            recorded_at=10000,
        ))
        m = compute_org_metrics("acme", "style", store, credibility_store=cred)
        assert m["revive_count"] == 1

    def test_necessity_demotion_count_ideas_with_invalid_at_no_superseded_by(self):
        store = InMemoryLearningStore()
        # A necessity-demoted idea: invalidAt set but no supersededBy.
        i = _make_idea("idea-d", authority_kind="merged", invalid_at=8888)
        store.put_idea(i)
        m = compute_org_metrics("acme", "style", store)
        assert m["necessity_demotion_count"] == 1

    def test_superseded_idea_not_counted_as_necessity_demotion(self):
        store = InMemoryLearningStore()
        # Superseded: both invalidAt and supersededBy set.
        i = _make_idea("idea-s", authority_kind="merged", invalid_at=7777, superseded_by="idea-x")
        store.put_idea(i)
        m = compute_org_metrics("acme", "style", store)
        assert m["necessity_demotion_count"] == 0

    def test_metrics_scoped_to_skill_family(self):
        """Metrics for skill 'A' do not bleed into skill 'B'."""
        store = InMemoryLearningStore()
        iA = _make_idea("idea-A", skill="skill-A", authority_kind="merged")
        iB = _make_idea("idea-B", skill="skill-B", authority_kind="user_directive", authored=True)
        store.put_idea(iA)
        store.put_idea(iB)

        mA = compute_org_metrics("acme", "skill-A", store)
        mB = compute_org_metrics("acme", "skill-B", store)

        assert mA["idea_count"] == 1
        assert mA["authored_count"] == 0
        assert mB["idea_count"] == 1
        assert mB["authored_count"] == 1

    def test_metrics_returns_all_expected_keys(self):
        """All R4 metric keys are present in the returned dict."""
        store = InMemoryLearningStore()
        m = compute_org_metrics("acme", "style", store)
        required = {
            "org",
            "skill_base_name",
            "idea_count",
            "corroboration_weight_mean",
            "corroboration_weight_max",
            "corroboration_weight_distribution",
            "distinct_pr_count_mean",
            "distinct_pr_distribution",
            "per_author_credibility",
            "supersede_event_count",
            "unfold_count",
            "revive_count",
            "necessity_demotion_count",
            "authored_count",
            "inferred_count",
            "authored_inferred_ratio",
        }
        assert required.issubset(m.keys())

    def test_empty_skill_returns_zero_counts(self):
        """An org with no ideas returns zeros everywhere (no exceptions)."""
        store = InMemoryLearningStore()
        m = compute_org_metrics("acme", "style", store)
        assert m["idea_count"] == 0
        assert m["authored_count"] == 0
        assert m["inferred_count"] == 0
        assert m["authored_inferred_ratio"] == 0.0
        assert m["supersede_event_count"] == 0
        assert m["revive_count"] == 0
        assert m["necessity_demotion_count"] == 0


# ---------------------------------------------------------------------------
# to_json_dict() — serialisation for dashboard / logging
# ---------------------------------------------------------------------------


class TestToJsonDict:
    def _full_snapshot(self) -> TelemetrySnapshot:
        acc = TelemetryAccumulator()
        acc.record_corroboration_weight(0.6, distinct_pr_count=1)
        acc.record_corroboration_weight(1.2, distinct_pr_count=3)
        acc.record_author_credibility("alice", 0.9)
        acc.record_anchor_resolution(resolved_as_symbol=True)
        acc.record_anchor_resolution(resolved_as_symbol=True)
        acc.record_anchor_resolution(resolved_as_symbol=False)
        acc.record_supersede_event(unfold_executed=True)
        acc.record_supersede_event(unfold_executed=False)
        acc.record_supersede_fp_check("v001", is_false_positive=False)
        acc.record_supersede_fp_check("v002", is_false_positive=True)
        acc.record_revive()
        acc.record_necessity_demotion()
        acc.record_necessity_demotion()
        acc.record_nli_verdict("corroborate")
        acc.record_nli_verdict("supersede")
        acc.record_nli_verdict("refine", low_confidence=True)
        acc.record_nli_verdict("neutral")
        acc.record_idea_authority("user_directive")
        acc.record_idea_authority("merged")
        return acc.snapshot()

    def test_to_json_dict_contains_all_metric_groups(self):
        snap = self._full_snapshot()
        d = to_json_dict(snap)
        assert "corroboration" in d
        assert "anchor" in d
        assert "supersede" in d
        assert "revive_count" in d
        assert "necessity_demotion_count" in d
        assert "nli" in d
        assert "authored_inferred" in d
        assert "per_author_credibility" in d

    def test_corroboration_group_correct(self):
        snap = self._full_snapshot()
        d = to_json_dict(snap)
        c = d["corroboration"]
        assert c["weight_distribution"] == [0.6, 1.2]
        assert pytest.approx(c["mean_weight"]) == 0.9
        assert c["max_weight"] == 1.2
        assert c["distinct_pr_distribution"] == [1, 3]
        assert pytest.approx(c["mean_distinct_prs"]) == 2.0

    def test_anchor_group_correct(self):
        snap = self._full_snapshot()
        d = to_json_dict(snap)
        a = d["anchor"]
        assert a["symbol_count"] == 2
        assert a["file_fallback_count"] == 1
        assert a["total"] == 3
        assert pytest.approx(a["symbol_resolution_rate"]) == 2 / 3

    def test_supersede_group_correct(self):
        snap = self._full_snapshot()
        d = to_json_dict(snap)
        s = d["supersede"]
        assert s["supersede_event_count"] == 2
        assert s["unfold_count"] == 1
        assert s["supersede_fp_sample_count"] == 2
        # 1 FP out of 2 spot checks → 50% FP rate
        assert pytest.approx(s["supersede_fp_rate"]) == 0.5

    def test_nli_group_correct(self):
        snap = self._full_snapshot()
        d = to_json_dict(snap)
        n = d["nli"]
        assert n["corroborate"] == 1
        assert n["supersede"] == 1
        assert n["refine"] == 1
        assert n["neutral"] == 1
        assert n["total"] == 4
        assert n["judge_fallback_count"] == 1
        assert pytest.approx(n["judge_fallback_rate"]) == 0.25
        dist = n["distribution"]
        assert pytest.approx(dist["corroborate"]) == 0.25
        assert pytest.approx(dist["supersede"]) == 0.25

    def test_authored_inferred_group_correct(self):
        snap = self._full_snapshot()
        d = to_json_dict(snap)
        ai = d["authored_inferred"]
        assert ai["authored_count"] == 1
        assert ai["inferred_count"] == 1
        assert ai["total"] == 2
        assert pytest.approx(ai["authored_inferred_ratio"]) == 0.5

    def test_per_author_credibility_in_output(self):
        snap = self._full_snapshot()
        d = to_json_dict(snap)
        assert d["per_author_credibility"]["alice"] == 0.9

    def test_revive_and_necessity_demotion_counts(self):
        snap = self._full_snapshot()
        d = to_json_dict(snap)
        assert d["revive_count"] == 1
        assert d["necessity_demotion_count"] == 2

    def test_json_dict_is_json_serialisable(self):
        """The output of to_json_dict must be JSON-serialisable (no dataclasses etc)."""
        import json
        snap = self._full_snapshot()
        d = to_json_dict(snap)
        # Should not raise.
        json.dumps(d)


# ---------------------------------------------------------------------------
# Metric dataclass helpers
# ---------------------------------------------------------------------------


class TestAnchorResolutionMetrics:
    def test_symbol_resolution_rate_all_symbol(self):
        m = AnchorResolutionMetrics(symbol_count=10, file_fallback_count=0)
        assert m.symbol_resolution_rate == 1.0

    def test_symbol_resolution_rate_all_file(self):
        m = AnchorResolutionMetrics(symbol_count=0, file_fallback_count=5)
        assert m.symbol_resolution_rate == 0.0

    def test_symbol_resolution_rate_empty(self):
        m = AnchorResolutionMetrics()
        assert m.symbol_resolution_rate == 0.0


class TestCorroborationWeightMetrics:
    def test_mean_weight_empty(self):
        m = CorroborationWeightMetrics()
        assert m.mean_weight == 0.0

    def test_max_weight_empty(self):
        m = CorroborationWeightMetrics()
        assert m.max_weight == 0.0

    def test_mean_distinct_prs_empty(self):
        m = CorroborationWeightMetrics()
        assert m.mean_distinct_prs == 0.0


class TestNliMoveMetrics:
    def test_distribution_zeros_when_empty(self):
        m = NliMoveMetrics()
        dist = m.distribution()
        assert all(v == 0.0 for v in dist.values())

    def test_judge_fallback_rate_zero_when_empty(self):
        m = NliMoveMetrics()
        assert m.judge_fallback_rate == 0.0


class TestAuthoredInferredMetrics:
    def test_ratio_zero_when_empty(self):
        m = AuthoredInferredMetrics()
        assert m.authored_inferred_ratio == 0.0

    def test_ratio_all_authored(self):
        m = AuthoredInferredMetrics(authored_count=5, inferred_count=0)
        assert m.authored_inferred_ratio == 1.0


# ---------------------------------------------------------------------------
# Integration: accumulator → gate_status round-trip
# ---------------------------------------------------------------------------


class TestAccumulatorToGateRoundTrip:
    def test_gate_opens_after_enough_samples(self):
        """End-to-end: accumulate events → snapshot → gate_status."""
        acc = TelemetryAccumulator()
        cfg = GateConfig(
            min_anchor_samples=5,
            supersede_fp_ceiling=0.10,
            supersede_fp_min_samples=3,
        )

        # Record enough anchor samples to open the verified_learning gate.
        for _ in range(4):
            acc.record_anchor_resolution(resolved_as_symbol=True)
        acc.record_anchor_resolution(resolved_as_symbol=False)  # total = 5

        # Record enough FP spot-checks (all TPs) to open the supersede gate.
        for i in range(3):
            acc.record_supersede_fp_check(f"v{i:03d}", is_false_positive=False)

        snap = acc.snapshot()
        gs = gate_status(snap, config=cfg)

        assert gs.verified_learning_gate_open is True
        assert gs.supersede_gate_open is True
        assert gs.unfold_gate_open is True

    def test_gate_blocked_when_fp_rate_too_high(self):
        acc = TelemetryAccumulator()
        cfg = GateConfig(
            min_anchor_samples=1,
            supersede_fp_ceiling=0.10,
            supersede_fp_min_samples=3,
        )
        acc.record_anchor_resolution(resolved_as_symbol=True)

        # 2 out of 3 spot-checks are FPs → 67% FP rate → gate blocked.
        acc.record_supersede_fp_check("v001", is_false_positive=True)
        acc.record_supersede_fp_check("v002", is_false_positive=True)
        acc.record_supersede_fp_check("v003", is_false_positive=False)

        snap = acc.snapshot()
        gs = gate_status(snap, config=cfg)
        assert gs.supersede_gate_open is False
        assert gs.unfold_gate_open is False
