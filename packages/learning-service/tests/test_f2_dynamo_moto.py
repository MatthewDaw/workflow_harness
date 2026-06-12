"""MAT-149 (F2) — moto/DynamoDB-Local-backed tests for the PRODUCTION store.

These exercise the *real* ``DynamoLearningStore`` and ``S3VectorStore`` code
paths — the boto3 ``_idea_to_dynamo`` / ``_dynamo_to_idea`` round-trip, the
``ConditionExpression`` optimistic-concurrency writes, and the
``FilterExpression`` queries (incl. invalidAt exclusion) — against a mocked AWS
backend (``moto`` for DynamoDB, an in-process mock for the brand-new s3vectors
client which moto does not yet model).

This is the gap the in-memory fakes could not cover: those tests passed "by
construction" because they never touched boto3.  Here every assertion runs
through the production class against a mock DynamoDB table / s3vectors client.

Skips cleanly if boto3/moto are not installed so the offline gate still runs.
"""

from __future__ import annotations

import importlib.util

import pytest

# Skip the whole module if boto3 / moto are unavailable (offline gate).
_HAS_BOTO = (
    importlib.util.find_spec("boto3") is not None
    and importlib.util.find_spec("moto") is not None
)
pytestmark = pytest.mark.skipif(
    not _HAS_BOTO, reason="boto3/moto not installed (production-store integration tests)"
)

if _HAS_BOTO:
    import boto3
    from moto import mock_aws

from learning_service.db.store import (
    DynamoLearningStore,
    OrgGuardError,
    ProcessedPrRecord,
    S3VectorStore,
    VectorItem,
    VersionConflictError,
    VerifyEventRecord,
    _dynamo_to_idea,
    _idea_to_dynamo,
)
from learning_service.schema.generated.py_types import (
    AnchorRecord,
    GoldenCaseRecord,
    IdeaRecord,
    IdeaSourceRecord,
    idea_key,
)

TABLE_NAME = "harness"


# ---------------------------------------------------------------------------
# Fixtures: a real (moto) single-table DynamoDB table + a DynamoLearningStore.
# ---------------------------------------------------------------------------


@pytest.fixture()
def dynamo_table():
    """Create the single-table ``harness`` (PK/SK string keys) in moto."""
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
def store(dynamo_table):
    return DynamoLearningStore(TABLE_NAME, dynamodb_resource=dynamo_table)


def _idea(
    *,
    org="acme",
    skill="my-skill",
    idea_id="idea-001",
    status="open",
    version=0,
    invalid_at=None,
    **extra,
) -> IdeaRecord:
    return IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill,
        org=org,
        body=f"body for {idea_id}",
        status=status,
        corroborationVersion=version,
        invalidAt=invalid_at,
        **extra,
    )


# ===========================================================================
# 1. Idea CRUD + full-field round-trip against mock DynamoDB
# ===========================================================================


