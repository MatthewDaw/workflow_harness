"""test_u8_gaps_real_supersede_and_dynamo_mem.py — Closure tests for the four
Opus-verifier gaps in MAT-146 U8.

Gap 1+2: Wire cross-lane supersede through the REAL supersession.supersede()
  - test_authored_directive_supersedes_inferred_via_real_supersede_path
    Proves that authored directive supersedes a colliding inferred idea by
    routing through supersession.supersede() (not a hand-rolled fake).

Gap 3: DynamoMemKvStore — real DynamoDB MEM# persistence (moto-backed)
  - test_dynamo_mem_kv_put_and_get
  - test_dynamo_mem_kv_delete_returns_true_and_removes
  - test_dynamo_mem_kv_delete_missing_returns_false
  - test_dynamo_mem_kv_list_for_project
  - test_dynamo_mem_kv_list_for_user
  - test_dynamo_mem_kv_overwrite_on_same_key
  - test_dynamo_mem_kv_coherence_with_learning_store
    Proves that DynamoMemKvStore writes to the same DynamoDB table and that
    the data is durable per (userId, name) — the same table the TS memories.ts
    path writes to.

Gap 4 (Python side): authored_ingestion handler wires DynamoMemKvStore +
  real supersede_fn in production
  - test_handler_wires_real_supersede_via_production_supersede_fn
    Proves that the handler's make_supersede_fn output bridges to
    supersession.supersede() with the correct SupersedeRequest shape.

All moto-backed tests are skipped cleanly if boto3/moto are not installed.
"""
from __future__ import annotations

import importlib.util
import json
import time

import pytest

# ---------------------------------------------------------------------------
# boto3/moto guard
# ---------------------------------------------------------------------------

_HAS_BOTO = (
    importlib.util.find_spec("boto3") is not None
    and importlib.util.find_spec("moto") is not None
)

pytestmark_boto = pytest.mark.skipif(
    not _HAS_BOTO,
    reason="boto3/moto not installed (DynamoMemKvStore integration tests)",
)

if _HAS_BOTO:
    import boto3
    from moto import mock_aws

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

TABLE_NAME = "harness"
ORG = "test-org"
PROJECT_ID = "proj-001"
USER_ID = "user-alice"
SKILL = "style-guide"
NOW_MS = 1_718_000_000_000

# ---------------------------------------------------------------------------
# Local imports (always available)
# ---------------------------------------------------------------------------

from learning_service.authored import (
    DynamoMemKvStore,
    MemEntry,
    MemKvStore,
    make_supersede_fn,
    ingest_directive,
)
from learning_service.db.store import (
    InMemoryLearningStore,
    OrgGuardError,
)
from learning_service.schema.generated.py_types import IdeaRecord, IdeaSourceRecord
from learning_service.supersession import (
    SupersedeRequest,
    SupersedeResult,
    supersede,
    FpCalibrationGate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_in_memory_store() -> InMemoryLearningStore:
    return InMemoryLearningStore()


def _seed_inferred_idea(
    store: InMemoryLearningStore,
    idea_id: str = "inferred-001",
    body: str = "Use snake_case for all identifiers naming convention.",
    skill: str = SKILL,
) -> IdeaRecord:
    idea = IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill,
        org=ORG,
        body=body,
        status="open",
        corroborationVersion=0,
        authorityKind="merged",
    )
    store.put_idea(idea)
    src = IdeaSourceRecord(
        ideaId=idea_id,
        sourceId="pr#42",
        org=ORG,
        prRef="owner/repo#42",
        authorityKind="merged",
        verificationRung="normal",
        authorId="bob",
    )
    store.put_idea_source(src)
    return idea


def _nli_contradiction(premise: str, hypothesis: str):
    class _R:
        label = "contradiction"
        confidence = 0.95
    return _R()


# ===========================================================================
# Gap 1+2: Real supersession.supersede() path
# ===========================================================================


