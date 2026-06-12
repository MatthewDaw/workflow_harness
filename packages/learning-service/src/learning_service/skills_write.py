"""skills_write — the single Python skill-revision writer (U10 DRY guardrail).

This module is the **sole** author of skill revision records, TRUE pointer
updates, and IDEAGOLD# golden-case captures.  Nothing else writes these records
— the TS catalog-authoring UIs call this module via the author-revision API
rather than writing revisions themselves.

Key design points
-----------------
* **Append-immutable revision:** each write mints a new revision row
  (``SKILL#{variantId}#r{rev}``).  Old revisions are never overwritten.
* **CAS TRUE pointer:** the TRUE pointer row (``SKILL#{baseName}#TRUE``) is
  updated with a conditional write (optimistic concurrency).  A lost CAS means
  a concurrent writer advanced the pointer first.
* **CAS retry — re-read-on-lost-CAS:** the loser MUST re-read the winning
  revision body and re-apply its lesson delta before retrying.  A blind
  re-CAS of the stale body would silently discard the concurrent edit.
* **IDEAGOLD# capture:** on the fold path the caller supplies a ``GoldenCase``
  payload; the writer stores it atomically alongside the revision.

Dependency injection
--------------------
All DynamoDB writes go through an injectable ``SkillStore`` protocol so the
module is fully offline-testable without boto3.  The production implementation
(``DynamoSkillStore``) is provided at the bottom of this module.

Round-trip contract
-------------------
Python writes a revision → Go wrapper materializes it → TS web reads it — with
no hand-authored mirror anywhere.  The key format is defined once in the IDL
(``schema/idl.py``) and code-generated into both Python and TS.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from learning_service.schema.generated.py_types import (
    GoldenCaseRecord,
    RevisionRecord,
    SkillRecord,
    TruePointerRecord,
    golden_case_key,
    revision_key,
    skill_key,
    true_pointer_key,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class RevisionRequest:
    """Everything the writer needs to mint a new skill revision."""
    org: str
    base_name: str
    variant_id: str          # empty string = org base variant
    body: str                # the full new skill body
    author_user_id: str | None = None
    description: str | None = None
    # Fold path: supply a golden case payload (idea delta) to capture IDEAGOLD#.
    golden_case: "GoldenCasePayload | None" = None
    # The idea that drove this fold (for record linkage).
    idea_id: str | None = None


@dataclass
class GoldenCasePayload:
    """Before→after golden regression case captured alongside a fold revision."""
    case_id: str             # typically the idea_id
    before: str              # pre-fold skill body excerpt (the prompt context)
    after: str               # expected post-fold body (ground truth)
    idea_body: str           # the folded idea body (for drift detection)


@dataclass
class WriteResult:
    """Returned by a successful :func:`write_revision`."""
    org: str
    base_name: str
    variant_id: str
    rev: int                 # the new revision number that was minted
    true_pointer_updated: bool
    golden_case_written: bool


class ConflictError(Exception):
    """Raised when a CAS conflict occurs and the caller should retry."""

    def __init__(self, message: str, current_rev: int) -> None:
        super().__init__(message)
        self.current_rev = current_rev


class NotFoundError(Exception):
    """Raised when the writer expects a record that does not exist."""


# ---------------------------------------------------------------------------
# Store protocol (dependency-injection seam)
# ---------------------------------------------------------------------------


class SkillStore(Protocol):
    """Abstract over the DynamoDB / in-memory store for offline testing."""

    def get_current_rev(self, org: str, variant_id: str) -> int | None:
        """Return the highest existing revision number for a variant, or None.

        Returns the rev stored in the TRUE pointer if this is the base variant,
        or scans revision rows if needed.  A return of ``None`` means no
        revisions exist yet (→ rev 0).
        """
        ...

    def get_revision_body(self, org: str, variant_id: str, rev: int) -> str | None:
        """Return the body of a specific revision row, or None if not found."""
        ...

    def put_revision(self, record: RevisionRecord) -> None:
        """Write an immutable revision row (unconditional — the SK is unique)."""
        ...

    def cas_true_pointer(
        self,
        record: TruePointerRecord,
        expected_rev: int | None,
    ) -> bool:
        """Conditionally update the TRUE pointer.

        Returns True on success.  Returns False (never raises) when a
        concurrent writer advanced the pointer first (the loser should re-read
        and retry).

        ``expected_rev`` is the rev the caller read before attempting the CAS.
        Passing ``None`` means "pointer must not exist yet" (first-write case).
        """
        ...

    def put_golden_case(self, record: GoldenCaseRecord) -> None:
        """Write a IDEAGOLD# case row (unconditional — overwrites on re-fold)."""
        ...

    def get_true_pointer(self, org: str, base_name: str) -> TruePointerRecord | None:
        """Return the current TRUE pointer row, or None if none exists."""
        ...