class TestIdeaCrudMoto:
    def test_put_and_get_idea(self, store):
        store.put_idea(_idea())
        got = store.get_idea("acme", "my-skill", "idea-001")
        assert got is not None
        assert got.ideaId == "idea-001"
        assert got.body == "body for idea-001"

    def test_get_missing_returns_none(self, store):
        assert store.get_idea("acme", "my-skill", "nope") is None

    def test_all_gap6_fields_round_trip_through_dynamo(self, store):
        """The U10 fields must survive _idea_to_dynamo → DynamoDB → _dynamo_to_idea."""
        rec = _idea(
            idea_id="rich",
            version=3,
            status="folded",
            foldedIntoRev=7,
            supersededBy="other",
            supersedes=["a", "b"],
            authored=True,
            authorityKind="user_directive",
            refines="parent-idea",
            revivedAt=1234567890123,
            legacyRecurrenceFold=True,
            sourceRef='{"kind":"authored_import","label":"doc"}',
            scopeTag="repo",
            authorId="dev-7",
            verificationRung="test",
        )
        store.put_idea(rec)
        got = store.get_idea("acme", "my-skill", "rich")
        assert got is not None
        # Every field made the round trip — none silently dropped.
        assert got.corroborationVersion == 3
        assert got.status == "folded"
        assert got.foldedIntoRev == 7
        assert got.supersededBy == "other"
        assert got.supersedes == ["a", "b"]
        assert got.authored is True
        assert got.authorityKind == "user_directive"
        assert got.refines == "parent-idea"
        assert got.revivedAt == 1234567890123
        assert got.legacyRecurrenceFold is True
        assert got.sourceRef == '{"kind":"authored_import","label":"doc"}'
        assert got.scopeTag == "repo"
        assert got.authorId == "dev-7"
        assert got.verificationRung == "test"

    def test_idea_to_dynamo_omits_unset_optionals(self, store):
        """An unset invalidAt must be ABSENT (not null) so attribute_not_exists works."""
        item = _idea_to_dynamo(_idea())
        assert "invalidAt" not in item
        assert "refines" not in item
        # And the round-trip helper reconstructs it as None.
        assert _dynamo_to_idea(item).invalidAt is None

    def test_org_guard_blocks_blank_org(self, store):
        with pytest.raises(OrgGuardError):
            store.put_idea(_idea(org=""))


# ===========================================================================
# 2. Conditional writes (OCC / VersionConflict) against mock DynamoDB
# ===========================================================================


class TestConditionalWritesMoto:
    def test_first_write_with_sentinel_succeeds(self, store):
        """expected_version=-1 → attribute_not_exists; first write lands."""
        store.put_idea_conditional(_idea(version=0), expected_version=-1)
        assert store.get_idea("acme", "my-skill", "idea-001") is not None

    def test_first_write_with_sentinel_fails_if_exists(self, store):
        store.put_idea(_idea(version=0))
        with pytest.raises(VersionConflictError):
            store.put_idea_conditional(_idea(version=1), expected_version=-1)

    def test_conditional_update_with_correct_version(self, store):
        store.put_idea(_idea(version=0))
        store.put_idea_conditional(
            _idea(status="folded", version=1), expected_version=0
        )
        got = store.get_idea("acme", "my-skill", "idea-001")
        assert got.status == "folded"
        assert got.corroborationVersion == 1

    def test_conditional_update_with_stale_version_conflicts(self, store):
        store.put_idea(_idea(version=0))
        # Concurrent writer bumped to 1.
        store.put_idea_conditional(_idea(version=1), expected_version=0)
        # Our stale write still thinks version is 0.
        with pytest.raises(VersionConflictError) as exc:
            store.put_idea_conditional(_idea(version=1), expected_version=0)
        assert exc.value.idea_id == "idea-001"
        assert exc.value.current == 1

    def test_conditional_update_on_missing_record_conflicts(self, store):
        with pytest.raises(VersionConflictError):
            store.put_idea_conditional(_idea(version=1), expected_version=0)


# ===========================================================================
# 3. Query FilterExpressions: invalidAt exclusion + current-set reads
# ===========================================================================


class TestQueryFiltersMoto:
    def test_list_current_ideas_excludes_invalid_at(self, store):
        store.put_idea(_idea(idea_id="active"))
        store.put_idea(_idea(idea_id="retired", invalid_at=999999))
        current = store.list_current_ideas("acme", "my-skill")
        ids = {r.ideaId for r in current}
        assert "active" in ids
        assert "retired" not in ids  # FilterExpression attribute_not_exists(invalidAt)

    def test_get_idea_still_returns_retired_for_history(self, store):
        store.put_idea(_idea(idea_id="retired", invalid_at=999999))
        got = store.get_idea("acme", "my-skill", "retired")
        assert got is not None and got.invalidAt == 999999

    def test_list_all_ideas_for_org_includes_retired(self, store):
        store.put_idea(_idea(idea_id="a", skill="s1"))
        store.put_idea(_idea(idea_id="b", skill="s1", invalid_at=1))
        store.put_idea(_idea(idea_id="c", skill="s2"))
        ids = {r.ideaId for r in store.list_all_ideas_for_org("acme")}
        assert ids == {"a", "b", "c"}

    def test_list_current_scoped_to_skill_via_begins_with(self, store):
        store.put_idea(_idea(idea_id="i1", skill="skill-a"))
        store.put_idea(_idea(idea_id="i2", skill="skill-b"))
        ids = {r.ideaId for r in store.list_current_ideas("acme", "skill-a")}
        assert ids == {"i1"}

    def test_current_set_does_not_leak_across_orgs(self, store):
        store.put_idea(_idea(org="acme", idea_id="acme-idea"))
        store.put_idea(_idea(org="rival", idea_id="rival-idea"))
        ids = {r.ideaId for r in store.list_current_ideas("acme", "my-skill")}
        assert "acme-idea" in ids and "rival-idea" not in ids