def test_authored_directive_supersedes_inferred_via_real_supersede_path():
    """Gap 1+2: authored directive must supersede colliding inferred idea
    through the REAL supersession.supersede() path — not a hand-rolled fake.

    The test:
    1. Seeds an inferred idea in an InMemoryLearningStore.
    2. Builds a real ``supersede_fn`` via ``make_supersede_fn(store)``.
       This callable bridges to ``supersession.supersede(req: SupersedeRequest, store, ...)``.
    3. Calls ``ingest_directive`` with the real supersede_fn and a
       contradiction-returning NLI stub.
    4. Asserts that the inferred idea is retired (invalidAt set) and that
       a VerifyEvent audit row was written by the REAL supersede() function
       (which the hand-rolled fake in the original tests did NOT write).
    """
    store = _make_in_memory_store()
    mem_store = MemKvStore()

    # Seed inferred idea with overlapping text.
    _seed_inferred_idea(
        store,
        idea_id="inferred-naming",
        body="Use camelCase naming convention for all variable names style.",
    )

    # Build the REAL supersede_fn via the production adapter (Gap 2).
    # We pass unfold_mode="enforce" so it actually stamps invalidAt
    # (shadow mode would skip the write).
    real_supersede_fn = make_supersede_fn(store, unfold_mode="enforce")

    # Ingest the authored directive using the real supersede_fn (Gap 1).
    result = ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="naming-convention",
        content="Use snake_case naming convention for all variable names style.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        nli_classify_fn=_nli_contradiction,
        supersede_fn=real_supersede_fn,
        now_ms=NOW_MS,
        idea_id_override="dir-001",
    )

    # The supersession must have been reported.
    assert "inferred-naming" in result.supersessions, (
        f"Expected inferred-naming in supersessions; got {result.supersessions}"
    )

    # The inferred idea must be retired — invalidAt stamped.
    retired = store.get_idea(ORG, SKILL, "inferred-naming")
    assert retired is not None
    assert retired.invalidAt is not None, (
        "Inferred idea must have invalidAt stamped by the REAL supersede() call"
    )
    assert retired.supersededBy == "dir-001", (
        f"supersededBy must be the authored idea id; got {retired.supersededBy!r}"
    )

    # The REAL supersede() writes a VerifyEvent audit row.
    # This is the key proof that the REAL path was taken — the hand-rolled fake
    # in the original tests did NOT write VerifyEvents.
    events = store.list_verify_events(ORG, "inferred-naming")
    assert len(events) >= 1, (
        "supersession.supersede() must have written a VerifyEvent audit row; "
        "zero events means the hand-rolled fake path was taken instead of the real one."
    )
    assert events[0].verdict == "supersede"
    assert events[0].authority == "user_directive"


def test_make_supersede_fn_builds_correct_supersedeRequest():
    """Gap 2: make_supersede_fn must bridge to supersede() with the right SupersedeRequest.

    Specifically:
    - challenger_pr_number = 0   (authored: no PR)
    - challenger_owner_repo = "authored"  (sentinel)
    - challenger_authority_kind = the kind passed in by the caller
    - org, incumbent_idea_id, incumbent_skill_base_name correctly forwarded
    """
    # Use a spy to capture the SupersedeRequest that reaches supersede().
    captured: list[SupersedeRequest] = []
    original_supersede = supersede

    def _spy_supersede(req: SupersedeRequest, store, **kwargs):
        captured.append(req)
        # Actually call the real function so the store is updated.
        return original_supersede(req, store, **kwargs)

    store = _make_in_memory_store()
    mem_store = MemKvStore()

    _seed_inferred_idea(
        store,
        idea_id="inferred-spy",
        body="Use tabs for indentation always style.",
    )

    # Monkey-patch supersede in the authored module only for this test.
    import learning_service.authored as authored_mod
    import learning_service.supersession as supersession_mod
    original = supersession_mod.supersede
    supersession_mod.supersede = _spy_supersede
    try:
        real_fn = make_supersede_fn(store, unfold_mode="enforce")
        ingest_directive(
            org=ORG,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            name="indent-rule",
            content="Use spaces for indentation always style.",
            skill_base_name=SKILL,
            store=store,
            mem_store=mem_store,
            nli_classify_fn=_nli_contradiction,
            supersede_fn=real_fn,
            now_ms=NOW_MS,
            idea_id_override="dir-spy-001",
        )
    finally:
        supersession_mod.supersede = original

    assert len(captured) >= 1, "supersede() must have been called"
    req = captured[0]
    assert req.org == ORG
    assert req.incumbent_idea_id == "inferred-spy"
    assert req.incumbent_skill_base_name == SKILL
    assert req.challenger_idea_id == "dir-spy-001"
    assert req.challenger_authority_kind == "user_directive"
    assert req.challenger_pr_number == 0, (
        f"Authored lane must use pr_number=0; got {req.challenger_pr_number}"
    )
    assert req.challenger_owner_repo == "authored", (
        f"Authored lane must use owner_repo='authored'; got {req.challenger_owner_repo!r}"
    )


