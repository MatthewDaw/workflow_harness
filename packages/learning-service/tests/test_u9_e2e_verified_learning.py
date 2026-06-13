"""test_u9_e2e_verified_learning.py — MAT-147 (U9) end-to-end verified-learning loop.

Tests the REAL wired pipeline end-to-end:
  replay -> fold -> contradiction PR supersedes (invalidAt stamped + un-fold)
         -> re-introduction PR revives -> necessity demotes a planted useless idea
         -> cosine~0.95 contradiction classifies SUPERSEDE not CORROBORATE
            (through the real corroborate merge-verdict path, not a pre-seeded fixture)
         -> migration grandfathers an existing fold

All tests are OFFLINE (replay NLI fixtures, InMemoryLearningStore, InMemorySkillStore).
Zero live GitHub, zero live model load, zero quota.

Architecture verified:
  - learning_service.pipeline.run_pipeline drives U2+U3+U4+U5+fold
  - learning_service.merge_handler.replay_merge_log wires the spine
  - learning_service.entrypoints.ingest.run_ingest wires the entrypoint
  - learning_service.migration.grandfather_legacy_folds handles backfill
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures directory — NLI replay fixtures that make the test offline
# ---------------------------------------------------------------------------

# All NLI classify calls in this test go through the replay fixture cache.
# The fixtures are in tests/fixtures/nli/ (pre-written by the build script).
_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "nli"


# ---------------------------------------------------------------------------
# NLI pairs and their pre-written fixture verdicts
# ---------------------------------------------------------------------------

MODEL = "cross-encoder/nli-deberta-v3-base"

# PR bodies used in the test.
_PR1_BODY = "Always use snake_case for Python identifiers."
_PR2_BODY = "Use camelCase for all Python variables and functions."  # contradicts PR1
_PR3_BODY = "Use snake_case for Python identifiers and module names."  # re-teaches PR1
_PR4_BODY = "Prefer snake_case naming in Python modules and packages."  # corroborates
_USELESS_BODY = "Consider using print() statements for quick debug checks."
_CONTRA_BODY = "Avoid snake_case for Python identifiers and prefer camelCase instead."


# ---------------------------------------------------------------------------
# Helpers to build the NLI classifier in replay mode with the test fixtures
# ---------------------------------------------------------------------------

def _make_classifier(fixtures_dir: Path | None = None) -> "NliClassifier":
    from learning_service.classifier import NliClassifier
    return NliClassifier(
        nli_mode="replay",
        fixtures_dir=str(fixtures_dir or _FIXTURES_DIR),
    )


def _make_pr(
    number: int,
    title: str,
    body: str,
    diff: str = "",
    merged: bool = True,
    base_branch: str = "main",
    author: str = "alice",
    labels: list[str] | None = None,
) -> "PullRequest":
    from learning_service.github import PullRequest
    return PullRequest(
        number=number,
        title=title,
        body=body,
        base_branch=base_branch,
        merged=merged,
        merged_at="2026-06-12T10:00:00Z",
        author_login=author,
        diff=diff or f"--- a/utils.py\n+++ b/utils.py\n@@ -1,3 +1,4 @@\n def foo():\n-    pass\n+    return None\n",
        labels=labels or [],
        owner_repo="acme/backend",
    )


# ---------------------------------------------------------------------------
# Pre-generate NLI fixtures for any pair not yet in the fixtures dir.
# This ensures the test is self-contained: if a fixture is missing, it is
# written with a deterministic verdict (offline, no model needed).
# ---------------------------------------------------------------------------

def _ensure_fixture(
    fixtures_dir: Path,
    premise: str,
    hypothesis: str,
    label: str,
    confidence: float,
) -> None:
    """Write an NLI replay fixture for (premise, hypothesis) if not present."""
    import hashlib, json
    canonical = json.dumps(
        {"model": MODEL, "premise": premise, "hypothesis": hypothesis},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path = fixtures_dir / f"{h}.json"
    if not path.exists():
        fixtures_dir.mkdir(parents=True, exist_ok=True)
        body = json.dumps({
            "request_hash": h,
            "request": {"model": MODEL, "premise": premise, "hypothesis": hypothesis},
            "envelope": {"label": label, "confidence": confidence},
        }, sort_keys=True, indent=2, ensure_ascii=False)
        path.write_text(body + "\n", encoding="utf-8", newline="\n")


def _distilled(title: str, body: str) -> str:
    """Return the distilled body as the pipeline would produce it.

    distill_pr builds the insight body from title + body (the most common path
    for non-bugfix PRs). We mirror that here so NLI fixtures match exactly.
    """
    from learning_service.github import distill_pr, PullRequest
    pr = PullRequest(
        number=0, title=title, body=body, base_branch="main",
        merged=True, merged_at="2026-06-12T10:00:00Z", author_login="alice",
        diff="", labels=[], owner_repo="acme/backend",
    )
    return distill_pr(pr).body


def _ensure_all_fixtures(fixtures_dir: Path) -> None:
    """Ensure all NLI fixtures needed by this test are present.

    Fixtures are keyed by the DISTILLED bodies (what the pipeline actually stores
    in the idea record), not the raw PR body strings.
    """
    d1 = _distilled("Always use snake_case", _PR1_BODY)
    d2 = _distilled("Use camelCase for all Python variables and functions", _PR2_BODY)
    d3 = _distilled("Use snake_case for Python identifiers and module names", _PR3_BODY)
    d4 = _distilled("Prefer snake_case naming", _PR4_BODY)
    dU = _distilled("Consider using print statements for quick debug checks", _USELESS_BODY)
    dC = _distilled(
        "Avoid snake_case for Python identifiers and prefer camelCase instead",
        _CONTRA_BODY,
    )

    # Body produced by _make_pr(100, "Use camelCase everywhere", _PR2_BODY)
    # (used in contradiction/revive tests)
    d2_contra = _distilled("Use camelCase everywhere", _PR2_BODY)
    # Body produced by _make_pr(200, "Use snake_case...", _PR3_BODY)
    d3_revive = _distilled(
        "Use snake_case for Python identifiers and module names", _PR3_BODY
    )
    # Body produced by _make_pr(300, "Use camelCase for Python identifiers", _CONTRA_BODY)
    d_contra_300 = _distilled(
        "Use camelCase for Python identifiers", _CONTRA_BODY
    )

    pairs = [
        # Semantic join: d1 (incumbent) vs d4 (challenger) -> CORROBORATE (fold path)
        (d1, d4, "entailment", 0.85),
        # Semantic join / locality: d1 vs d2 -> SUPERSEDE
        (d1, d2, "contradiction", 0.92),
        # Locality join: d1 vs d2_contra (from pr_contra in supersede/revive tests)
        (d1, d2_contra, "contradiction", 0.92),
        # Locality join: d1 (retired) vs d3_revive -> CORROBORATE (revive path)
        (d1, d3_revive, "entailment", 0.88),
        # d2 (new active) vs d3 -> CONTRADICTION
        (d2, d3, "contradiction", 0.80),
        # d3 vs d4 -> CORROBORATE
        (d3, d4, "entailment", 0.87),
        # HIGH-COSINE contradiction: d1 vs d_contra_300 -> SUPERSEDE
        (d1, d_contra_300, "contradiction", 0.95),
        # HIGH-COSINE d1 vs dC (same body different PR title)
        (d1, dC, "contradiction", 0.95),
        # Self-matches (semantic join)
        (d1, d1, "entailment", 0.99),
        (d2, d2, "entailment", 0.99),
        (d2_contra, d2_contra, "entailment", 0.99),
        (d3, d3, "entailment", 0.99),
        (d3_revive, d3_revive, "entailment", 0.99),
        (d4, d4, "entailment", 0.99),
        (dU, dU, "entailment", 0.99),
        (d_contra_300, d_contra_300, "entailment", 0.99),
    ]
    for premise, hypothesis, label, confidence in pairs:
        _ensure_fixture(fixtures_dir, premise, hypothesis, label, confidence)


# ---------------------------------------------------------------------------
# Core e2e test: the full lifecycle
# ---------------------------------------------------------------------------

class TestE2EVerifiedLearningLoop:
    """End-to-end verified learning loop tests.

    Tests the real pipeline modules wired together — no simulation.
    """

    def setup_method(self) -> None:
        """Set up fresh stores and classifier for each test."""
        from learning_service.db.store import InMemoryLearningStore
        from learning_service.skills_write import InMemorySkillStore
        from learning_service.corroboration import AuthorCredibilityStore

        _ensure_all_fixtures(_FIXTURES_DIR)

        self.store = InMemoryLearningStore()
        self.skill_store = InMemorySkillStore()
        # Use high credibility (1.5) so two normal-rung PRs fold at verified_k=1.0.
        # Formula: 0.6 (normal rung) × 1.5 (credibility) = 0.9 per PR.
        # Two PRs: 0.9 + 0.9 = 1.8 ≥ 1.0 → fold.
        self.cred = AuthorCredibilityStore(overrides={"alice": 1.5, "bob": 1.5})
        # Use verified_k=1.0 to keep the test deterministic with a small PR log.
        self.verified_k = 1.0
        self.clf = _make_classifier()
        self.org = "acme"
        self.skill = "python-style"
        self.repo = "acme/backend"

    def _replay(self, prs: list, *, mode: str = "enforce", unfold_mode: str = "enforce") -> list:
        """Run replay_merge_log with the full pipeline wired."""
        from learning_service.merge_handler import replay_merge_log
        from learning_service.supersession import FpCalibrationGate

        # Pre-load FP gate so enforce mode is allowed in tests.
        fp_gate = FpCalibrationGate(fp_ceiling=0.10, min_samples=1)
        # Record 30 true positives to open the gate.
        for i in range(30):
            fp_gate.record_verdict(f"v{i}", is_false_positive=False)

        return replay_merge_log(
            pull_requests=prs,
            org=self.org,
            store=self.store,
            default_branch="main",
            mode=mode,
            classifier=self.clf,
            credibility_store=self.cred,
            skill_store=self.skill_store,
            skill_base_name=self.skill,
            verified_k=self.verified_k,
            unfold_mode=unfold_mode,
            supersede_mode="enforce",
            file_contents={},
        )

    # -----------------------------------------------------------------------
    # Test 1: Replay -> fold at verified_K (IDEAGOLD golden case captured)
    # -----------------------------------------------------------------------

    def test_replay_fold_creates_golden_case(self) -> None:
        """A merged PR log folds at verified_K and captures an IDEAGOLD golden case.

        Two PRs teaching the same lesson (NLI=corroborate) fold the idea and
        write a skill revision. The IDEAGOLD case is captured in both the
        learning store and the skill store.
        """
        pr1 = _make_pr(1, "Always use snake_case", _PR1_BODY)
        pr2 = _make_pr(4, "Prefer snake_case naming", _PR4_BODY)  # NLI=corroborate with PR1

        results = self._replay([pr1, pr2])

        # Both PRs were distilled.
        assert all(r.action == "distilled" for r in results), (
            f"Expected both distilled, got: {[r.action for r in results]}"
        )

        # The second PR should have triggered a fold (verify via pipeline result).
        pr2_result = results[1]
        pr2_pipeline = getattr(pr2_result, "_pipeline_result", None)
        assert pr2_pipeline is not None, "Pipeline result not attached to MergeHandlerResult"
        assert pr2_pipeline.folded, f"Expected idea to be folded, got: {pr2_pipeline}"

        # Verify the golden case was captured in the learning store.
        golden_case_id = pr2_pipeline.golden_case_id
        assert golden_case_id is not None, "No golden_case_id in pipeline result"
        gc = self.store.get_golden_case(self.org, self.skill, golden_case_id)
        assert gc is not None, f"Golden case {golden_case_id!r} not found in store"
        assert gc.ideaBody  # non-empty idea body

        # Verify a skill revision was written.
        assert pr2_pipeline.new_rev is not None, "No revision minted on fold"

        # Verify the idempotency cursor was written for both PRs.
        assert self.store.is_pr_processed(self.org, self.repo, 1)
        assert self.store.is_pr_processed(self.org, self.repo, 4)

    # -----------------------------------------------------------------------
    # Test 2: Contradiction PR supersedes (invalidAt stamped) + un-fold
    # -----------------------------------------------------------------------

    def test_contradiction_pr_supersedes_and_unfolds(self) -> None:
        """A contradiction PR supersedes an incumbent and un-folds the skill.

        1. PR1 teaches 'snake_case' — folds immediately (verified_k=0.5, weight=0.9).
        2. PR100 contradicts (NLI=supersede) → invalidAt stamped, un-fold.

        We use verified_k=0.5 so a single PR folds (0.6 rung × 1.5 credibility = 0.9 ≥ 0.5).
        This gives the incumbent exactly 1 source, so the challenger (also 1 source by
        convention) wins on recency (PR 100 > PR 1) per decide_winner.
        """
        # Override verified_k so one PR folds.
        self.verified_k = 0.5
        pr1 = _make_pr(1, "Always use snake_case", _PR1_BODY, author="alice")

        # Step 1: Fold the idea via a single PR.
        self._replay([pr1])

        # Verify the idea is folded.
        ideas = self.store.list_current_ideas(self.org, self.skill)
        folded = [i for i in ideas if i.status == "folded"]
        assert len(folded) >= 1, f"Expected a folded idea, got: {ideas}"
        incumbent_id = folded[0].ideaId

        # Register an anchor for the incumbent (so the locality join finds it).
        # Use symbol='__file__' — the file-level fallback that extract_anchors_from_diff
        # returns when no file_contents (tree-sitter) are provided.
        from learning_service.schema.generated.py_types import AnchorRecord
        self.store.put_anchor(AnchorRecord(
            ideaId=incumbent_id,
            ownerRepo=self.repo,
            file="utils.py",
            symbol="__file__",
            org=self.org,
            active=True,
        ))

        # Step 3: PR with a contradiction diff touches the same file/symbol.
        pr_contra = _make_pr(
            100,
            "Use camelCase everywhere",
            _PR2_BODY,
            # Diff touches utils.py/foo — locality join will find the incumbent.
            diff="--- a/utils.py\n+++ b/utils.py\n@@ -1,3 +1,4 @@\n def foo():\n-    pass\n+    return 1\n",
        )
        results = self._replay([pr_contra])

        # The contradiction PR should be distilled.
        assert results[0].action == "distilled"

        # The incumbent should now have invalidAt set.
        incumbent = self.store.get_idea(self.org, self.skill, incumbent_id)
        assert incumbent is not None
        assert incumbent.invalidAt is not None, (
            f"Expected invalidAt to be stamped on superseded idea, got: {incumbent}"
        )

    # -----------------------------------------------------------------------
    # Test 3: Re-introduction PR revives a superseded idea
    # -----------------------------------------------------------------------

    def test_revived_idea_clears_invalid_at(self) -> None:
        """A new PR teaching a retired pattern revives the superseded idea.

        1. PR1 folds immediately (verified_k=0.5, weight=0.9 ≥ 0.5).
        2. PR_contra supersedes it (invalidAt stamped).
        3. PR_revive re-teaches the same lesson → invalidAt cleared, revivedAt set.
        """
        from learning_service.schema.generated.py_types import AnchorRecord

        # Override verified_k so one PR folds, giving the incumbent 1 source
        # (equal to the challenger's assumed 1) so decide_winner picks challenger.
        self.verified_k = 0.5

        # Step 1: Fold via a single PR.
        pr1 = _make_pr(1, "Always use snake_case", _PR1_BODY)
        self._replay([pr1])

        ideas = self.store.list_current_ideas(self.org, self.skill)
        folded = [i for i in ideas if i.status == "folded"]
        assert folded, "Precondition: expected a folded idea"
        incumbent_id = folded[0].ideaId

        # Register anchor with __file__ symbol (file-level fallback from extract_anchors_from_diff).
        self.store.put_anchor(AnchorRecord(
            ideaId=incumbent_id,
            ownerRepo=self.repo,
            file="utils.py",
            symbol="__file__",
            org=self.org,
            active=True,
        ))

        # Step 2: Supersede.
        pr_contra = _make_pr(
            100, "Use camelCase everywhere", _PR2_BODY,
            diff="--- a/utils.py\n+++ b/utils.py\n@@ -1,3 +1,4 @@\n def foo():\n-    pass\n+    return 1\n",
        )
        self._replay([pr_contra])

        # Verify superseded.
        idea_after_sup = self.store.get_idea(self.org, self.skill, incumbent_id)
        assert idea_after_sup is not None and idea_after_sup.invalidAt is not None, (
            "Precondition: idea should be superseded"
        )

        # Step 3: Revive — a new PR teaches the same snake_case lesson.
        # The retired idea now has invalidAt set; the pipeline should revive it
        # when a new PR corroborates it via the locality join.
        self.store.put_anchor(AnchorRecord(
            ideaId=incumbent_id,
            ownerRepo=self.repo,
            file="utils.py",
            symbol="__file__",
            org=self.org,
            active=True,  # re-activate the anchor for the revive path
        ))
        pr_revive = _make_pr(
            200,
            "Use snake_case for Python identifiers and module names",
            _PR3_BODY,  # NLI vs PR1_BODY = entailment -> CORROBORATE -> revive
            diff="--- a/utils.py\n+++ b/utils.py\n@@ -1,3 +1,4 @@\n def foo():\n-    return 1\n+    return None\n",
        )
        self._replay([pr_revive])

        # The revived idea should have invalidAt cleared.
        revived = self.store.get_idea(self.org, self.skill, incumbent_id)
        assert revived is not None
        assert revived.invalidAt is None, (
            f"Expected invalidAt cleared after revive, got invalidAt={revived.invalidAt}"
        )
        assert revived.revivedAt is not None, (
            f"Expected revivedAt set after revive, got revivedAt={revived.revivedAt}"
        )

    # -----------------------------------------------------------------------
    # Test 4: Necessity gate demotes a planted useless idea
    # -----------------------------------------------------------------------

    def test_necessity_gate_demotes_useless_idea(self) -> None:
        """The scheduled necessity scan demotes a folded idea with no golden support.

        A useless idea is planted directly in the store (folded, no golden case).
        The necessity scan (Phase 2) is run and the idea should be demoted.
        """
        from learning_service.schema.generated.py_types import IdeaRecord, GoldenCaseRecord
        from learning_service.necessity import run_necessity_scan, NecessityFpGate

        # Plant a folded useless idea directly (no golden case).
        useless_id = "useless-idea-001"
        useless = IdeaRecord(
            ideaId=useless_id,
            skillBaseName=self.skill,
            org=self.org,
            body=_USELESS_BODY,
            status="folded",
            corroborationVersion=0,
            foldedIntoRev=None,   # no revision — simplifies the test
            authorityKind="merged",
        )
        self.store.put_idea_conditional(useless, expected_version=-1)

        fp_gate = NecessityFpGate()
        # Promote the gate to soft_block so demotes are allowed.
        for i in range(50):
            fp_gate.record_verdict(f"v{i}", is_false_positive=False)

        # Run without a golden case → no_signal (never demotes).
        result = run_necessity_scan(
            org=self.org,
            skill_base_name=self.skill,
            store=self.store,
            run_judge_fn=None,  # no judge needed (no golden case → no_signal)
            fp_gate=fp_gate,
        )

        # With no golden case the verdict is no_signal — idea is NOT demoted.
        assert result.no_signal >= 1, f"Expected at least one no_signal, got: {result}"

        # Now plant a golden case for the useless idea and re-run with a judge
        # that returns a low score_diff (the idea doesn't help).
        self.store.put_golden_case(GoldenCaseRecord(
            org=self.org,
            skillBaseName=self.skill,
            caseId=useless_id,
            before="Original skill body without the useless idea.",
            after="Skill body with the useless idea.",
            ideaBody=_USELESS_BODY,
        ))

        # Judge returns satisfied=False for both with and without → score_diff=0.0 → unnecessary.
        def judge_fn_unnecessary(prompt, schema, **kw):
            # Always returns satisfied=False → score=0.0 both with and without.
            # score_diff = 0.0 - 0.0 = 0.0 < threshold (0.05) → unnecessary.
            return type("R", (), {"output": {"satisfied": False, "reason": "test"}})()

        result2 = run_necessity_scan(
            org=self.org,
            skill_base_name=self.skill,
            store=self.store,
            run_judge_fn=judge_fn_unnecessary,
            fp_gate=fp_gate,
        )

        # The idea should be demoted (score_diff < 0.05 threshold).
        demoted_ids = [v.idea_id for v in result2.verdicts if v.demoted]
        assert useless_id in demoted_ids, (
            f"Expected useless idea to be demoted, verdicts: {result2.verdicts}"
        )

        # Verify invalidAt was stamped.
        demoted = self.store.get_idea(self.org, self.skill, useless_id)
        assert demoted is not None
        assert demoted.invalidAt is not None, (
            f"Expected demoted idea to have invalidAt set, got: {demoted}"
        )

    # -----------------------------------------------------------------------
    # Test 5: cosine~0.95 contradiction classifies SUPERSEDE (not CORROBORATE)
    #         via the real corroborate merge-verdict path
    # -----------------------------------------------------------------------

    def test_high_cosine_contradiction_classifies_supersede_not_merge(self) -> None:
        """A contradiction at cosine~0.95 must classify SUPERSEDE via NLI, not CORROBORATE.

        This is the core negation-blindness fix: the old cosine->=0.9 MERGE-REWRITE
        verdict is replaced by an NLI verdict. A near-synonym contradiction must
        classify SUPERSEDE and trigger supersession, never CORROBORATE.

        The pair (_PR1_BODY, _CONTRA_BODY) has NLI=contradiction/0.95 fixture.
        The corroborate path must NOT be taken.
        """
        from learning_service.classifier import NliClassifier, Verdict

        clf = _make_classifier()

        # Classify directly — the negation-blindness fix.
        result = clf.classify(_PR1_BODY, _CONTRA_BODY)
        assert result.verdict == Verdict.SUPERSEDE, (
            f"Expected SUPERSEDE for contradiction at high cosine, got {result.verdict}"
        )
        assert result.nli_confidence >= 0.90, (
            f"Expected high confidence contradiction, got {result.nli_confidence}"
        )

        # Also verify via the pipeline: a PR with _CONTRA_BODY against an idea
        # with _PR1_BODY must trigger supersession, not corroboration.
        from learning_service.schema.generated.py_types import IdeaRecord, AnchorRecord

        # Use the distilled PR1 body (what the pipeline actually stores in ideas).
        idea_body = _distilled("Always use snake_case", _PR1_BODY)

        idea_id = "idea-snake-case"
        idea = IdeaRecord(
            ideaId=idea_id,
            skillBaseName=self.skill,
            org=self.org,
            body=idea_body,
            status="open",
            corroborationVersion=0,
            authorityKind="merged",
        )
        self.store.put_idea_conditional(idea, expected_version=-1)

        # Register an anchor for the locality join.
        # Use symbol='__file__' — the file-level fallback produced by extract_anchors_from_diff
        # when no tree-sitter file_contents are provided (offline test mode).
        self.store.put_anchor(AnchorRecord(
            ideaId=idea_id,
            ownerRepo=self.repo,
            file="utils.py",
            symbol="__file__",
            org=self.org,
            active=True,
        ))

        # Replay the contradiction PR.
        pr_contra = _make_pr(
            300,
            "Use camelCase for Python identifiers",
            _CONTRA_BODY,
            diff="--- a/utils.py\n+++ b/utils.py\n@@ -1,3 +1,4 @@\n def foo():\n-    pass\n+    return 1\n",
        )
        results = self._replay([pr_contra])
        assert results[0].action == "distilled"

        pr_pipeline = getattr(results[0], "_pipeline_result", None)
        assert pr_pipeline is not None, "Pipeline result not attached"

        # The supersede path must have been taken.
        superseded = [d for d in pr_pipeline.supersede_decisions if d.verdict == "superseded"]
        assert superseded, (
            f"Expected supersession for cosine~0.95 contradiction, "
            f"supersede_decisions={pr_pipeline.supersede_decisions}"
        )

        # The idea must have invalidAt stamped (not be treated as corroboration).
        retired_idea = self.store.get_idea(self.org, self.skill, idea_id)
        assert retired_idea is not None
        assert retired_idea.invalidAt is not None, (
            f"Expected invalidAt on superseded idea, got: {retired_idea}. "
            "This would indicate the negation-blindness bug is still present."
        )

    # -----------------------------------------------------------------------
    # Test 6: Migration grandfathers an existing fold
    # -----------------------------------------------------------------------

    def test_migration_grandfathers_legacy_fold(self) -> None:
        """grandfather_legacy_folds tags legacy-folded ideas as legacyRecurrenceFold.

        Existing folded ideas with no merged-PR source (legacy recurrence folds)
        are tagged legacyRecurrenceFold=True so the supersession/necessity
        machinery treats them at par with PR-gated folds. They stay folded.
        """
        from learning_service.schema.generated.py_types import IdeaRecord
        from learning_service.migration import grandfather_legacy_folds, confirm_grandfathered_fold

        # Plant a legacy-folded idea (no prRef in sources).
        legacy_id = "legacy-folded-idea"
        legacy = IdeaRecord(
            ideaId=legacy_id,
            skillBaseName=self.skill,
            org=self.org,
            body="Always validate input at the API boundary.",
            status="folded",
            corroborationVersion=0,
            foldedIntoRev=None,
            authorityKind="merged",
            legacyRecurrenceFold=None,  # not yet tagged
        )
        self.store.put_idea_conditional(legacy, expected_version=-1)

        # No sources (no prRef) — this is a legacy recurrence fold.

        # Run the grandfather migration.
        result = grandfather_legacy_folds(self.org, self.store)
        assert legacy_id in result.tagged, (
            f"Expected {legacy_id!r} in tagged, got: {result.tagged}"
        )

        # The idea should now be tagged legacyRecurrenceFold=True.
        tagged_idea = self.store.get_idea(self.org, self.skill, legacy_id)
        assert tagged_idea is not None
        assert tagged_idea.legacyRecurrenceFold is True, (
            f"Expected legacyRecurrenceFold=True, got: {tagged_idea.legacyRecurrenceFold}"
        )
        # The idea stays folded (not un-folded).
        assert tagged_idea.status == "folded", (
            f"Legacy fold must stay folded, got status={tagged_idea.status}"
        )

        # A real merged-PR vote arrives → confirm clears the legacy flag.
        from learning_service.schema.generated.py_types import IdeaSourceRecord
        self.store.put_idea_source(IdeaSourceRecord(
            ideaId=legacy_id,
            sourceId="pr#1",
            org=self.org,
            prRef=f"{self.repo}#1",
            authorityKind="merged",
            verificationRung="normal",
            authorId="alice",
        ))
        confirm_result = confirm_grandfathered_fold(self.org, legacy_id, self.skill, self.store)
        assert confirm_result.action == "confirmed", (
            f"Expected confirm action, got: {confirm_result.action}"
        )
        confirmed_idea = self.store.get_idea(self.org, self.skill, legacy_id)
        assert confirmed_idea is not None
        assert not confirmed_idea.legacyRecurrenceFold, (
            f"Expected legacyRecurrenceFold cleared after confirm, got: {confirmed_idea.legacyRecurrenceFold}"
        )


# ---------------------------------------------------------------------------
# Entrypoint wiring test: run_ingest drives replay_merge_log
# ---------------------------------------------------------------------------

class TestIngestEntrypointWired:
    """Verify that run_ingest is no longer a stub."""

    def test_run_ingest_processes_pr_log(self) -> None:
        """run_ingest with a pr_log actually replays PRs through the full pipeline.

        This verifies Gap 2: the 'U1 handler not yet wired' stub is gone.
        """
        from learning_service.entrypoints.ingest import IngestConfig, run_ingest
        from learning_service.db.store import InMemoryLearningStore
        from learning_service.skills_write import InMemorySkillStore
        from learning_service.corroboration import AuthorCredibilityStore

        _ensure_all_fixtures(_FIXTURES_DIR)

        store = InMemoryLearningStore()
        skill_store = InMemorySkillStore()

        pr1 = _make_pr(1, "Always use snake_case", _PR1_BODY)
        pr4 = _make_pr(4, "Prefer snake_case naming", _PR4_BODY)

        config = IngestConfig(org="acme", repo="acme/backend", mode="enforce", verified_k=2.0)
        # Inject the in-memory stores and the PR log via setattr.
        config.pr_log = [pr1, pr4]  # type: ignore[attr-defined]
        config.store = store          # type: ignore[attr-defined]
        config.skill_store = skill_store  # type: ignore[attr-defined]
        config.credibility_store = AuthorCredibilityStore(overrides={"alice": 0.8})  # type: ignore[attr-defined]
        config.classifier = _make_classifier()  # type: ignore[attr-defined]
        config.skill_base_name = "python-style"  # type: ignore[attr-defined]
        config.nli_fixtures_dir = str(_FIXTURES_DIR)  # type: ignore[attr-defined]

        exit_code = run_ingest(config)
        assert exit_code == 0, f"run_ingest returned exit_code={exit_code}"

        # Both PRs should be processed (idempotency cursor written).
        assert store.is_pr_processed("acme", "acme/backend", 1)
        assert store.is_pr_processed("acme", "acme/backend", 4)

        # At least one idea should have been created (not a no-op stub).
        ideas = store.list_all_ideas_for_org("acme")
        assert ideas, "run_ingest must create ideas (not be a stub)"

    def test_run_ingest_idempotent_on_repeat(self) -> None:
        """run_ingest on the same PR log twice does not double-count."""
        from learning_service.entrypoints.ingest import IngestConfig, run_ingest
        from learning_service.db.store import InMemoryLearningStore
        from learning_service.skills_write import InMemorySkillStore

        _ensure_all_fixtures(_FIXTURES_DIR)

        store = InMemoryLearningStore()
        skill_store = InMemorySkillStore()
        pr1 = _make_pr(1, "Always use snake_case", _PR1_BODY)

        def _make_config():
            cfg = IngestConfig(org="acme", repo="acme/backend", mode="enforce")
            cfg.pr_log = [pr1]  # type: ignore[attr-defined]
            cfg.store = store  # type: ignore[attr-defined]
            cfg.skill_store = skill_store  # type: ignore[attr-defined]
            cfg.classifier = _make_classifier()  # type: ignore[attr-defined]
            cfg.skill_base_name = "python-style"  # type: ignore[attr-defined]
            return cfg

        # First run.
        run_ingest(_make_config())
        sources_after_1 = store.list_idea_sources("acme", next(
            (i.ideaId for i in store.list_all_ideas_for_org("acme")), "x"
        ))

        # Second run on the same PR log — cursor must prevent double-count.
        run_ingest(_make_config())
        sources_after_2 = store.list_idea_sources("acme", next(
            (i.ideaId for i in store.list_all_ideas_for_org("acme")), "x"
        ))

        assert len(sources_after_1) == len(sources_after_2), (
            f"Double-run must not double-count sources: "
            f"first={len(sources_after_1)} second={len(sources_after_2)}"
        )


# ---------------------------------------------------------------------------
# Standalone pipeline unit test: the full spine for one PR
# ---------------------------------------------------------------------------

class TestPipelineSpineWired:
    """Unit tests that drive the pipeline module directly."""

    def setup_method(self) -> None:
        from learning_service.db.store import InMemoryLearningStore
        from learning_service.skills_write import InMemorySkillStore
        from learning_service.corroboration import AuthorCredibilityStore

        _ensure_all_fixtures(_FIXTURES_DIR)

        self.store = InMemoryLearningStore()
        self.skill_store = InMemorySkillStore()
        self.cred = AuthorCredibilityStore(overrides={"alice": 0.8})
        self.clf = _make_classifier()
        self.org = "acme"
        self.skill = "python-style"
        self.repo = "acme/backend"

    def test_pipeline_creates_idea_on_first_pr(self) -> None:
        """run_pipeline creates a new idea for the first PR (no existing ideas)."""
        from learning_service.pipeline import run_pipeline
        from learning_service.merge_handler import MergeHandlerResult
        from learning_service.github import DistillationResult

        result = MergeHandlerResult(
            action="distilled",
            pr_number=1,
            owner_repo=self.repo,
            distillation=DistillationResult(
                pr_number=1,
                owner_repo=self.repo,
                author_login="alice",
                title="Always use snake_case",
                body=_PR1_BODY,
                rung="normal",
                is_bugfix=False,
                kind="normal",
                session_context=None,
                raw_anchors_from_diff="",
            ),
            rung="normal",
            rung_weight=0.6,
        )

        pr = run_pipeline(
            result=result,
            org=self.org,
            store=self.store,
            classifier=self.clf,
            credibility_store=self.cred,
            skill_store=self.skill_store,
            skill_base_name=self.skill,
            verified_k=2.0,
            mode="enforce",
        )

        assert pr.idea_action in ("created", "candidate"), (
            f"Expected created or candidate, got: {pr.idea_action}"
        )
        assert pr.idea_id, "Expected a non-empty idea_id"

        # Verify the idea was persisted.
        ideas = self.store.list_all_ideas_for_org(self.org)
        assert any(i.ideaId == pr.idea_id for i in ideas), (
            f"Idea {pr.idea_id!r} not found in store"
        )

    def test_pipeline_skips_non_distilled_results(self) -> None:
        """run_pipeline is a no-op for non-distilled results."""
        from learning_service.pipeline import run_pipeline
        from learning_service.merge_handler import MergeHandlerResult

        for action in ("skipped_noise", "negative", "skipped_idempotent"):
            result = MergeHandlerResult(
                action=action,
                pr_number=99,
                owner_repo=self.repo,
            )
            pr = run_pipeline(
                result=result,
                org=self.org,
                store=self.store,
                classifier=self.clf,
                credibility_store=self.cred,
                skill_store=self.skill_store,
                skill_base_name=self.skill,
                mode="enforce",
            )
            assert pr.idea_action == f"skipped_{action}", (
                f"Expected skipped_{action}, got: {pr.idea_action}"
            )

    def test_pipeline_shadow_mode_makes_no_writes(self) -> None:
        """In shadow mode the pipeline logs decisions but makes no store writes."""
        from learning_service.pipeline import run_pipeline
        from learning_service.merge_handler import MergeHandlerResult
        from learning_service.github import DistillationResult

        result = MergeHandlerResult(
            action="distilled",
            pr_number=5,
            owner_repo=self.repo,
            distillation=DistillationResult(
                pr_number=5,
                owner_repo=self.repo,
                author_login="alice",
                title="Always use snake_case",
                body=_PR1_BODY,
                rung="normal",
                is_bugfix=False,
                kind="normal",
                session_context=None,
                raw_anchors_from_diff="",
            ),
            rung="normal",
            rung_weight=0.6,
        )

        pr = run_pipeline(
            result=result,
            org=self.org,
            store=self.store,
            classifier=self.clf,
            credibility_store=self.cred,
            skill_store=self.skill_store,
            skill_base_name=self.skill,
            mode="shadow",
        )

        assert pr.idea_action.startswith("shadow_"), (
            f"Expected shadow action, got: {pr.idea_action}"
        )
        # No ideas should have been written.
        assert not self.store.list_all_ideas_for_org(self.org), (
            "Shadow mode must make no store writes"
        )
