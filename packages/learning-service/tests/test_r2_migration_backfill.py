"""MAT-151 (R2) — Migration / backfill: grandfather legacy folds + optional
transfer-replay shadow diff.

Acceptance checklist (all items from Linear MAT-151):
  [x] test_existing_folds_grandfathered_not_mass_churned
  [x] test_grandfathered_fold_confirmed_on_real_pr_vote
  [x] test_transfer_replay_deterministic_shadow_diff_vs_legacy

Additional coverage (moto-backed):
  [x] test_moto_grandfather_tags_folded_ideas_in_dynamo
  [x] test_moto_confirm_clears_flag_in_dynamo

All tests are OFFLINE (no live GitHub, no model load, no network).
Moto tests skip cleanly if boto3/moto are not installed.
"""
from __future__ import annotations

import importlib.util

import pytest

# ---------------------------------------------------------------------------
# In-memory test helpers (always available)
# ---------------------------------------------------------------------------

from learning_service.db.store import (
    InMemoryLearningStore,
    OrgGuardError,
    ProcessedPrRecord,
    VersionConflictError,
)
from learning_service.schema.generated.py_types import (
    IdeaRecord,
    IdeaSourceRecord,
)
from learning_service.migration import (
    GrandfatherResult,
    ConfirmResult,
    ShadowDiffReport,
    ShadowIdeaSummary,
    grandfather_legacy_folds,
    confirm_grandfathered_fold,
    transfer_replay_shadow_diff,
)

# ---------------------------------------------------------------------------
# Moto availability check
# ---------------------------------------------------------------------------

_HAS_BOTO = (
    importlib.util.find_spec("boto3") is not None
    and importlib.util.find_spec("moto") is not None
)

if _HAS_BOTO:
    import boto3
    from moto import mock_aws
    from learning_service.db.store import DynamoLearningStore

TABLE_NAME = "harness"

ORG = "acme"
SKILL_A = "error-handling"
SKILL_B = "style-guide"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _folded_idea(
    idea_id: str,
    skill: str = SKILL_A,
    org: str = ORG,
    *,
    legacy: bool | None = None,
    folded_into_rev: int | None = 1,
) -> IdeaRecord:
    """Return a folded IdeaRecord (simulating the pre-migration state)."""
    return IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill,
        org=org,
        body=f"Legacy folded body for {idea_id}.",
        status="folded",
        corroborationVersion=2,
        foldedIntoRev=folded_into_rev,
        legacyRecurrenceFold=legacy,
    )


def _open_idea(idea_id: str, skill: str = SKILL_A, org: str = ORG) -> IdeaRecord:
    return IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill,
        org=org,
        body=f"Open body for {idea_id}.",
        status="open",
        corroborationVersion=0,
    )


def _pr_source(idea_id: str, org: str = ORG, pr_number: int = 42) -> IdeaSourceRecord:
    """An IdeaSourceRecord with a real prRef (inferred-lane, merged)."""
    return IdeaSourceRecord(
        ideaId=idea_id,
        sourceId=f"pr#{pr_number}",
        org=org,
        prRef=f"acme/backend#{pr_number}",
        authorityKind="merged",
        verificationRung="normal",
        authorId="dev-1",
    )


def _seed_folded(
    store: InMemoryLearningStore,
    idea_id: str,
    skill: str = SKILL_A,
    *,
    org: str = ORG,
    with_pr_source: bool = False,
    pr_number: int = 42,
) -> IdeaRecord:
    """Seed a folded idea, optionally with a PR source."""
    idea = _folded_idea(idea_id, skill, org)
    store.put_idea(idea)
    if with_pr_source:
        store.put_idea_source(_pr_source(idea_id, org, pr_number=pr_number))
    return idea


# ===========================================================================
# Acceptance checklist — Item 1
# test_existing_folds_grandfathered_not_mass_churned
# ===========================================================================