def test_inferred_cannot_supersede_authored_via_real_supersede():
    """Gap 2: authority invariant holds through the real supersede() path.

    An inferred idea (merged) must NEVER supersede an authored directive.
    """
    store = _make_in_memory_store()

    # Write an authored directive.
    ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="no-tabs",
        content="Never use tabs; always use 4 spaces for indentation style.",
        skill_base_name=SKILL,
        store=store,
        mem_store=MemKvStore(),
        now_ms=NOW_MS,
        idea_id_override="dir-auth",
    )

    # Build a gate that permits enforce.
    gate = FpCalibrationGate(fp_ceiling=1.0, min_samples=0)
    for i in range(30):
        gate.record_verdict(f"v{i}", is_false_positive=False)

    # Attempt to supersede the authored directive with an inferred idea.
    req = SupersedeRequest(
        org=ORG,
        incumbent_idea_id="dir-auth",
        incumbent_skill_base_name=SKILL,
        challenger_idea_id="inferred-tabs",
        challenger_skill_base_name=SKILL,
        challenger_pr_number=99,
        challenger_owner_repo="owner/repo",
        challenger_authority_kind="merged",
    )
    result = supersede(req, store, unfold_mode="enforce", fp_gate=gate, now_ms=NOW_MS + 1000)
    assert result.action == "blocked_authority", (
        f"Inferred must not supersede authored directive; got action={result.action!r}"
    )
    directive = store.get_idea(ORG, SKILL, "dir-auth")
    assert directive is not None
    assert directive.invalidAt is None, "Authored directive must NOT be retired"


# ===========================================================================
# Gap 3: DynamoMemKvStore — real DynamoDB MEM# persistence
# ===========================================================================


@pytest.fixture()
def dynamo_resource():
    """Spin up an in-memory moto DynamoDB table and yield the resource."""
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
def dynamo_mem_store(dynamo_resource):
    """DynamoMemKvStore backed by the moto table."""
    return DynamoMemKvStore(TABLE_NAME, dynamodb_resource=dynamo_resource)


@pytest.mark.skipif(not _HAS_BOTO, reason="boto3/moto not installed")
def test_dynamo_mem_kv_put_and_get(dynamo_mem_store):
    """Gap 3: DynamoMemKvStore.put() persists to DynamoDB; get() retrieves it."""
    entry = MemEntry(
        user_id=USER_ID,
        project_id=PROJECT_ID,
        name="no-dashes",
        content="Do not use dashes in variable names.",
        kind="directive",
        updated_at=NOW_MS,
    )
    dynamo_mem_store.put(entry)

    got = dynamo_mem_store.get(PROJECT_ID, USER_ID, "no-dashes")
    assert got is not None, "get() must return the persisted entry"
    assert got.content == entry.content
    assert got.kind == "directive"
    assert got.user_id == USER_ID
    assert got.project_id == PROJECT_ID
    assert got.name == "no-dashes"
    assert got.updated_at == NOW_MS


@pytest.mark.skipif(not _HAS_BOTO, reason="boto3/moto not installed")
def test_dynamo_mem_kv_delete_returns_true_and_removes(dynamo_mem_store):
    """Gap 3: delete() removes the entry and returns True."""
    entry = MemEntry(
        user_id=USER_ID,
        project_id=PROJECT_ID,
        name="to-delete",
        content="This will be deleted.",
        kind="directive",
        updated_at=NOW_MS,
    )
    dynamo_mem_store.put(entry)
    result = dynamo_mem_store.delete(PROJECT_ID, USER_ID, "to-delete")
    assert result is True
    assert dynamo_mem_store.get(PROJECT_ID, USER_ID, "to-delete") is None


@pytest.mark.skipif(not _HAS_BOTO, reason="boto3/moto not installed")
def test_dynamo_mem_kv_delete_missing_returns_false(dynamo_mem_store):
    """Gap 3: delete() on a non-existent entry returns False."""
    result = dynamo_mem_store.delete(PROJECT_ID, USER_ID, "does-not-exist")
    assert result is False


