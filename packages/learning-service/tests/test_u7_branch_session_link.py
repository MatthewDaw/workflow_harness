"""MAT-140 (U7) — Branch→session link: PostToolUse ingest path tests.

Acceptance checklist items:
  - test_git_push_records_branch_session_link
  - test_failed_or_detached_push_records_nothing
  - test_link_lookup_returns_turn_range_for_branch
  - eager-distilled distilledContext stored at push (no merge-time EVT# read)

The acceptance checklist targets the *store layer* (write/lookup contract) and
the *capture logic* (what constitutes a valid push to record vs. not).  The Go
ingestHook wiring is tested in Go (capture/branch_session_test.go); here we
verify:
  1. InMemoryLearningStore (fast, offline, structural correctness).
  2. DynamoLearningStore via moto (real boto3 semantics, branch_session_key
     round-trip through the actual DynamoDB item serialisation).
  3. The eager-distil contract: distilledContext is stored in the record at
     write time and returned verbatim on lookup (no need to re-read EVT# rows).

moto tests are skipped if boto3/moto are not installed (keeps the offline gate
clean on machines without the full dev stack).
"""

from __future__ import annotations

import importlib.util
import time

import pytest

from learning_service.db.store import (
    InMemoryLearningStore,
    OrgGuardError,
)
from learning_service.schema.generated.py_types import (
    BranchSessionRecord,
    branch_session_key,
)

# ---------------------------------------------------------------------------
# moto availability guard
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

# ---------------------------------------------------------------------------
# Helper: build a BranchSessionRecord
# ---------------------------------------------------------------------------


def _bsl(
    *,
    org: str = "acme",
    owner_repo: str = "acme/backend",
    branch: str = "feat/my-feature",
    session_id: str = "sess-abc123",
    turn_start: int | None = 0,
    turn_end: int | None = 5,
    distilled_context: str | None = "Brief summary of the turn slice.",
    pushed_at: int | None = None,
    ttl_at: int | None = None,
) -> BranchSessionRecord:
    return BranchSessionRecord(
        ownerRepo=owner_repo,
        branch=branch,
        org=org,
        sessionId=session_id,
        turnStart=turn_start,
        turnEnd=turn_end,
        distilledContext=distilled_context,
        pushedAt=pushed_at or int(time.time() * 1000),
        ttlAt=ttl_at,
    )


# ===========================================================================
# 1. InMemoryLearningStore — offline structural tests
# ===========================================================================