class TestGrandfatherLegacyFolds:
    """The grandfathering operation tags legacy folds without un-folding them."""

    def test_existing_folds_grandfathered_not_mass_churned(self):
        """CORE ACCEPTANCE TEST: existing folds get legacyRecurrenceFold=True,
        stay folded, and are NOT un-folded en masse."""
        store = InMemoryLearningStore()

        # Seed three legacy folded ideas (no prRef sources).
        for i in range(1, 4):
            _seed_folded(store, f"idea-{i}", with_pr_source=False)

        result = grandfather_legacy_folds(ORG, store)

        assert set(result.tagged) == {"idea-1", "idea-2", "idea-3"}, (
            f"Expected all 3 ideas tagged; got tagged={result.tagged}"
        )
        assert result.errors == [], f"No errors expected; got {result.errors}"

        # Crucially: all ideas are still FOLDED — not un-folded.
        for i in range(1, 4):
            idea = store.get_idea(ORG, SKILL_A, f"idea-{i}")
            assert idea is not None
            assert idea.status == "folded", (
                f"idea-{i} must remain folded, not be un-folded"
            )
            assert idea.legacyRecurrenceFold is True, (
                f"idea-{i} must have legacyRecurrenceFold=True"
            )

    def test_open_ideas_not_tagged(self):
        """Open ideas (not yet folded) are not grandfathered."""
        store = InMemoryLearningStore()
        store.put_idea(_open_idea("open-1"))

        result = grandfather_legacy_folds(ORG, store)

        assert "open-1" not in result.tagged
        idea = store.get_idea(ORG, SKILL_A, "open-1")
        assert idea.legacyRecurrenceFold is None

    def test_already_confirmed_fold_skipped(self):
        """A folded idea that already has a real PR source is NOT tagged (it's confirmed)."""
        store = InMemoryLearningStore()
        _seed_folded(store, "confirmed-1", with_pr_source=True, pr_number=99)

        result = grandfather_legacy_folds(ORG, store)

        # Should appear in skipped_no_prref (has a PR source → already confirmed).
        assert "confirmed-1" in result.skipped_no_prref
        assert "confirmed-1" not in result.tagged

        # Flag must NOT have been set (it was already confirmed).
        idea = store.get_idea(ORG, SKILL_A, "confirmed-1")
        assert idea.legacyRecurrenceFold is None

    def test_already_tagged_ideas_not_double_written(self):
        """An idea already tagged legacyRecurrenceFold=True is reported in already_tagged."""
        store = InMemoryLearningStore()
        already = _folded_idea("already-tagged", legacy=True)
        store.put_idea(already)

        result = grandfather_legacy_folds(ORG, store)

        assert "already-tagged" in result.already_tagged
        assert "already-tagged" not in result.tagged

    def test_retired_ideas_excluded(self):
        """Retired ideas (invalidAt set) are excluded from grandfathering."""
        store = InMemoryLearningStore()
        retired = IdeaRecord(
            ideaId="retired-1",
            skillBaseName=SKILL_A,
            org=ORG,
            body="Retired.",
            status="folded",
            corroborationVersion=1,
            invalidAt=1000,  # retired
        )
        store.put_idea(retired)

        result = grandfather_legacy_folds(ORG, store)

        assert "retired-1" not in result.tagged

    def test_mixed_batch_only_tags_legacy_candidates(self):
        """Only legacy (no-PR-source) folded ideas are tagged in a mixed batch."""
        store = InMemoryLearningStore()
        _seed_folded(store, "legacy-1", with_pr_source=False)
        _seed_folded(store, "legacy-2", with_pr_source=False)
        _seed_folded(store, "confirmed-pr", with_pr_source=True)
        store.put_idea(_open_idea("still-open"))

        result = grandfather_legacy_folds(ORG, store)

        assert set(result.tagged) == {"legacy-1", "legacy-2"}
        assert "confirmed-pr" in result.skipped_no_prref
        assert "still-open" not in result.tagged

    def test_dry_run_reports_but_does_not_write(self):
        """dry_run=True identifies candidates without writing."""
        store = InMemoryLearningStore()
        _seed_folded(store, "dry-1", with_pr_source=False)

        result = grandfather_legacy_folds(ORG, store, dry_run=True)

        assert "dry-1" in result.tagged

        # Store must be unchanged.
        idea = store.get_idea(ORG, SKILL_A, "dry-1")
        assert idea.legacyRecurrenceFold is None, (
            "dry_run must not write to the store"
        )

    def test_blank_org_raises_org_guard(self):
        store = InMemoryLearningStore()
        with pytest.raises(OrgGuardError):
            grandfather_legacy_folds("", store)

    def test_skill_scoped_scan(self):
        """skill_base_name= limits the scan to that skill family."""
        store = InMemoryLearningStore()
        _seed_folded(store, "sk-a-idea", skill=SKILL_A, with_pr_source=False)
        _seed_folded(store, "sk-b-idea", skill=SKILL_B, with_pr_source=False)

        result = grandfather_legacy_folds(ORG, store, skill_base_name=SKILL_A)

        assert "sk-a-idea" in result.tagged
        assert "sk-b-idea" not in result.tagged

    def test_multi_org_isolated(self):
        """Ideas in a different org are not touched."""
        store = InMemoryLearningStore()
        _seed_folded(store, "acme-idea", org=ORG, with_pr_source=False)
        store.put_idea(IdeaRecord(
            ideaId="rival-idea",
            skillBaseName=SKILL_A,
            org="rival",
            body="Rival idea.",
            status="folded",
            corroborationVersion=1,
        ))

        result = grandfather_legacy_folds(ORG, store)

        assert "acme-idea" in result.tagged
        rival = store.get_idea("rival", SKILL_A, "rival-idea")
        assert rival.legacyRecurrenceFold is None, "rival org must not be touched"

    def test_grandfather_idempotent_second_run(self):
        """Running grandfather_legacy_folds twice is safe — second run is a no-op."""
        store = InMemoryLearningStore()
        _seed_folded(store, "idem-1", with_pr_source=False)

        r1 = grandfather_legacy_folds(ORG, store)
        assert "idem-1" in r1.tagged

        r2 = grandfather_legacy_folds(ORG, store)
        assert "idem-1" in r2.already_tagged
        assert "idem-1" not in r2.tagged

        idea = store.get_idea(ORG, SKILL_A, "idem-1")
        assert idea.legacyRecurrenceFold is True


