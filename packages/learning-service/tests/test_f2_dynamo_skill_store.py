"""MAT-141 (U10) Gap 5 — the author-revision writer talks the REAL F2 store.

``entrypoints/author_revision._get_production_store()`` must return the real
DynamoDB-backed ``DynamoSkillStore`` (the F2 single-table store) when
``HARNESS_TABLE`` is set — NOT the ``InMemorySkillStore`` stub.  And
``DynamoSkillStore`` must actually speak DynamoDB semantics (conditional CAS
writes via ``ConditionExpression``, item put/get).

boto3 / moto are not installed in this offline gate, so we drive the store
through its dependency-injection seam (``dynamodb_resource=``) with a faithful
fake Table that implements the exact conditional-write semantics DynamoDB
provides (``attribute_not_exists(PK)`` and ``#rev = :expected``).  This is a
mock-backed test of the REAL store code path, not a Python-only simulation of a
different store.
"""

from __future__ import annotations

import os
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# A faithful fake DynamoDB Table + resource (the DI seam DynamoSkillStore uses).
# ---------------------------------------------------------------------------


class _ConditionalCheckFailed(Exception):
    """Mirrors botocore's ConditionalCheckFailedException shape."""

    def __init__(self) -> None:
        super().__init__("ConditionalCheckFailedException")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _FakeTable:
    """In-memory table honouring the exact ConditionExpressions the store emits."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None,
                 ExpressionAttributeValues=None):  # noqa: N803
        key = (Item["PK"], Item["SK"])
        cond = ConditionExpression or ""
        if "attribute_not_exists(PK)" in cond:
            if key in self.items:
                raise _ConditionalCheckFailed()
        elif cond:
            # `#rev = :expected` — the stored item's rev must equal expected.
            existing = self.items.get(key)
            names = ExpressionAttributeNames or {}
            values = ExpressionAttributeValues or {}
            attr = names.get("#rev", "rev")
            expected = values.get(":expected")
            if existing is None or existing.get(attr) != expected:
                raise _ConditionalCheckFailed()
        self.items[key] = dict(Item)
        return {}

    def get_item(self, Key):  # noqa: N803
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}


class _FakeResource:
    def __init__(self, table: _FakeTable) -> None:
        self._table = table

    def Table(self, _name):  # noqa: N802
        return self._table


@pytest.fixture()
def _fake_botocore(monkeypatch):
    """Provide a botocore.exceptions.ClientError the store can `except` on.

    DynamoSkillStore.cas_true_pointer imports botocore.exceptions.ClientError to
    classify the conditional failure.  boto3/botocore are absent here, so inject a
    minimal stub module whose ClientError is our _ConditionalCheckFailed type.
    """
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = _ConditionalCheckFailed  # type: ignore[attr-defined]
    botocore.exceptions = exceptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    yield


def test_get_production_store_returns_dynamo_store_not_stub(monkeypatch):
    """With HARNESS_TABLE set, _get_production_store returns the real DynamoSkillStore.

    boto3 is absent here, so stub the `boto3` module the real store imports — the
    point is to prove the production selector constructs DynamoSkillStore (the F2
    store), NOT the InMemorySkillStore stub.
    """
    from learning_service.entrypoints import author_revision
    from learning_service.skills_write import DynamoSkillStore, InMemorySkillStore

    boto3_stub = types.ModuleType("boto3")
    boto3_stub.resource = lambda _svc: _FakeResource(_FakeTable())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3_stub)

    monkeypatch.setenv("HARNESS_TABLE", "harness")
    store = author_revision._get_production_store()
    assert isinstance(store, DynamoSkillStore)
    assert not isinstance(store, InMemorySkillStore)


def test_get_production_store_falls_back_to_inmemory_only_without_table(monkeypatch):
    from learning_service.entrypoints import author_revision
    from learning_service.skills_write import InMemorySkillStore

    monkeypatch.delenv("HARNESS_TABLE", raising=False)
    store = author_revision._get_production_store()
    assert isinstance(store, InMemorySkillStore)


def test_dynamo_skill_store_round_trips_revision_and_pointer(_fake_botocore):
    """DynamoSkillStore writes/reads revision + TRUE pointer with DynamoDB semantics."""
    from learning_service.skills_write import (
        DynamoSkillStore,
        RevisionRequest,
        write_revision,
    )

    table = _FakeTable()
    store = DynamoSkillStore("harness", dynamodb_resource=_FakeResource(table))

    req = RevisionRequest(org="acme", base_name="reconcile", variant_id="", body="v1 body")
    result = write_revision(req, store)
    assert result.rev == 1

    # The revision row + TRUE pointer landed in the (fake) DynamoDB table under the
    # generated key format.
    assert store.get_revision_body("acme", "", 1) == "v1 body"
    ptr = store.get_true_pointer("acme", "reconcile")
    assert ptr is not None and ptr.rev == 1

    # A second write advances rev via the CAS pointer (real conditional path).
    r2 = write_revision(
        RevisionRequest(org="acme", base_name="reconcile", variant_id="", body="v2 body"),
        store,
    )
    assert r2.rev == 2
    assert store.get_true_pointer("acme", "reconcile").rev == 2


def test_dynamo_skill_store_cas_conflict_is_detected(_fake_botocore):
    """A stale expected_rev makes the conditional pointer write fail (CAS lost)."""
    from learning_service.skills_write import DynamoSkillStore
    from learning_service.schema.generated.py_types import TruePointerRecord

    table = _FakeTable()
    store = DynamoSkillStore("harness", dynamodb_resource=_FakeResource(table))

    # First pointer write (expected_rev None → attribute_not_exists) succeeds.
    rec1 = TruePointerRecord(baseName="s", variantId="", rev=1, org="acme")
    assert store.cas_true_pointer(rec1, expected_rev=None) is True

    # A write expecting rev=None again must fail (item now exists).
    assert store.cas_true_pointer(rec1, expected_rev=None) is False

    # A write expecting the wrong current rev fails; the right one succeeds.
    rec2 = TruePointerRecord(baseName="s", variantId="", rev=2, org="acme")
    assert store.cas_true_pointer(rec2, expected_rev=5) is False
    assert store.cas_true_pointer(rec2, expected_rev=1) is True


def test_dynamo_skill_store_writes_golden_case(_fake_botocore):
    from learning_service.skills_write import (
        DynamoSkillStore,
        GoldenCasePayload,
        RevisionRequest,
        write_revision,
    )
    from learning_service.schema.generated.py_types import golden_case_key

    table = _FakeTable()
    store = DynamoSkillStore("harness", dynamodb_resource=_FakeResource(table))

    req = RevisionRequest(
        org="acme",
        base_name="reconcile",
        variant_id="",
        body="folded body",
        idea_id="i-1",
        golden_case=GoldenCasePayload(
            case_id="i-1", before="before", after="folded body", idea_body="lesson"
        ),
    )
    result = write_revision(req, store)
    assert result.golden_case_written is True

    # The golden case landed under the generated IDEAGOLD# key.
    gk = golden_case_key("acme", "reconcile", "i-1")
    assert (gk["PK"], gk["SK"]) in table.items
    assert table.items[(gk["PK"], gk["SK"])]["before"] == "before"