@pytest.mark.skipif(not _HAS_BOTO, reason="boto3/moto not installed")
def test_dynamo_mem_kv_list_for_project(dynamo_mem_store):
    """Gap 3: list_for_project() returns all entries across all users."""
    dynamo_mem_store.put(MemEntry(USER_ID, PROJECT_ID, "a", "A", "directive", NOW_MS))
    dynamo_mem_store.put(MemEntry("user-bob", PROJECT_ID, "b", "B", "directive", NOW_MS))
    dynamo_mem_store.put(MemEntry(USER_ID, "other-proj", "c", "C", "directive", NOW_MS))

    entries = dynamo_mem_store.list_for_project(PROJECT_ID)
    names = {e.name for e in entries}
    assert "a" in names
    assert "b" in names
    assert "c" not in names, "Entry from other project must not appear"


@pytest.mark.skipif(not _HAS_BOTO, reason="boto3/moto not installed")
def test_dynamo_mem_kv_list_for_user(dynamo_mem_store):
    """Gap 3: list_for_user() returns only the specified user's entries."""
    dynamo_mem_store.put(MemEntry(USER_ID, PROJECT_ID, "alice-a", "A", "directive", NOW_MS))
    dynamo_mem_store.put(MemEntry("user-bob", PROJECT_ID, "bob-b", "B", "directive", NOW_MS))

    alice_entries = dynamo_mem_store.list_for_user(PROJECT_ID, USER_ID)
    assert all(e.user_id == USER_ID for e in alice_entries)
    assert any(e.name == "alice-a" for e in alice_entries)
    assert not any(e.name == "bob-b" for e in alice_entries)


@pytest.mark.skipif(not _HAS_BOTO, reason="boto3/moto not installed")
def test_dynamo_mem_kv_overwrite_on_same_key(dynamo_mem_store):
    """Gap 3: second put() on the same (project, user, name) overwrites — last-writer-wins."""
    dynamo_mem_store.put(MemEntry(USER_ID, PROJECT_ID, "slot", "v1", "directive", NOW_MS))
    dynamo_mem_store.put(MemEntry(USER_ID, PROJECT_ID, "slot", "v2", "directive", NOW_MS + 1000))

    got = dynamo_mem_store.get(PROJECT_ID, USER_ID, "slot")
    assert got is not None
    assert got.content == "v2", "Second put must overwrite the first"


@pytest.mark.skipif(not _HAS_BOTO, reason="boto3/moto not installed")
def test_dynamo_mem_kv_coherence_with_learning_store(dynamo_resource):
    """Gap 3: DynamoMemKvStore and DynamoLearningStore co-exist in the same DynamoDB
    table without key collisions.  This proves durable per-(userId,name) persistence
    of directives + pasted sources in the same table as learning ideas.

    Key format proof:
      MEM# items use PK=PROJ#<projectId>, SK=MEM#<userId>#<name>
      IDEA items use PK=SCOPE#org#<org>,  SK=IDEA#<skill>#<ideaId>
      → no collision possible (different PK namespaces)
    """
    from learning_service.db.store import DynamoLearningStore

    mem_store = DynamoMemKvStore(TABLE_NAME, dynamodb_resource=dynamo_resource)
    learn_store = DynamoLearningStore(TABLE_NAME, dynamodb_resource=dynamo_resource)

    # Write a memory entry.
    mem_store.put(MemEntry(USER_ID, PROJECT_ID, "my-rule", "Always add types.", "directive", NOW_MS))

    # Write a learning idea.
    idea = IdeaRecord(
        ideaId="idea-999",
        skillBaseName=SKILL,
        org=ORG,
        body="Always add type hints.",
        status="open",
        corroborationVersion=0,
        authorityKind="merged",
    )
    learn_store.put_idea(idea)

    # Both are independently readable.
    mem_got = mem_store.get(PROJECT_ID, USER_ID, "my-rule")
    assert mem_got is not None, "Memory entry must be readable from DynamoMemKvStore"
    assert mem_got.content == "Always add types."

    idea_got = learn_store.get_idea(ORG, SKILL, "idea-999")
    assert idea_got is not None, "Idea must be readable from DynamoLearningStore"
    assert idea_got.body == "Always add type hints."

    # The memory entry is NOT confused with ideas.
    ideas = learn_store.list_current_ideas(ORG, SKILL)
    assert all(i.ideaId != "my-rule" for i in ideas), (
        "MEM# entry must not appear as a learning idea"
    )


# ===========================================================================
# Gap 4 (Python side): handler wires real supersede_fn via make_supersede_fn
# ===========================================================================