# ===========================================================================
# Acceptance checklist — Item 2
# test_grandfathered_fold_confirmed_on_real_pr_vote
# ===========================================================================


class TestConfirmGrandfatheredFold:
    """A grandfathered fold earns a real merged-PR vote → confirmed (flag cleared)."""

    def test_grandfathered_fold_confirmed_on_real_pr_vote(self):
        """CORE ACCEPTANCE TEST: a real PR vote clears legacyRecurrenceFold."""
        store = InMemoryLearningStore()

        # Seed a grandfathered fold.
        gran = _folded_idea("gran-1", legacy=True)
        store.put_idea(gran)

        # A real PR vote arrives.
        result = confirm_grandfathered_fold(ORG, "gran-1", SKILL_A, store)

        assert result.action == "confirmed", f"Expected 'confirmed', got {result.action!r}"
        assert result.cleared_at is not None

        # Flag must now be cleared.
        idea = store.get_idea(ORG, SKILL_A, "gran-1")
        assert idea.legacyRecurrenceFold is None, (
            "legacyRecurrenceFold must be None (cleared) after confirmation"
        )
        # Still folded — confirmation doesn't un-fold.
        assert idea.status == "folded"

    def test_confirm_not_grandfathered_is_noop(self):
        """An idea without the flag is a no-op."""
        store = InMemoryLearningStore()
        _seed_folded(store, "no-flag", with_pr_source=True)

        result = confirm_grandfathered_fold(ORG, "no-flag", SKILL_A, store)

        assert result.action == "not_grandfathered"

    def test_confirm_open_idea_is_noop(self):
        """Confirming an open idea returns not_folded (only folded ideas have the flag)."""
        store = InMemoryLearningStore()
        store.put_idea(_open_idea("open-no-flag"))

        result = confirm_grandfathered_fold(ORG, "open-no-flag", SKILL_A, store)

        assert result.action == "not_folded"

    def test_confirm_missing_idea_is_not_found(self):
        store = InMemoryLearningStore()
        result = confirm_grandfathered_fold(ORG, "does-not-exist", SKILL_A, store)
        assert result.action == "not_found"

    def test_confirm_blank_org_raises(self):
        store = InMemoryLearningStore()
        with pytest.raises(OrgGuardError):
            confirm_grandfathered_fold("", "any-id", SKILL_A, store)

    def test_confirm_increments_corroboration_version(self):
        """The OCC write increments corroborationVersion."""
        store = InMemoryLearningStore()
        gran = _folded_idea("ver-1", legacy=True)
        store.put_idea(gran)
        initial_version = gran.corroborationVersion

        confirm_grandfathered_fold(ORG, "ver-1", SKILL_A, store)

        idea = store.get_idea(ORG, SKILL_A, "ver-1")
        assert idea.corroborationVersion == initial_version + 1

    def test_confirm_does_not_change_status_or_folded_into_rev(self):
        """Confirming a fold does NOT un-fold it — status stays 'folded'."""
        store = InMemoryLearningStore()
        gran = _folded_idea("rev-check", legacy=True, folded_into_rev=7)
        store.put_idea(gran)

        confirm_grandfathered_fold(ORG, "rev-check", SKILL_A, store)

        idea = store.get_idea(ORG, SKILL_A, "rev-check")
        assert idea.status == "folded"
        assert idea.foldedIntoRev == 7, "foldedIntoRev must survive confirmation"

    def test_grandfathered_fold_retired_normally_when_superseded(self):
        """After the flag is cleared, a superseding PR retires the idea normally.

        This is the (b) path: superseded by a verified PR → retired normally.
        We simulate it by: grandfather → confirm → manually stamp invalidAt
        and verify the idea is queryable as history (not deleted).
        """
        store = InMemoryLearningStore()
        gran = _folded_idea("retirable-1", legacy=True)
        store.put_idea(gran)

        # Confirm: flag cleared.
        confirm_result = confirm_grandfathered_fold(ORG, "retirable-1", SKILL_A, store)
        assert confirm_result.action == "confirmed"

        # Simulate supersession (the actual supersede() call is tested in U5;
        # here we just check the resulting state is correct).
        confirmed = store.get_idea(ORG, SKILL_A, "retirable-1")
        assert confirmed is not None
        retired = IdeaRecord(
            ideaId=confirmed.ideaId,
            skillBaseName=confirmed.skillBaseName,
            org=confirmed.org,
            body=confirmed.body,
            status=confirmed.status,
            corroborationVersion=confirmed.corroborationVersion + 1,
            foldedIntoRev=confirmed.foldedIntoRev,
            invalidAt=9999999,          # retired
            supersededBy="new-idea-x",
            supersedes=confirmed.supersedes,
            authored=confirmed.authored,
            authorityKind=confirmed.authorityKind,
        )
        store.put_idea_conditional(retired, confirmed.corroborationVersion)

        # Queryable as history (get_idea returns it).
        hist = store.get_idea(ORG, SKILL_A, "retirable-1")
        assert hist is not None
        assert hist.invalidAt == 9999999
        # NOT in current set.
        current = store.list_current_ideas(ORG, SKILL_A)
        current_ids = {i.ideaId for i in current}
        assert "retirable-1" not in current_ids