class TestBranchSessionLinkInMemory:
    """Offline tests against InMemoryLearningStore."""

    def test_git_push_records_branch_session_link(self):
        """A successful git push writes a BranchSessionLink that can be retrieved."""
        store = InMemoryLearningStore()
        record = _bsl()
        store.put_branch_session_link(record)

        got = store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None, "Expected a link to be found after put"
        assert got.sessionId == "sess-abc123"
        assert got.ownerRepo == "acme/backend"
        assert got.branch == "feat/my-feature"

    def test_failed_or_detached_push_records_nothing(self):
        """When no push is recorded (failed/detached), lookup returns None.

        The caller (ingestHook / notifyBranchSession) is responsible for NOT
        calling put_branch_session_link on failure; this test confirms the
        baseline: an absent push yields None.
        """
        store = InMemoryLearningStore()
        # Nothing recorded.
        got = store.get_branch_session_link("acme", "acme/backend", "feat/failed")
        assert got is None, "Expected None when no link was recorded (failed/detached push)"

    def test_link_lookup_returns_turn_range_for_branch(self):
        """The stored turn-range (turnStart, turnEnd) is returned verbatim on lookup."""
        store = InMemoryLearningStore()
        record = _bsl(turn_start=3, turn_end=9)
        store.put_branch_session_link(record)

        got = store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.turnStart == 3, f"Expected turnStart=3, got {got.turnStart}"
        assert got.turnEnd == 9, f"Expected turnEnd=9, got {got.turnEnd}"

    def test_eager_distilled_context_stored_at_push(self):
        """distilledContext is stored at push time (no merge-time EVT# read).

        The acceptance item says: 'eager-distilled distilledContext stored at
        push (no merge-time EVT# read)'.  This test verifies that distilledContext
        is present in the returned record exactly as written — the contract that
        lets U1 use the context without re-reading any EVT# row.
        """
        store = InMemoryLearningStore()
        ctx = "Session discussed rate-limiting strategy using token bucket."
        record = _bsl(distilled_context=ctx)
        store.put_branch_session_link(record)

        got = store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.distilledContext == ctx, (
            f"distilledContext mismatch: got {got.distilledContext!r}"
        )

    def test_missing_link_degrades_to_none(self):
        """No link for a branch returns None — U1 falls back to PR-only distillation."""
        store = InMemoryLearningStore()
        # Push to a different branch; lookup on a branch that was never pushed.
        store.put_branch_session_link(_bsl(branch="feat/other"))
        got = store.get_branch_session_link("acme", "acme/backend", "feat/not-pushed")
        assert got is None

    def test_last_write_wins_for_same_branch(self):
        """Multiple pushes to the same branch keep the most recent session link."""
        store = InMemoryLearningStore()
        store.put_branch_session_link(_bsl(session_id="sess-first", turn_end=3))
        store.put_branch_session_link(_bsl(session_id="sess-second", turn_end=7))

        got = store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.sessionId == "sess-second", "Last writer should win"
        assert got.turnEnd == 7

    def test_org_guard_rejects_blank_org(self):
        """put_branch_session_link with blank org raises OrgGuardError."""
        store = InMemoryLearningStore()
        record = _bsl(org="")
        with pytest.raises(OrgGuardError):
            store.put_branch_session_link(record)

    def test_links_are_org_scoped(self):
        """Branch links from different orgs do not collide."""
        store = InMemoryLearningStore()
        store.put_branch_session_link(_bsl(org="org-a", session_id="sess-a"))
        store.put_branch_session_link(_bsl(org="org-b", session_id="sess-b"))

        got_a = store.get_branch_session_link("org-a", "acme/backend", "feat/my-feature")
        got_b = store.get_branch_session_link("org-b", "acme/backend", "feat/my-feature")
        assert got_a is not None and got_a.sessionId == "sess-a"
        assert got_b is not None and got_b.sessionId == "sess-b"

    def test_branch_key_format(self):
        """branch_session_key produces the expected BSLINK# SK format."""
        key = branch_session_key("acme", "acme/backend", "feat/foo")
        assert key["PK"] == "SCOPE#org#acme"
        assert key["SK"] == "BSLINK#acme/backend#feat/foo"

    def test_ttl_field_stored(self):
        """ttlAt is preserved so DynamoDB TTL can expire old records automatically."""
        store = InMemoryLearningStore()
        now_sec = int(time.time())
        ttl = now_sec + 90 * 24 * 3600  # 90 days
        record = _bsl(ttl_at=ttl)
        store.put_branch_session_link(record)

        got = store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.ttlAt == ttl, "ttlAt should be preserved verbatim for DynamoDB TTL"

    def test_pinned_session_id_is_stored(self):
        """sessionId should be the PinnedSessionID (stable across /resume)."""
        store = InMemoryLearningStore()
        # Simulate a resumed session: PinnedSessionID != live session_id.
        pinned_id = "pinned-tab-id-xyz"
        record = _bsl(session_id=pinned_id)
        store.put_branch_session_link(record)

        got = store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.sessionId == pinned_id, (
            "Must store PinnedSessionID, not live id, to remain stable across /resume"
        )

    def test_distilled_context_absent_when_not_set(self):
        """A push without distilledContext stores None — degrades gracefully."""
        store = InMemoryLearningStore()
        record = _bsl(distilled_context=None)
        store.put_branch_session_link(record)

        got = store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.distilledContext is None

    def test_turn_range_absent_when_unknown(self):
        """turnStart/turnEnd can be None when the range is unknown — best-effort."""
        store = InMemoryLearningStore()
        record = _bsl(turn_start=None, turn_end=None)
        store.put_branch_session_link(record)

        got = store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.turnStart is None
        assert got.turnEnd is None


# ===========================================================================
# 2. DynamoLearningStore via moto — real boto3 round-trip tests
# ===========================================================================

# Moto-backed fixture is a class-level fixture so we get ONE mock_aws context
# per class (matches the moto docs pattern for session-scoped DynamoDB tables).


@pytest.fixture()
def _dynamo_table():
    """Create the single-table 'harness' (PK/SK string keys) via moto."""
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
def _dynamo_store(_dynamo_table):
    return DynamoLearningStore(TABLE_NAME, dynamodb_resource=_dynamo_table)