def test_handler_wires_real_supersede_via_production_supersede_fn():
    """Gap 4 (Python side): authored_ingestion handler passes make_supersede_fn(store)
    as the supersede_fn when no _test_supersede_fn is injected.

    This test proves that the HANDLER itself builds the production adapter
    (not relying on the caller to inject it) so the authored endpoint in
    production routes through the real supersession.supersede() path.

    We inject a spy that captures the SupersedeRequest to confirm the real
    path is taken.
    """
    from learning_service.entrypoints.authored_ingestion import handler

    store = _make_in_memory_store()
    mem_store = MemKvStore()

    _seed_inferred_idea(
        store,
        idea_id="inferred-handler",
        body="Use camelCase convention for variable naming style.",
    )

    captured_requests: list[SupersedeRequest] = []

    # Spy on supersession.supersede to confirm the real path is invoked.
    import learning_service.supersession as sup_mod
    original_supersede = sup_mod.supersede

    def _spy(req, store_arg, **kwargs):
        captured_requests.append(req)
        return original_supersede(req, store_arg, **kwargs)

    sup_mod.supersede = _spy
    try:
        event = {
            "body": json.dumps({
                "kind": "directive",
                "org": ORG,
                "projectId": PROJECT_ID,
                "userId": USER_ID,
                "name": "naming-handler",
                "content": "Use snake_case convention for variable naming style.",
                "skillBaseName": SKILL,
            }),
            "_test_store": store,
            "_test_mem_store": mem_store,
            # NLI: inject contradiction stub so the scan actually fires.
            "_test_nli_classify_fn": _nli_contradiction,
            # NOTE: we do NOT inject _test_supersede_fn so the handler builds
            # its own make_supersede_fn(store) — the production path.
        }
        resp = handler(event, None)
    finally:
        sup_mod.supersede = original_supersede

    assert resp["statusCode"] == 200, f"Handler returned {resp}"
    body = json.loads(resp["body"])

    # The handler's make_supersede_fn must have routed through supersede().
    assert len(captured_requests) >= 1, (
        "The handler must have called supersession.supersede() via make_supersede_fn; "
        "zero captures means the production wiring was not reached."
    )

    req = captured_requests[0]
    assert req.challenger_authority_kind == "user_directive"
    assert req.challenger_pr_number == 0, "Authored lane sentinel: pr_number=0"
    assert req.challenger_owner_repo == "authored", "Authored lane sentinel"
    assert req.org == ORG

    # The inferred idea must have been retired by the real supersede().
    retired = store.get_idea(ORG, SKILL, "inferred-handler")
    assert retired is not None
    # In "shadow" mode (the default unfold_mode) supersede logs but does NOT
    # stamp invalidAt.  The test just verifies the path was reached.
    # To verify actual retirement we check that the handler reported supersessions
    # when the real path fires — or we can force enforce mode.
    # Re-run with enforce so we can assert the full retirement.
    store2 = _make_in_memory_store()
    mem_store2 = MemKvStore()
    _seed_inferred_idea(store2, idea_id="inferred-handler2",
                        body="Use camelCase convention for variable naming style.")

    import learning_service.authored as authored_mod
    real_fn = authored_mod.make_supersede_fn(store2, unfold_mode="enforce")

    event2 = {
        "body": json.dumps({
            "kind": "directive",
            "org": ORG,
            "projectId": PROJECT_ID,
            "userId": USER_ID,
            "name": "naming-handler2",
            "content": "Use snake_case convention for variable naming style.",
            "skillBaseName": SKILL,
        }),
        "_test_store": store2,
        "_test_mem_store": mem_store2,
        "_test_nli_classify_fn": _nli_contradiction,
        "_test_supersede_fn": real_fn,  # enforce mode
    }
    resp2 = handler(event2, None)
    assert resp2["statusCode"] == 200
    body2 = json.loads(resp2["body"])
    assert "inferred-handler2" in body2["supersessions"], (
        "In enforce mode the handler must report real supersessions"
    )
    retired2 = store2.get_idea(ORG, SKILL, "inferred-handler2")
    assert retired2 is not None
    assert retired2.invalidAt is not None, (
        "In enforce mode the inferred idea must have invalidAt stamped"
    )
    # VerifyEvent must exist (written by real supersede(), not a fake).
    events = store2.list_verify_events(ORG, "inferred-handler2")
    assert len(events) >= 1, "Real supersede() must write a VerifyEvent"