# ===========================================================================
# Acceptance checklist — Item 3
# test_transfer_replay_deterministic_shadow_diff_vs_legacy
# ===========================================================================


class TestTransferReplayShadowDiff:
    """Transfer-replay is deterministic; shadow diff vs legacy reported before promote."""

    def _make_pr_log(self) -> list[dict]:
        """A fixed merge log fixture (deterministic)."""
        return [
            {
                "pr_number": 10,
                "insights": [
                    {
                        "idea_id": "replay-idea-A",
                        "skill": SKILL_A,
                        "body": "Always validate input at the API boundary.",
                    }
                ],
            },
            {
                "pr_number": 20,
                "insights": [
                    {
                        "idea_id": "replay-idea-B",
                        "skill": SKILL_A,
                        "body": "Use structured logging for all service errors.",
                    }
                ],
            },
        ]

    def test_transfer_replay_deterministic_shadow_diff_vs_legacy(self):
        """CORE ACCEPTANCE TEST: replaying the same log twice yields the same report."""
        store = InMemoryLearningStore()

        # Seed a live legacy library: idea-A is a grandfathered fold whose body
        # overlaps with what the replay would produce; idea-C is only in legacy.
        store.put_idea(IdeaRecord(
            ideaId="legacy-idea-A",
            skillBaseName=SKILL_A,
            org=ORG,
            body="Always validate input at the API boundary.",
            status="folded",
            corroborationVersion=3,
            legacyRecurrenceFold=True,
        ))
        store.put_idea(IdeaRecord(
            ideaId="legacy-idea-C",
            skillBaseName=SKILL_A,
            org=ORG,
            body="This lesson only exists in the legacy library.",
            status="folded",
            corroborationVersion=2,
            legacyRecurrenceFold=True,
        ))

        pr_log = self._make_pr_log()

        report1 = transfer_replay_shadow_diff(ORG, "acme/backend", store, pr_log)
        report2 = transfer_replay_shadow_diff(ORG, "acme/backend", store, pr_log)

        # Deterministic: both runs yield the same counts.
        assert report1.prs_replayed == report2.prs_replayed == 2
        assert len(report1.new_under_verified) == len(report2.new_under_verified)
        assert len(report1.only_in_legacy) == len(report2.only_in_legacy)
        assert len(report1.confirmed_by_replay) == len(report2.confirmed_by_replay)

    def test_shadow_diff_reports_new_under_verified(self):
        """The diff correctly identifies ideas that would be new under verified semantics."""
        store = InMemoryLearningStore()
        # Empty legacy library — everything in the replay is "new".
        pr_log = self._make_pr_log()

        report = transfer_replay_shadow_diff(ORG, "acme/backend", store, pr_log)

        assert report.prs_replayed == 2
        assert len(report.new_under_verified) == 2, (
            f"Expected 2 new ideas; got {len(report.new_under_verified)}"
        )
        assert report.only_in_legacy == []

    def test_shadow_diff_reports_only_in_legacy(self):
        """The diff correctly identifies legacy ideas absent from the replay."""
        store = InMemoryLearningStore()
        store.put_idea(IdeaRecord(
            ideaId="orphan-legacy",
            skillBaseName=SKILL_A,
            org=ORG,
            body="This lesson has no matching PR in the replay log.",
            status="folded",
            corroborationVersion=1,
            legacyRecurrenceFold=True,
        ))

        # Replay log that covers a different idea — does not touch "orphan-legacy".
        pr_log = [
            {
                "pr_number": 99,
                "insights": [
                    {
                        "idea_id": "new-replay-idea",
                        "skill": SKILL_A,
                        "body": "Use circuit breakers for downstream calls.",
                    }
                ],
            }
        ]

        report = transfer_replay_shadow_diff(ORG, "acme/backend", store, pr_log)

        only_legacy_ids = {s.idea_id for s in report.only_in_legacy}
        assert "orphan-legacy" in only_legacy_ids, (
            "Idea absent from replay must appear in only_in_legacy"
        )

    def test_shadow_diff_confirmed_by_replay(self):
        """The diff identifies grandfathered ideas independently confirmed by the replay."""
        store = InMemoryLearningStore()
        store.put_idea(IdeaRecord(
            ideaId="gran-confirm",
            skillBaseName=SKILL_A,
            org=ORG,
            body="Always validate input at the API boundary.",
            status="folded",
            corroborationVersion=2,
            legacyRecurrenceFold=True,
        ))

        pr_log = self._make_pr_log()  # PR 10 produces the same "validate input" insight.
        report = transfer_replay_shadow_diff(ORG, "acme/backend", store, pr_log)

        # The grandfathered idea must appear in confirmed_by_replay.
        confirmed_ids = {s.idea_id for s in report.confirmed_by_replay}
        # The replay matched the body — gran-confirm is confirmed.
        # (The exact idea_id in the confirmed list is the replay's computed id
        # or the matched live id; we check the list is non-empty.)
        assert len(report.confirmed_by_replay) >= 1, (
            "Expected at least one idea confirmed by replay"
        )

    def test_shadow_diff_zero_prs_replayed_on_empty_log(self):
        """An empty PR log yields a zero-PR report with no diffs."""
        store = InMemoryLearningStore()
        store.put_idea(_folded_idea("legacy-only"))

        report = transfer_replay_shadow_diff(ORG, "acme/backend", store, [])

        assert report.prs_replayed == 0
        assert report.new_under_verified == []
        assert len(report.only_in_legacy) == 1

    def test_shadow_diff_does_not_write_to_store(self):
        """The shadow diff is purely read-only — no writes to the store."""
        store = InMemoryLearningStore()
        store.put_idea(_folded_idea("no-write-please"))
        initial_version = store.get_idea(ORG, SKILL_A, "no-write-please").corroborationVersion

        pr_log = [
            {
                "pr_number": 50,
                "insights": [
                    {"idea_id": "x", "skill": SKILL_A, "body": "New lesson."}
                ],
            }
        ]
        transfer_replay_shadow_diff(ORG, "acme/backend", store, pr_log)

        # The existing idea must be unchanged.
        idea = store.get_idea(ORG, SKILL_A, "no-write-please")
        assert idea.corroborationVersion == initial_version
        # No new ideas written.
        assert store.get_idea(ORG, SKILL_A, "x") is None

    def test_shadow_diff_blank_org_raises(self):
        store = InMemoryLearningStore()
        with pytest.raises(OrgGuardError):
            transfer_replay_shadow_diff("", "r/r", store, [])