# ===========================================================================
# 4. Idea sources (new record type) — CRUD + query
# ===========================================================================


class TestIdeaSourceMoto:
    def test_put_get_and_list_idea_sources(self, store):
        s1 = IdeaSourceRecord(
            ideaId="idea-001", sourceId="acme/backend#10", org="acme",
            prRef="acme/backend#10", authorityKind="merged",
            verificationRung="test", authorId="dev-1",
            anchors='[{"file":"a.ts","symbol":"f"}]',
        )
        s2 = IdeaSourceRecord(
            ideaId="idea-001", sourceId="acme/backend#20", org="acme",
            prRef="acme/backend#20", authorityKind="merged",
            verificationRung="normal", authorId="dev-2",
        )
        store.put_idea_source(s1)
        store.put_idea_source(s2)

        got = store.get_idea_source("acme", "idea-001", "acme/backend#10")
        assert got is not None
        assert got.prRef == "acme/backend#10"
        assert got.verificationRung == "test"
        assert got.anchors == '[{"file":"a.ts","symbol":"f"}]'

        sources = store.list_idea_sources("acme", "idea-001")
        assert {s.sourceId for s in sources} == {"acme/backend#10", "acme/backend#20"}

    def test_idea_source_org_guard(self, store):
        with pytest.raises(OrgGuardError):
            store.put_idea_source(
                IdeaSourceRecord(ideaId="i", sourceId="s", org="")
            )

    def test_idea_source_requires_identity(self, store):
        with pytest.raises(ValueError):
            store.put_idea_source(IdeaSourceRecord(org="acme"))

    def test_idea_sources_scoped_per_idea(self, store):
        store.put_idea_source(
            IdeaSourceRecord(ideaId="idea-A", sourceId="r#1", org="acme")
        )
        store.put_idea_source(
            IdeaSourceRecord(ideaId="idea-B", sourceId="r#2", org="acme")
        )
        a = store.list_idea_sources("acme", "idea-A")
        assert {s.sourceId for s in a} == {"r#1"}


# ===========================================================================
# 5. Golden cases — CRUD against mock DynamoDB
# ===========================================================================


class TestGoldenCaseMoto:
    def test_put_get_and_overwrite(self, store):
        gc1 = GoldenCaseRecord(
            caseId="idea-001", skillBaseName="my-skill", org="acme",
            before="b1", after="a1", ideaBody="i1", createdAt=111,
        )
        store.put_golden_case(gc1)
        got = store.get_golden_case("acme", "my-skill", "idea-001")
        assert got is not None and got.before == "b1" and got.createdAt == 111

        gc2 = GoldenCaseRecord(
            caseId="idea-001", skillBaseName="my-skill", org="acme",
            before="b2", after="a2", ideaBody="i2",
        )
        store.put_golden_case(gc2)
        assert store.get_golden_case("acme", "my-skill", "idea-001").before == "b2"

    def test_missing_golden_case_is_none(self, store):
        assert store.get_golden_case("acme", "my-skill", "x") is None

    def test_golden_case_org_guard(self, store):
        with pytest.raises(OrgGuardError):
            store.put_golden_case(GoldenCaseRecord(
                caseId="c", skillBaseName="s", org="", before="b", after="a", ideaBody="i"
            ))