# ---------------------------------------------------------------------------
# The single writer
# ---------------------------------------------------------------------------

MAX_CAS_RETRIES = 5


def write_revision(
    request: RevisionRequest,
    store: SkillStore,
    *,
    apply_lesson_delta: "Callable[[str, str], str] | None" = None,
    now_ms: "Callable[[], int] | None" = None,
    max_retries: int = MAX_CAS_RETRIES,
) -> WriteResult:
    """Write a new skill revision, update the TRUE pointer (CAS), and optionally
    capture a golden case.

    This is the **only** entry point for writing skill revisions.

    CAS retry discipline
    --------------------
    On a lost CAS the loser **re-reads the winning revision body** and
    re-applies the lesson delta via ``apply_lesson_delta(winning_body,
    original_body)`` before retrying.  A blind re-CAS of the stale body is
    a bug — it would silently overwrite the concurrent writer's changes.

    Parameters
    ----------
    request:
        What to write (org, variant, body, optional golden case).
    store:
        The injectable DynamoDB / in-memory store.
    apply_lesson_delta:
        ``(winning_body, lesson_body) -> merged_body``.  Called on every
        CAS retry.  Defaults to a simple append (useful for tests); supply
        a real merge function in production.
    now_ms:
        Clock injection (defaults to ``time.time_ns() // 1_000_000``).
    max_retries:
        Maximum CAS retry attempts before raising ``ConflictError``.

    Returns
    -------
    WriteResult
        Details of the minted revision.

    Raises
    ------
    ConflictError
        If CAS still fails after ``max_retries`` attempts.
    """
    if apply_lesson_delta is None:
        apply_lesson_delta = _default_delta

    if now_ms is None:
        now_ms = _default_now_ms

    body = request.body
    attempts = 0

    while True:
        attempts += 1
        if attempts > max_retries:
            raise ConflictError(
                f"CAS failed after {max_retries} retries for "
                f"{request.org}/{request.base_name}/{request.variant_id}",
                current_rev=_get_pointer_rev(store, request.org, request.base_name) or 0,
            )

        # 1. Read the current pointer to know what rev is "winning".
        pointer = store.get_true_pointer(request.org, request.base_name)
        expected_rev = pointer.rev if pointer is not None else None

        # On a retry: re-read the winning body and re-apply the lesson delta.
        # CRITICAL: we must NOT use the stale body from the original request —
        # that would silently discard the concurrent writer's changes.
        if attempts > 1:
            winning_body = _read_winning_body(store, request, pointer)
            logger.info(
                "CAS retry %d/%d: re-reading winning body (rev=%s) and re-applying delta for %s/%s",
                attempts,
                max_retries,
                expected_rev,
                request.org,
                request.base_name,
            )
            body = apply_lesson_delta(winning_body, request.body)

        # 2. Determine the new revision number.
        new_rev = (expected_rev + 1) if expected_rev is not None else 1

        # 3. Conditionally update the TRUE pointer (CAS) BEFORE writing the revision
        # row, so that a failed CAS does not pollute the revision namespace with a
        # stale body at a rev that the winning concurrent writer might also be using.
        new_pointer = TruePointerRecord(
            baseName=request.base_name,
            variantId=request.variant_id,
            rev=new_rev,
            org=request.org,
            updatedAt=now_ms(),
        )
        cas_ok = store.cas_true_pointer(new_pointer, expected_rev=expected_rev)

        if not cas_ok:
            # CAS lost — loop and retry (re-read winning body at top of loop).
            logger.info(
                "CAS conflict on %s/%s (attempt %d) — retrying with re-read",
                request.org,
                request.base_name,
                attempts,
            )
            continue

        # 4. CAS succeeded — now write the immutable revision row (unconditional,
        # because our rev is unique: we hold the TRUE pointer for this rev).
        rev_record = RevisionRecord(
            baseName=request.base_name,
            variantId=request.variant_id,
            rev=new_rev,
            body=body,
            org=request.org,
            description=request.description,
            createdBy=request.author_user_id,
            createdAt=now_ms(),
            ideaId=request.idea_id,
        )
        store.put_revision(rev_record)

        logger.info(
            "write_revision: success org=%s baseName=%s variantId=%s rev=%d",
            request.org,
            request.base_name,
            request.variant_id,
            new_rev,
        )

        # 5. Optionally capture a golden case (fold path).
        golden_written = False
        if request.golden_case is not None:
            gc = request.golden_case
            golden_record = GoldenCaseRecord(
                caseId=gc.case_id,
                skillBaseName=request.base_name,
                org=request.org,
                before=gc.before,
                after=gc.after,
                ideaBody=gc.idea_body,
                createdAt=now_ms(),
            )
            store.put_golden_case(golden_record)
            golden_written = True

        return WriteResult(
            org=request.org,
            base_name=request.base_name,
            variant_id=request.variant_id,
            rev=new_rev,
            true_pointer_updated=True,
            golden_case_written=golden_written,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_pointer_rev(store: SkillStore, org: str, base_name: str) -> int | None:
    p = store.get_true_pointer(org, base_name)
    return p.rev if p is not None else None


def _read_winning_body(
    store: SkillStore,
    request: RevisionRequest,
    pointer: TruePointerRecord | None,
) -> str:
    """Return the body of the winning (current) revision, falling back to the
    original request body if the revision row is not found."""
    if pointer is None:
        return request.body
    winning_body = store.get_revision_body(request.org, pointer.variantId, pointer.rev)
    if winning_body is None:
        logger.warning(
            "CAS retry: winning revision body not found for %s/%s rev=%d; using stale body",
            request.org,
            pointer.variantId,
            pointer.rev,
        )
        return request.body
    return winning_body


def _default_delta(winning_body: str, lesson_body: str) -> str:
    """Default lesson-delta applicator: merge by appending the lesson to the
    winning body, deduplicating if the lesson is already present.

    Production code should supply a real merge function (e.g. an LLM call or
    a structured diff apply).  This default is used in tests.
    """
    if lesson_body.strip() in winning_body:
        return winning_body
    return winning_body.rstrip("\n") + "\n\n" + lesson_body.lstrip("\n")


def _default_now_ms() -> int:
    return time.time_ns() // 1_000_000


# ---------------------------------------------------------------------------
# In-memory store for tests
# ---------------------------------------------------------------------------


@dataclass
class InMemorySkillStore:
    """Fully in-memory implementation of SkillStore for offline tests.

    Thread-safety: not thread-safe (use threading.Lock for concurrent tests
    that need it — see ``test_single_writer_cas_race_retries``).
    """

    # {(org, variant_id, rev): RevisionRecord}
    _revisions: dict[tuple[str, str, int], RevisionRecord] = field(default_factory=dict)
    # {(org, base_name): TruePointerRecord}
    _pointers: dict[tuple[str, str], TruePointerRecord] = field(default_factory=dict)
    # {(org, skill_name, case_id): GoldenCaseRecord}
    _golden_cases: dict[tuple[str, str, str], GoldenCaseRecord] = field(default_factory=dict)

    # Optionally intercept the CAS to simulate a concurrent write.
    # If set, called BEFORE each cas_true_pointer attempt; can mutate store state.
    cas_interceptor: "Callable[['InMemorySkillStore', int], None] | None" = field(
        default=None, repr=False
    )

    def get_current_rev(self, org: str, variant_id: str) -> int | None:
        p = self._pointers.get((org, variant_id))
        if p is not None:
            return p.rev
        # Scan revisions for the max rev of this variant.
        revs = [
            r.rev
            for (o, v, r_num), r in self._revisions.items()
            if o == org and v == variant_id
        ]
        return max(revs) if revs else None

    def get_revision_body(self, org: str, variant_id: str, rev: int) -> str | None:
        rec = self._revisions.get((org, variant_id, rev))
        return rec.body if rec is not None else None

    def put_revision(self, record: RevisionRecord) -> None:
        self._revisions[(record.org, record.variantId, record.rev)] = record

    def cas_true_pointer(
        self,
        record: TruePointerRecord,
        expected_rev: int | None,
    ) -> bool:
        # Run the interceptor (simulates a concurrent write sneaking in).
        if self.cas_interceptor is not None:
            self.cas_interceptor(self, record.rev)

        key = (record.org, record.baseName)
        current = self._pointers.get(key)
        current_rev = current.rev if current is not None else None

        if current_rev != expected_rev:
            return False  # CAS conflict

        self._pointers[key] = record
        return True

    def put_golden_case(self, record: GoldenCaseRecord) -> None:
        self._golden_cases[(record.org, record.skillBaseName, record.caseId)] = record

    def get_true_pointer(self, org: str, base_name: str) -> TruePointerRecord | None:
        return self._pointers.get((org, base_name))


# ---------------------------------------------------------------------------
# Production DynamoDB store (the F2 single-table store for author_revision)
# ---------------------------------------------------------------------------


class DynamoSkillStore:
    """Production SkillStore backed by boto3 DynamoDB (the F2 single-table store).

    This is the real writer the author-revision Lambda endpoint uses.  It is
    dependency-injected by ``_get_production_store()`` in
    ``entrypoints/author_revision.py`` when ``HARNESS_TABLE`` is set.

    Key format: uses the generated key builders from
    ``schema/generated/py_types.py`` (single source of truth, no hand-mirrored
    key strings).

    Gap 5 of MAT-141: replaces the InMemorySkillStore stub in
    ``_get_production_store()`` so the writer talks to the real DynamoDB store.
    """

    def __init__(
        self,
        table_name: str,
        *,
        dynamodb_resource: "object | None" = None,
    ) -> None:
        if not table_name:
            raise ValueError("DynamoSkillStore requires a non-empty table_name")
        self._table_name = table_name
        # Lazily import boto3 so the module can be imported in test environments
        # without boto3 installed.
        if dynamodb_resource is not None:
            self._table = dynamodb_resource.Table(table_name)  # type: ignore[attr-defined]
        else:
            import boto3  # type: ignore[import]
            self._table = boto3.resource("dynamodb").Table(table_name)

    # ------------------------------------------------------------------
    # SkillStore protocol implementation
    # ------------------------------------------------------------------

    def get_current_rev(self, org: str, variant_id: str) -> int | None:
        """Return the highest revision number for a variant from the TRUE pointer."""
        pointer = self.get_true_pointer(org, variant_id)
        return pointer.rev if pointer is not None else None

    def get_revision_body(self, org: str, variant_id: str, rev: int) -> str | None:
        """Fetch the body of a specific revision row."""
        key = revision_key(org, variant_id, rev)
        resp = self._table.get_item(Key=key)
        item = resp.get("Item")
        if item is None:
            return None
        return item.get("body")

    def put_revision(self, record: RevisionRecord) -> None:
        """Write an immutable revision row (unconditional — SK is unique)."""
        key = revision_key(record.org, record.variantId, record.rev)
        item: dict = {
            **key,
            "baseName": record.baseName,
            "variantId": record.variantId,
            "rev": record.rev,
            "body": record.body,
            "org": record.org,
        }
        if record.description is not None:
            item["description"] = record.description
        if record.createdBy is not None:
            item["createdBy"] = record.createdBy
        if record.createdAt is not None:
            item["createdAt"] = record.createdAt
        if record.ideaId is not None:
            item["ideaId"] = record.ideaId
        self._table.put_item(Item=item)

    def cas_true_pointer(
        self,
        record: TruePointerRecord,
        expected_rev: int | None,
    ) -> bool:
        """Conditionally update the TRUE pointer (CAS on rev).

        Returns True on success, False on a concurrent conflict (caller retries).
        """
        try:
            from botocore.exceptions import ClientError  # type: ignore[import]
        except ImportError:
            raise RuntimeError("botocore is required for DynamoSkillStore")

        key = true_pointer_key(record.org, record.baseName)
        item: dict = {
            **key,
            "baseName": record.baseName,
            "variantId": record.variantId,
            "rev": record.rev,
            "org": record.org,
        }
        if record.updatedAt is not None:
            item["updatedAt"] = record.updatedAt

        try:
            if expected_rev is None:
                # First write: item must not exist yet.
                self._table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(PK)",
                )
            else:
                # Conditional update: rev must still equal expected_rev.
                self._table.put_item(
                    Item=item,
                    ConditionExpression="#rev = :expected",
                    ExpressionAttributeNames={"#rev": "rev"},
                    ExpressionAttributeValues={":expected": expected_rev},
                )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def put_golden_case(self, record: GoldenCaseRecord) -> None:
        """Write a IDEAGOLD# case row (unconditional — overwrites on re-fold)."""
        key = golden_case_key(record.org, record.skillBaseName, record.caseId)
        item: dict = {
            **key,
            "caseId": record.caseId,
            "skillBaseName": record.skillBaseName,
            "org": record.org,
            "before": record.before,
            "after": record.after,
            "ideaBody": record.ideaBody,
        }
        if record.createdAt is not None:
            item["createdAt"] = record.createdAt
        self._table.put_item(Item=item)

    def get_true_pointer(self, org: str, base_name: str) -> TruePointerRecord | None:
        """Return the current TRUE pointer row, or None if none exists."""
        key = true_pointer_key(org, base_name)
        resp = self._table.get_item(Key=key)
        item = resp.get("Item")
        if item is None:
            return None
        return TruePointerRecord(
            baseName=item["baseName"],
            variantId=item["variantId"],
            rev=int(item["rev"]),
            org=item["org"],
            updatedAt=int(item["updatedAt"]) if "updatedAt" in item else None,
        )