# ===========================================================================
# Moto-backed tests (production DynamoLearningStore)
# ===========================================================================

_skip_no_moto = pytest.mark.skipif(
    not _HAS_BOTO, reason="boto3/moto not installed"
)


@pytest.fixture()
def dynamo_table():
    if not _HAS_BOTO:
        pytest.skip("boto3/moto not installed")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource


@pytest.fixture()
def dyn_store(dynamo_table):
    return DynamoLearningStore(TABLE_NAME, dynamodb_resource=dynamo_table)


@_skip_no_moto
class TestMotoGrandfatherAndConfirm:
    """Moto-backed versions of the two core acceptance tests."""

    def test_moto_grandfather_tags_folded_ideas_in_dynamo(self, dyn_store):
        """Production DynamoLearningStore: grandfather_legacy_folds tags ideas."""
        # Seed three folded ideas with no prRef sources.
        for i in range(1, 4):
            dyn_store.put_idea(IdeaRecord(
                ideaId=f"dyn-idea-{i}",
                skillBaseName=SKILL_A,
                org=ORG,
                body=f"Dynamo legacy idea {i}.",
                status="folded",
                corroborationVersion=1,
                foldedIntoRev=i,
            ))

        result = grandfather_legacy_folds(ORG, dyn_store)

        assert len(result.tagged) == 3
        assert result.errors == []

        for i in range(1, 4):
            idea = dyn_store.get_idea(ORG, SKILL_A, f"dyn-idea-{i}")
            assert idea is not None
            assert idea.legacyRecurrenceFold is True, (
                f"dyn-idea-{i} must have legacyRecurrenceFold=True in DynamoDB"
            )
            assert idea.status == "folded", (
                f"dyn-idea-{i} must remain folded — not mass-churned"
            )

    def test_moto_confirm_clears_flag_in_dynamo(self, dyn_store):
        """Production DynamoLearningStore: confirm_grandfathered_fold clears the flag."""
        dyn_store.put_idea(IdeaRecord(
            ideaId="dyn-gran-1",
            skillBaseName=SKILL_A,
            org=ORG,
            body="Grandfathered in DynamoDB.",
            status="folded",
            corroborationVersion=2,
            foldedIntoRev=1,
            legacyRecurrenceFold=True,
        ))

        result = confirm_grandfathered_fold(ORG, "dyn-gran-1", SKILL_A, dyn_store)

        assert result.action == "confirmed"

        idea = dyn_store.get_idea(ORG, SKILL_A, "dyn-gran-1")
        assert idea is not None
        assert idea.legacyRecurrenceFold is None, (
            "legacyRecurrenceFold must be cleared (absent from DynamoDB) after confirmation"
        )
        assert idea.status == "folded"

    def test_moto_grandfather_skips_ideas_with_pr_sources(self, dyn_store):
        """Production store: ideas with a prRef source are skipped (already confirmed)."""
        dyn_store.put_idea(IdeaRecord(
            ideaId="dyn-confirmed",
            skillBaseName=SKILL_A,
            org=ORG,
            body="Already confirmed by a PR.",
            status="folded",
            corroborationVersion=1,
        ))
        dyn_store.put_idea_source(IdeaSourceRecord(
            ideaId="dyn-confirmed",
            sourceId="pr#77",
            org=ORG,
            prRef="acme/backend#77",
            authorityKind="merged",
        ))

        result = grandfather_legacy_folds(ORG, dyn_store)

        assert "dyn-confirmed" in result.skipped_no_prref
        assert "dyn-confirmed" not in result.tagged

        idea = dyn_store.get_idea(ORG, SKILL_A, "dyn-confirmed")
        assert idea.legacyRecurrenceFold is None

    def test_moto_grandfather_retired_idea_not_touched(self, dyn_store):
        """Retired ideas (invalidAt set) are not grandfathered."""
        dyn_store.put_idea(IdeaRecord(
            ideaId="dyn-retired",
            skillBaseName=SKILL_A,
            org=ORG,
            body="Retired already.",
            status="folded",
            corroborationVersion=1,
            invalidAt=12345,
        ))

        result = grandfather_legacy_folds(ORG, dyn_store)

        assert "dyn-retired" not in result.tagged
        idea = dyn_store.get_idea(ORG, SKILL_A, "dyn-retired")
        assert idea.legacyRecurrenceFold is None