# ===========================================================================
# 6. Anchor index — CRUD + locality FilterExpression (active + idea filter)
# ===========================================================================


class TestAnchorMoto:
    def _anchor(self, **kw):
        base = dict(
            ideaId="idea-001", ownerRepo="acme/backend",
            file="src/foo.ts", symbol="fooFn", org="acme", active=True,
        )
        base.update(kw)
        return AnchorRecord(**base)

    def test_anchor_written_and_locality_lookup(self, store):
        store.put_anchor(self._anchor())
        hits = store.get_ideas_by_anchor("acme", "acme/backend", "src/foo.ts", "fooFn")
        assert "idea-001" in hits

    def test_inactive_anchor_excluded_by_filter(self, store):
        store.put_anchor(self._anchor(active=False))
        hits = store.get_ideas_by_anchor("acme", "acme/backend", "src/foo.ts", "fooFn")
        assert "idea-001" not in hits  # FilterExpression Attr('active').eq(True)

    def test_get_anchors_for_idea_filters_by_idea(self, store):
        store.put_anchor(self._anchor(file="a.ts", symbol="aF"))
        store.put_anchor(self._anchor(file="b.ts", symbol="bF"))
        store.put_anchor(self._anchor(ideaId="idea-002", file="c.ts", symbol="cF"))
        anchors = store.get_anchors_for_idea("acme", "acme/backend", "idea-001")
        assert {a.file for a in anchors} == {"a.ts", "b.ts"}

    def test_anchor_org_scoped(self, store):
        store.put_anchor(self._anchor())
        store.put_anchor(self._anchor(ideaId="idea-evil", org="rival"))
        hits = store.get_ideas_by_anchor("acme", "acme/backend", "src/foo.ts", "fooFn")
        assert "idea-001" in hits and "idea-evil" not in hits

    def test_anchor_org_guard(self, store):
        with pytest.raises(OrgGuardError):
            store.put_anchor(self._anchor(org=""))


# ===========================================================================
# 7. Processed-PR cursor — idempotency against mock DynamoDB
# ===========================================================================


class TestProcessedPrMoto:
    def test_mark_and_check(self, store):
        rec = ProcessedPrRecord(
            org="acme", owner_repo="acme/backend", pr_number=42, processed_at=1000
        )
        assert not store.is_pr_processed("acme", "acme/backend", 42)
        store.mark_pr_processed(rec)
        assert store.is_pr_processed("acme", "acme/backend", 42)

    def test_distinct_prs_independent(self, store):
        store.mark_pr_processed(ProcessedPrRecord(
            org="acme", owner_repo="acme/backend", pr_number=10, processed_at=0
        ))
        assert store.is_pr_processed("acme", "acme/backend", 10)
        assert not store.is_pr_processed("acme", "acme/backend", 11)

    def test_cursor_org_scoped(self, store):
        store.mark_pr_processed(ProcessedPrRecord(
            org="acme", owner_repo="acme/backend", pr_number=42, processed_at=0
        ))
        assert not store.is_pr_processed("rival", "acme/backend", 42)

    def test_cursor_org_guard(self, store):
        with pytest.raises(OrgGuardError):
            store.mark_pr_processed(ProcessedPrRecord(
                org="", owner_repo="r/r", pr_number=1, processed_at=0
            ))


# ===========================================================================
# 8. Verify-event audit — append-only + seq-ordered query
# ===========================================================================