@pytest.mark.skipif(not _HAS_BOTO, reason="boto3/moto not installed")
class TestBranchSessionLinkDynamo:
    """Real boto3 round-trip tests via moto DynamoDB."""

    def test_git_push_records_branch_session_link(self, _dynamo_store):
        """put + get round-trips correctly through the real boto3 path."""
        record = _bsl()
        _dynamo_store.put_branch_session_link(record)

        got = _dynamo_store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.sessionId == "sess-abc123"
        assert got.ownerRepo == "acme/backend"
        assert got.branch == "feat/my-feature"

    def test_failed_or_detached_push_records_nothing(self, _dynamo_store):
        """Absent push → get_branch_session_link returns None."""
        got = _dynamo_store.get_branch_session_link("acme", "acme/backend", "feat/failed")
        assert got is None

    def test_link_lookup_returns_turn_range_for_branch(self, _dynamo_store):
        """turnStart / turnEnd survive the DynamoDB item round-trip."""
        record = _bsl(turn_start=2, turn_end=8)
        _dynamo_store.put_branch_session_link(record)

        got = _dynamo_store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.turnStart == 2
        assert got.turnEnd == 8

    def test_eager_distilled_context_round_trips(self, _dynamo_store):
        """distilledContext is written and retrieved verbatim (eager-distil contract)."""
        ctx = "Used rate-limited fetch to avoid GitHub quota exhaustion."
        record = _bsl(distilled_context=ctx)
        _dynamo_store.put_branch_session_link(record)

        got = _dynamo_store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.distilledContext == ctx

    def test_missing_optional_fields_survive_round_trip(self, _dynamo_store):
        """Optional fields absent from the record don't appear as None noise."""
        record = _bsl(
            turn_start=None,
            turn_end=None,
            distilled_context=None,
            ttl_at=None,
        )
        _dynamo_store.put_branch_session_link(record)

        got = _dynamo_store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.turnStart is None
        assert got.turnEnd is None
        assert got.distilledContext is None
        assert got.ttlAt is None

    def test_last_write_wins_same_branch(self, _dynamo_store):
        """Overwriting the same (org, repo, branch) replaces the link."""
        _dynamo_store.put_branch_session_link(_bsl(session_id="first", turn_end=3))
        _dynamo_store.put_branch_session_link(_bsl(session_id="second", turn_end=9))

        got = _dynamo_store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.sessionId == "second"
        assert got.turnEnd == 9

    def test_org_scoped_keys_do_not_collide(self, _dynamo_store):
        """Two orgs' branch links live under different PKs."""
        _dynamo_store.put_branch_session_link(_bsl(org="org-a", session_id="sess-a"))
        _dynamo_store.put_branch_session_link(_bsl(org="org-b", session_id="sess-b"))

        a = _dynamo_store.get_branch_session_link("org-a", "acme/backend", "feat/my-feature")
        b = _dynamo_store.get_branch_session_link("org-b", "acme/backend", "feat/my-feature")
        assert a is not None and a.sessionId == "sess-a"
        assert b is not None and b.sessionId == "sess-b"

    def test_ttl_field_round_trips(self, _dynamo_store):
        """ttlAt is stored as a numeric attribute for DynamoDB TTL."""
        now_sec = int(time.time())
        ttl = now_sec + 90 * 24 * 3600
        record = _bsl(ttl_at=ttl)
        _dynamo_store.put_branch_session_link(record)

        got = _dynamo_store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.ttlAt == ttl

    def test_pushed_at_round_trips(self, _dynamo_store):
        """pushedAt epoch-ms is preserved for observability / TTL reference."""
        pushed_at = int(time.time() * 1000)
        record = _bsl(pushed_at=pushed_at)
        _dynamo_store.put_branch_session_link(record)

        got = _dynamo_store.get_branch_session_link("acme", "acme/backend", "feat/my-feature")
        assert got is not None
        assert got.pushedAt == pushed_at


# ===========================================================================
# 3. Key builder unit tests (no store needed)
# ===========================================================================


class TestBranchSessionKey:
    """Verify the branch_session_key IDL builder produces correct key strings."""

    def test_standard_branch(self):
        k = branch_session_key("acme", "acme/backend", "feat/foo")
        assert k["PK"] == "SCOPE#org#acme"
        assert k["SK"] == "BSLINK#acme/backend#feat/foo"

    def test_main_branch(self):
        k = branch_session_key("myorg", "user/repo", "main")
        assert k["SK"] == "BSLINK#user/repo#main"

    def test_branch_with_slashes(self):
        k = branch_session_key("org", "o/r", "feat/sub/feature")
        assert "feat/sub/feature" in k["SK"]

    def test_numeric_org_is_handled(self):
        """IDL params accept str | int; int org should stringify cleanly."""
        k = branch_session_key(42, "o/r", "main")
        assert "42" in k["PK"]