class TestVerifyEventMoto:
    def test_append_and_list_in_seq_order(self, store):
        # Insert out of order; the VERIFY# SK is zero-padded so the query
        # (ScanIndexForward) returns them ascending.
        for seq in (3, 1, 2):
            store.append_verify_event(VerifyEventRecord(
                org="acme", idea_id="idea-001", seq=seq,
                verdict="corroborate", pr_ref=f"acme/backend#{seq}",
                authority="merged", recorded_at=seq * 1000,
            ))
        events = store.list_verify_events("acme", "idea-001")
        assert [e.seq for e in events] == [1, 2, 3]
        assert events[0].pr_ref == "acme/backend#1"

    def test_optional_fields_round_trip(self, store):
        store.append_verify_event(VerifyEventRecord(
            org="acme", idea_id="idea-001", seq=1,
            verdict="supersede", pr_ref=None, authority=None, recorded_at=5,
        ))
        ev = store.list_verify_events("acme", "idea-001")[0]
        assert ev.verdict == "supersede"
        assert ev.pr_ref is None and ev.authority is None

    def test_empty_for_no_events(self, store):
        assert store.list_verify_events("acme", "idea-none") == []

    def test_verify_event_org_guard(self, store):
        with pytest.raises(OrgGuardError):
            store.append_verify_event(VerifyEventRecord(
                org="", idea_id="i", seq=1, verdict="x",
                pr_ref=None, authority=None, recorded_at=0,
            ))


# ===========================================================================
# 9. Key parity — production writes land at the codegen'd key
# ===========================================================================


def test_idea_lands_at_generated_key(store, dynamo_table):
    store.put_idea(_idea(idea_id="keytest"))
    k = idea_key("acme", "my-skill", "keytest")
    table = dynamo_table.Table(TABLE_NAME)
    resp = table.get_item(Key={"PK": k["PK"], "SK": k["SK"]})
    assert "Item" in resp
    assert resp["Item"]["SK"] == "IDEA#my-skill#keytest"


# ===========================================================================
# 10. S3 Vectors — production S3VectorStore against a mocked s3vectors client
# ===========================================================================


class _FakeS3VectorsClient:
    """Faithful in-process double for the boto3 ``s3vectors`` client.

    moto does not model s3vectors (a 2024+ service), so we mock the client the
    production ``S3VectorStore`` calls, honouring the exact request/response
    shapes from the boto3 s3vectors API:
      - put_vectors(vectorBucketName, indexName, vectors=[{key,data:{float32},metadata}])
      - delete_vectors(vectorBucketName, indexName, keys)
      - query_vectors(...) -> {vectors:[{key, distance, metadata}]}
      - get_vectors(...)   -> {vectors:[{key, metadata}]}
    """

    def __init__(self):
        # {(bucket, index): {key: {"data": [...], "metadata": {...}}}}
        self._store: dict[tuple[str, str], dict[str, dict]] = {}
        self.put_calls = 0

    def _idx(self, bucket, index):
        return self._store.setdefault((bucket, index), {})

    def put_vectors(self, *, vectorBucketName, indexName, vectors):  # noqa: N803
        self.put_calls += 1
        idx = self._idx(vectorBucketName, indexName)
        for v in vectors:
            assert "float32" in v["data"], "must send data.float32"
            idx[v["key"]] = {"data": v["data"]["float32"], "metadata": v.get("metadata", {})}

    def delete_vectors(self, *, vectorBucketName, indexName, keys):  # noqa: N803
        idx = self._idx(vectorBucketName, indexName)
        for k in keys:
            idx.pop(k, None)

    def query_vectors(self, *, vectorBucketName, indexName, topK,  # noqa: N803
                      queryVector, returnMetadata=False, returnDistance=False,
                      filter=None):
        idx = self._idx(vectorBucketName, indexName)
        q = queryVector["float32"]
        out = []
        for key, rec in idx.items():
            if filter:
                if any(rec["metadata"].get(fk) != fv for fk, fv in filter.items()):
                    continue
            sim = sum(a * b for a, b in zip(q, rec["data"]))
            out.append({"key": key, "distance": 1.0 - sim, "metadata": rec["metadata"]})
        out.sort(key=lambda r: r["distance"])
        return {"vectors": out[:topK]}

    def get_vectors(self, *, vectorBucketName, indexName, keys,  # noqa: N803
                    returnMetadata=False, returnData=False):
        idx = self._idx(vectorBucketName, indexName)
        return {"vectors": [
            {"key": k, "metadata": idx[k]["metadata"]} for k in keys if k in idx
        ]}


@pytest.fixture()
def vstore():
    return S3VectorStore("test-bucket", client=_FakeS3VectorsClient())


def _unit(dim, idx):
    v = [0.0] * dim
    v[idx] = 1.0
    return v


class TestS3VectorStoreMocked:
    def test_put_and_query_returns_hit(self, vstore):
        vstore.put_vectors("ideas", [
            VectorItem(key="acme#sk", vector=_unit(4, 0), metadata={"org": "acme"}),
        ])
        hits = vstore.query_top_k("ideas", _unit(4, 0), k=5, org_filter="acme")
        assert len(hits) == 1
        assert hits[0].key == "acme#sk"
        assert hits[0].score == pytest.approx(1.0)

    def test_org_filter_isolates(self, vstore):
        vstore.put_vectors("ideas", [
            VectorItem(key="acme#x", vector=_unit(4, 0), metadata={"org": "acme"}),
            VectorItem(key="rival#x", vector=_unit(4, 0), metadata={"org": "rival"}),
        ])
        hits = vstore.query_top_k("ideas", _unit(4, 0), k=10, org_filter="acme")
        keys = {h.key for h in hits}
        assert "acme#x" in keys and "rival#x" not in keys

    def test_floor_filters_low_scores(self, vstore):
        vstore.put_vectors("ideas", [
            VectorItem(key="aligned", vector=_unit(4, 0), metadata={"org": "a"}),
            VectorItem(key="ortho", vector=_unit(4, 1), metadata={"org": "a"}),
        ])
        hits = vstore.query_top_k("ideas", _unit(4, 0), k=10, org_filter="a", floor=0.5)
        keys = {h.key for h in hits}
        assert "aligned" in keys and "ortho" not in keys

    def test_upsert_overwrites(self, vstore):
        vstore.put_vectors("ideas", [VectorItem(key="k", vector=_unit(4, 0), metadata={"v": 1})])
        vstore.put_vectors("ideas", [VectorItem(key="k", vector=_unit(4, 1), metadata={"v": 2})])
        assert vstore.get_vectors("ideas", ["k"])["k"]["v"] == 2

    def test_delete_removes(self, vstore):
        vstore.put_vectors("ideas", [VectorItem(key="del", vector=_unit(4, 0), metadata={})])
        vstore.delete_vectors("ideas", ["del"])
        assert vstore.get_vectors("ideas", ["del"]) == {}

    def test_topk_cap(self, vstore):
        vstore.put_vectors("ideas", [
            VectorItem(key=f"v{i}", vector=_unit(10, i), metadata={"org": "a"})
            for i in range(8)
        ])
        hits = vstore.query_top_k("ideas", _unit(10, 0), k=3, org_filter="a")
        assert len(hits) <= 3

    def test_get_vectors_metadata(self, vstore):
        vstore.put_vectors("skills", [
            VectorItem(key="acme#sk", vector=_unit(4, 0), metadata={"org": "acme", "n": 5}),
        ])
        result = vstore.get_vectors("skills", ["acme#sk"])
        assert result["acme#sk"]["n"] == 5

    def test_empty_put_is_noop(self, vstore):
        vstore.put_vectors("ideas", [])  # must not call the client / raise
        assert vstore._client.put_calls == 0

    def test_chunking_for_large_batches(self):
        """A batch over PUT_BATCH_LIMIT must be split into multiple put calls."""
        from learning_service.db.store import PUT_BATCH_LIMIT
        client = _FakeS3VectorsClient()
        vs = S3VectorStore("test-bucket", client=client)
        items = [
            VectorItem(key=f"k{i}", vector=_unit(4, 0), metadata={"org": "a"})
            for i in range(PUT_BATCH_LIMIT + 5)
        ]
        vs.put_vectors("ideas", items)
        assert client.put_calls == 2  # one full batch + one remainder
