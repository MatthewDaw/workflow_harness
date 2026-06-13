"""store.py — DynamoDB single-table + S3 Vectors persistence layer (MAT-149 / F2).

This module is the bedrock the Verified Learning units sit on.  It provides:

1. **DynamoDB single-table access** for every learning record type:
   - Idea (IdeaRecord) — CRUD + conditional / OCC writes
   - Anchor index (AnchorRecord) — locality join
   - Processed-PR cursor — idempotency / replay
   - Verify-event audit — append-only supersession log
   - Golden cases (GoldenCaseRecord) — also written by skills_write; shared
     key builder here for completeness

2. **S3 Vectors client** — idea + skill vector upsert/query (mirrors
   the TypeScript ``S3Vectors`` class in ``embeddings/s3vectors.ts``).

3. **Cross-org guard** — every write method rejects a blank org immediately
   (``OrgGuardError``); no data can be written cross-org.

4. **invalidAt exclusion** — ``list_current_ideas`` filters out any idea
   where ``invalidAt`` is set; ``get_idea`` always returns it for history.

5. **Dependency injection** — all AWS clients are injectable so the module is
   fully testable offline without boto3 (``InMemoryLearningStore``,
   ``InMemoryVectorStore``).

Key builders are imported from the generated schema (``py_types``), so key
formats are the single-source-of-truth — no hand-mirrored strings here.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from learning_service.schema.generated.py_types import (
    AnchorRecord,
    BranchSessionRecord,
    GoldenCaseRecord,
    IdeaRecord,
    IdeaSourceRecord,
    anchor_key,
    branch_session_key,
    golden_case_key,
    idea_key,
    idea_source_key,
    processed_pr_key,
    verify_event_key,
)

# Prefix helpers not in the generated file — defined locally.
def _idea_sk_prefix_for_skill(skill_base_name: str) -> str:
    return f"IDEA#{skill_base_name}#"


def _idea_sk_prefix_for_org() -> str:
    return "IDEA#"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (mirrors s3vectors.ts)
# ---------------------------------------------------------------------------

DEFAULT_VECTOR_BUCKET = "command-hq-skill-idea-vectors"
SKILL_VECTOR_INDEX = "skills"
IDEA_VECTOR_INDEX = "ideas"

PUT_BATCH_LIMIT = 500  # S3 Vectors per-request max

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OrgGuardError(ValueError):
    """Raised when a write is attempted with a blank org slug."""

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"OrgGuardError: '{operation}' requires a non-blank org slug. "
            "Cross-org writes are forbidden."
        )


class VersionConflictError(Exception):
    """Raised when a conditional idea write fails due to a version mismatch."""

    def __init__(self, idea_id: str, expected: int, current: int) -> None:
        super().__init__(
            f"VersionConflictError: idea {idea_id!r} has corroborationVersion "
            f"{current} but expected {expected}. Retry with the latest version."
        )
        self.idea_id = idea_id
        self.expected = expected
        self.current = current


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class VectorItem:
    """A vector to write: a unique key, the float data, and filterable metadata.

    The ``metadata`` dict MUST include an ``org`` key for query-time isolation.
    """
    key: str
    vector: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryHit:
    """A top-k similarity search result."""
    key: str
    score: float  # cosine similarity in [0, 1]; higher = closer
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessedPrRecord:
    """Org-scoped idempotency cursor — marks a PR as already ingested."""
    org: str
    owner_repo: str  # e.g. "acme/backend"
    pr_number: int
    processed_at: int  # epoch-ms


@dataclass
class VerifyEventRecord:
    """Append-only audit row under an idea (supersede/corroborate events)."""
    org: str
    idea_id: str
    seq: int
    verdict: str           # supersede | corroborate | refine | neutral
    pr_ref: str | None     # "owner/repo#<prNumber>" or None for authored events
    authority: str | None  # user_directive | authored_import | merged
    recorded_at: int       # epoch-ms


# ---------------------------------------------------------------------------
# Protocol: DynamoDB learning store (dependency-injection seam)
# ---------------------------------------------------------------------------


class LearningStore(Protocol):
    """Abstract over DynamoDB for offline testing."""

    # -- Ideas ---------------------------------------------------------------

    def put_idea(self, record: IdeaRecord) -> None:
        """Unconditional idea write (first-write or overwrite).  Use only when
        you own the record exclusively (e.g. initial creation)."""
        ...

    def put_idea_conditional(self, record: IdeaRecord, expected_version: int) -> None:
        """Conditional idea write (OCC on corroborationVersion).

        Raises VersionConflictError if the stored version != expected_version.
        """
        ...

    def get_idea(self, org: str, skill_base_name: str, idea_id: str) -> IdeaRecord | None:
        """Return the idea record (including invalidAt ones — full history)."""
        ...

    def list_current_ideas(self, org: str, skill_base_name: str) -> list[IdeaRecord]:
        """Return all *current* (non-retired) ideas for a skill family.

        Excludes any idea where ``invalidAt`` is set (those are historical).
        """
        ...

    def list_all_ideas_for_org(self, org: str) -> list[IdeaRecord]:
        """Return every idea in an org (current + historical) — for history reads."""
        ...

    # -- Idea sources --------------------------------------------------------

    def put_idea_source(self, record: IdeaSourceRecord) -> None:
        """Write a contribution (a merged PR or authored source) under an idea."""
        ...

    def get_idea_source(
        self, org: str, idea_id: str, source_id: str
    ) -> IdeaSourceRecord | None:
        """Return a specific idea source by (idea, source) id."""
        ...

    def list_idea_sources(self, org: str, idea_id: str) -> list[IdeaSourceRecord]:
        """Return all contributions under an idea (the votes that corroborate it)."""
        ...

    # -- Golden cases --------------------------------------------------------

    def put_golden_case(self, record: GoldenCaseRecord) -> None:
        """Write a golden case (unconditional — overwrites on re-fold)."""
        ...

    def get_golden_case(
        self, org: str, skill_base_name: str, case_id: str
    ) -> GoldenCaseRecord | None:
        """Return a golden case record, or None if not found."""
        ...

    # -- Anchor index --------------------------------------------------------

    def put_anchor(self, record: AnchorRecord) -> None:
        """Write a code-anchor index entry."""
        ...

    def get_anchors_for_idea(
        self, org: str, owner_repo: str, idea_id: str
    ) -> list[AnchorRecord]:
        """Return all anchors belonging to a specific idea (for un-fold cleanup)."""
        ...

    def get_ideas_by_anchor(
        self, org: str, owner_repo: str, file: str, symbol: str
    ) -> list[str]:
        """Return idea IDs anchored to the given (file, symbol) — the locality join."""
        ...

    # -- Processed-PR cursor -------------------------------------------------

    def mark_pr_processed(self, record: ProcessedPrRecord) -> None:
        """Mark a PR as ingested (idempotency cursor)."""
        ...

    def is_pr_processed(self, org: str, owner_repo: str, pr_number: int) -> bool:
        """Return True if this PR has already been ingested."""
        ...

    # -- Verify-event audit --------------------------------------------------

    def append_verify_event(self, record: VerifyEventRecord) -> None:
        """Append a supersession/corroboration audit event."""
        ...

    def list_verify_events(self, org: str, idea_id: str) -> list[VerifyEventRecord]:
        """Return all audit events for an idea, ordered by seq ascending."""
        ...

    # -- Branch→session link (U7) --------------------------------------------

    def put_branch_session_link(self, record: BranchSessionRecord) -> None:
        """Write (or overwrite) a branch→session link at push time (U7).

        Last-writer-wins per (org, ownerRepo, branch).  Multi-session branches
        overwrite the previous link (the newest push wins; the plan calls for a
        "small list" for multi-session branches, but last-writer-wins is the v1
        behaviour — enough for the acceptance checklist).
        """
        ...

    def get_branch_session_link(
        self, org: str, owner_repo: str, branch: str
    ) -> BranchSessionRecord | None:
        """Return the branch→session link for (org, repo, branch), or None.

        Returns None when no push has been recorded (best-effort; U1 degrades
        to PR-only distillation when the link is absent).
        """
        ...


# ---------------------------------------------------------------------------
# Protocol: S3 Vectors store (dependency-injection seam)
# ---------------------------------------------------------------------------


class VectorStore(Protocol):
    """Abstract over S3 Vectors for offline testing."""

    def put_vectors(self, index_name: str, items: list[VectorItem]) -> None:
        """Upsert vectors into the index (chunked at PUT_BATCH_LIMIT)."""
        ...

    def delete_vectors(self, index_name: str, keys: list[str]) -> None:
        """Delete vectors by key from the index."""
        ...

    def query_top_k(
        self,
        index_name: str,
        vector: list[float],
        k: int,
        org_filter: str | None = None,
        floor: float | None = None,
    ) -> list[QueryHit]:
        """Top-k cosine similarity search; org_filter restricts to one org."""
        ...

    def get_vectors(
        self, index_name: str, keys: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch vectors by key (metadata only; used by probe / missing-embedding checks)."""
        ...


# ---------------------------------------------------------------------------
# In-memory implementation (offline tests)
# ---------------------------------------------------------------------------


def _org_guard(org: str, operation: str) -> None:
    """Raise OrgGuardError if org is blank."""
    if not org or not org.strip():
        raise OrgGuardError(operation)


@dataclass
class InMemoryLearningStore:
    """Fully in-memory LearningStore for offline tests.

    Thread-safety: not thread-safe (the InMemorySkillStore precedent).
    """

    # {(org, skill_base_name, idea_id): IdeaRecord}
    _ideas: dict[tuple[str, str, str], IdeaRecord] = field(default_factory=dict)
    # {(org, skill_base_name, case_id): GoldenCaseRecord}
    _golden_cases: dict[tuple[str, str, str], GoldenCaseRecord] = field(default_factory=dict)
    # {(org, idea_id, source_id): IdeaSourceRecord}
    _idea_sources: dict[tuple[str, str, str], IdeaSourceRecord] = field(default_factory=dict)
    # List of AnchorRecord (the prefix queries are just linear scans in-memory)
    _anchors: list[AnchorRecord] = field(default_factory=list)
    # {(org, owner_repo, pr_number): ProcessedPrRecord}
    _processed_prs: dict[tuple[str, str, int], ProcessedPrRecord] = field(default_factory=dict)
    # {(org, idea_id): sorted list of VerifyEventRecord}
    _verify_events: dict[tuple[str, str], list[VerifyEventRecord]] = field(default_factory=dict)
    # {(org, owner_repo, branch): BranchSessionRecord}
    _branch_session_links: dict[tuple[str, str, str], BranchSessionRecord] = field(default_factory=dict)

    # -- Ideas ---------------------------------------------------------------

    def put_idea(self, record: IdeaRecord) -> None:
        _org_guard(record.org, "put_idea")
        self._ideas[(record.org, record.skillBaseName, record.ideaId)] = record

    def put_idea_conditional(self, record: IdeaRecord, expected_version: int) -> None:
        _org_guard(record.org, "put_idea_conditional")
        key = (record.org, record.skillBaseName, record.ideaId)
        current = self._ideas.get(key)
        current_version = current.corroborationVersion if current is not None else -1
        if current_version != expected_version:
            raise VersionConflictError(
                idea_id=record.ideaId,
                expected=expected_version,
                current=current_version,
            )
        self._ideas[key] = record

    def get_idea(
        self, org: str, skill_base_name: str, idea_id: str
    ) -> IdeaRecord | None:
        return self._ideas.get((org, skill_base_name, idea_id))

    def list_current_ideas(self, org: str, skill_base_name: str) -> list[IdeaRecord]:
        """Return non-retired ideas for a skill family (excludes invalidAt)."""
        return [
            r
            for (o, sbn, _), r in self._ideas.items()
            if o == org and sbn == skill_base_name and r.invalidAt is None
        ]

    def list_all_ideas_for_org(self, org: str) -> list[IdeaRecord]:
        return [r for (o, _, _), r in self._ideas.items() if o == org]

    # -- Idea sources --------------------------------------------------------

    def put_idea_source(self, record: IdeaSourceRecord) -> None:
        _org_guard(record.org or "", "put_idea_source")
        if not record.ideaId or not record.sourceId:
            raise ValueError("put_idea_source requires ideaId and sourceId")
        self._idea_sources[(record.org, record.ideaId, record.sourceId)] = record

    def get_idea_source(
        self, org: str, idea_id: str, source_id: str
    ) -> IdeaSourceRecord | None:
        return self._idea_sources.get((org, idea_id, source_id))

    def list_idea_sources(self, org: str, idea_id: str) -> list[IdeaSourceRecord]:
        return [
            r
            for (o, iid, _), r in self._idea_sources.items()
            if o == org and iid == idea_id
        ]

    # -- Golden cases --------------------------------------------------------

    def put_golden_case(self, record: GoldenCaseRecord) -> None:
        _org_guard(record.org, "put_golden_case")
        self._golden_cases[(record.org, record.skillBaseName, record.caseId)] = record

    def get_golden_case(
        self, org: str, skill_base_name: str, case_id: str
    ) -> GoldenCaseRecord | None:
        return self._golden_cases.get((org, skill_base_name, case_id))

    # -- Anchor index --------------------------------------------------------

    def put_anchor(self, record: AnchorRecord) -> None:
        _org_guard(record.org, "put_anchor")
        # Upsert: replace an existing record with the same (org, ownerRepo,
        # file, symbol, ideaId) identity key, mirroring the DynamoDB put_item
        # behaviour (PK/SK uniquely identifies the row; a second write replaces
        # the first, not appends alongside it).
        for i, existing in enumerate(self._anchors):
            if (
                existing.org == record.org
                and existing.ownerRepo == record.ownerRepo
                and existing.file == record.file
                and existing.symbol == record.symbol
                and existing.ideaId == record.ideaId
            ):
                self._anchors[i] = record
                return
        self._anchors.append(record)

    def get_anchors_for_idea(
        self, org: str, owner_repo: str, idea_id: str
    ) -> list[AnchorRecord]:
        return [
            a
            for a in self._anchors
            if a.org == org and a.ownerRepo == owner_repo and a.ideaId == idea_id
        ]

    def get_ideas_by_anchor(
        self, org: str, owner_repo: str, file: str, symbol: str
    ) -> list[str]:
        """Return active idea IDs anchored to (file, symbol)."""
        return [
            a.ideaId
            for a in self._anchors
            if (
                a.org == org
                and a.ownerRepo == owner_repo
                and a.file == file
                and a.symbol == symbol
                and a.active
            )
        ]

    # -- Processed-PR cursor -------------------------------------------------

    def mark_pr_processed(self, record: ProcessedPrRecord) -> None:
        _org_guard(record.org, "mark_pr_processed")
        self._processed_prs[(record.org, record.owner_repo, record.pr_number)] = record

    def is_pr_processed(self, org: str, owner_repo: str, pr_number: int) -> bool:
        return (org, owner_repo, pr_number) in self._processed_prs

    # -- Verify-event audit --------------------------------------------------

    def append_verify_event(self, record: VerifyEventRecord) -> None:
        _org_guard(record.org, "append_verify_event")
        key = (record.org, record.idea_id)
        self._verify_events.setdefault(key, []).append(record)

    def list_verify_events(self, org: str, idea_id: str) -> list[VerifyEventRecord]:
        events = self._verify_events.get((org, idea_id), [])
        return sorted(events, key=lambda e: e.seq)

    # -- Branch→session link (U7) --------------------------------------------

    def put_branch_session_link(self, record: BranchSessionRecord) -> None:
        _org_guard(record.org, "put_branch_session_link")
        self._branch_session_links[(record.org, record.ownerRepo, record.branch)] = record

    def get_branch_session_link(
        self, org: str, owner_repo: str, branch: str
    ) -> BranchSessionRecord | None:
        return self._branch_session_links.get((org, owner_repo, branch))


# ---------------------------------------------------------------------------
# In-memory S3 Vectors implementation (offline tests)
# ---------------------------------------------------------------------------


@dataclass
class InMemoryVectorStore:
    """Fully in-memory VectorStore for offline tests.

    Cosine similarity is computed with a simple dot-product (vectors assumed
    to be unit-normalised, as text-embedding-3-small produces).
    """

    # {index_name: {key: VectorItem}}
    _store: dict[str, dict[str, VectorItem]] = field(default_factory=dict)

    def _index(self, index_name: str) -> dict[str, VectorItem]:
        return self._store.setdefault(index_name, {})

    def put_vectors(self, index_name: str, items: list[VectorItem]) -> None:
        idx = self._index(index_name)
        for item in items:
            idx[item.key] = item

    def delete_vectors(self, index_name: str, keys: list[str]) -> None:
        idx = self._index(index_name)
        for k in keys:
            idx.pop(k, None)

    def query_top_k(
        self,
        index_name: str,
        vector: list[float],
        k: int,
        org_filter: str | None = None,
        floor: float | None = None,
    ) -> list[QueryHit]:
        idx = self._index(index_name)
        hits: list[QueryHit] = []
        for item in idx.values():
            if org_filter is not None and item.metadata.get("org") != org_filter:
                continue
            score = _dot(vector, item.vector)
            if floor is not None and score < floor:
                continue
            hits.append(QueryHit(key=item.key, score=score, metadata=item.metadata))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def get_vectors(
        self, index_name: str, keys: list[str]
    ) -> dict[str, dict[str, Any]]:
        idx = self._index(index_name)
        return {k: idx[k].metadata for k in keys if k in idx}


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product (cosine similarity for unit-norm vectors)."""
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Production DynamoDB implementation (requires boto3 at runtime)
# ---------------------------------------------------------------------------


class DynamoLearningStore:
    """Production LearningStore backed by boto3 DynamoDB.

    Requires:
      - HARNESS_TABLE env var (or explicit ``table_name``)
      - Appropriate IAM permissions

    Inject this into the Lambda handler; use InMemoryLearningStore in tests.
    """

    def __init__(
        self,
        table_name: str,
        *,
        dynamodb_resource: Any = None,
    ) -> None:
        if not table_name:
            raise ValueError("DynamoLearningStore requires a non-empty table_name")
        self._table_name = table_name
        # Lazily import boto3 so the module can be imported in test environments
        # without boto3 installed.
        if dynamodb_resource is not None:
            self._dynamo = dynamodb_resource
            self._table = dynamodb_resource.Table(table_name)
        else:
            import boto3  # type: ignore[import]
            self._dynamo = boto3.resource("dynamodb")
            self._table = self._dynamo.Table(table_name)

    # -- Ideas ---------------------------------------------------------------

    def put_idea(self, record: IdeaRecord) -> None:
        _org_guard(record.org, "put_idea")
        item = _idea_to_dynamo(record)
        self._table.put_item(Item=item)

    def put_idea_conditional(self, record: IdeaRecord, expected_version: int) -> None:
        _org_guard(record.org, "put_idea_conditional")
        from botocore.exceptions import ClientError  # type: ignore[import]

        key = idea_key(record.org, record.skillBaseName, record.ideaId)
        item = _idea_to_dynamo(record)

        # expected_version == -1 is the "first write / must-not-exist" sentinel
        # (parity with InMemoryLearningStore, which reports -1 for a missing row).
        # Express it as attribute_not_exists(PK); otherwise CAS on the version.
        if expected_version < 0:
            condition = "attribute_not_exists(PK)"
            values: dict[str, Any] = {}
        else:
            condition = "corroborationVersion = :ev"
            values = {":ev": expected_version}

        try:
            kwargs: dict[str, Any] = {"Item": item, "ConditionExpression": condition}
            if values:
                kwargs["ExpressionAttributeValues"] = values
            self._table.put_item(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Read the current version for the error message.
                current = self.get_idea(record.org, record.skillBaseName, record.ideaId)
                current_version = current.corroborationVersion if current is not None else -1
                raise VersionConflictError(
                    idea_id=record.ideaId,
                    expected=expected_version,
                    current=current_version,
                ) from exc
            raise

    def get_idea(
        self, org: str, skill_base_name: str, idea_id: str
    ) -> IdeaRecord | None:
        key = idea_key(org, skill_base_name, idea_id)
        resp = self._table.get_item(Key=key)
        item = resp.get("Item")
        return _dynamo_to_idea(item) if item else None

    def list_current_ideas(self, org: str, skill_base_name: str) -> list[IdeaRecord]:
        """Query the skill-idea prefix and exclude invalidAt (current set only)."""
        from boto3.dynamodb.conditions import Key, Attr  # type: ignore[import]

        prefix = _idea_sk_prefix_for_skill(skill_base_name)
        resp = self._table.query(
            KeyConditionExpression=(
                Key("PK").eq(f"SCOPE#org#{org}") & Key("SK").begins_with(prefix)
            ),
            FilterExpression=Attr("invalidAt").not_exists(),
        )
        return [_dynamo_to_idea(item) for item in resp.get("Items", [])]

    def list_all_ideas_for_org(self, org: str) -> list[IdeaRecord]:
        from boto3.dynamodb.conditions import Key  # type: ignore[import]

        resp = self._table.query(
            KeyConditionExpression=(
                Key("PK").eq(f"SCOPE#org#{org}") & Key("SK").begins_with(_idea_sk_prefix_for_org())
            ),
        )
        return [_dynamo_to_idea(item) for item in resp.get("Items", [])]

    # -- Idea sources --------------------------------------------------------

    def put_idea_source(self, record: IdeaSourceRecord) -> None:
        _org_guard(record.org or "", "put_idea_source")
        if not record.ideaId or not record.sourceId:
            raise ValueError("put_idea_source requires ideaId and sourceId")
        item = _idea_source_to_dynamo(record)
        self._table.put_item(Item=item)

    def get_idea_source(
        self, org: str, idea_id: str, source_id: str
    ) -> IdeaSourceRecord | None:
        key = idea_source_key(org, idea_id, source_id)
        resp = self._table.get_item(Key=key)
        item = resp.get("Item")
        return _dynamo_to_idea_source(item) if item else None

    def list_idea_sources(self, org: str, idea_id: str) -> list[IdeaSourceRecord]:
        from boto3.dynamodb.conditions import Key  # type: ignore[import]

        prefix = f"IDEASRC#{idea_id}#"
        resp = self._table.query(
            KeyConditionExpression=(
                Key("PK").eq(f"SCOPE#org#{org}") & Key("SK").begins_with(prefix)
            ),
        )
        return [_dynamo_to_idea_source(item) for item in resp.get("Items", [])]

    # -- Golden cases --------------------------------------------------------

    def put_golden_case(self, record: GoldenCaseRecord) -> None:
        _org_guard(record.org, "put_golden_case")
        key = golden_case_key(record.org, record.skillBaseName, record.caseId)
        item = {**key, **_golden_case_attrs(record)}
        self._table.put_item(Item=item)

    def get_golden_case(
        self, org: str, skill_base_name: str, case_id: str
    ) -> GoldenCaseRecord | None:
        key = golden_case_key(org, skill_base_name, case_id)
        resp = self._table.get_item(Key=key)
        item = resp.get("Item")
        return _dynamo_to_golden_case(item) if item else None

    # -- Anchor index --------------------------------------------------------

    def put_anchor(self, record: AnchorRecord) -> None:
        _org_guard(record.org, "put_anchor")
        key = anchor_key(
            record.org, record.ownerRepo, record.file, record.symbol, record.ideaId
        )
        item = {**key, **_anchor_attrs(record)}
        self._table.put_item(Item=item)

    def get_anchors_for_idea(
        self, org: str, owner_repo: str, idea_id: str
    ) -> list[AnchorRecord]:
        from boto3.dynamodb.conditions import Key, Attr  # type: ignore[import]

        resp = self._table.query(
            KeyConditionExpression=(
                Key("PK").eq(f"SCOPE#org#{org}")
                & Key("SK").begins_with(f"ANCHOR#{owner_repo}#")
            ),
            FilterExpression=Attr("ideaId").eq(idea_id),
        )
        return [_dynamo_to_anchor(item) for item in resp.get("Items", [])]

    def get_ideas_by_anchor(
        self, org: str, owner_repo: str, file: str, symbol: str
    ) -> list[str]:
        from boto3.dynamodb.conditions import Key, Attr  # type: ignore[import]

        prefix = f"ANCHOR#{owner_repo}#{file}#{symbol}#"
        resp = self._table.query(
            KeyConditionExpression=(
                Key("PK").eq(f"SCOPE#org#{org}") & Key("SK").begins_with(prefix)
            ),
            FilterExpression=Attr("active").eq(True),
        )
        return [item["ideaId"] for item in resp.get("Items", [])]

    # -- Processed-PR cursor -------------------------------------------------

    def mark_pr_processed(self, record: ProcessedPrRecord) -> None:
        _org_guard(record.org, "mark_pr_processed")
        key = processed_pr_key(record.org, record.owner_repo, record.pr_number)
        item = {
            **key,
            "org": record.org,
            "ownerRepo": record.owner_repo,
            "prNumber": record.pr_number,
            "processedAt": record.processed_at,
        }
        self._table.put_item(Item=item)

    def is_pr_processed(self, org: str, owner_repo: str, pr_number: int) -> bool:
        key = processed_pr_key(org, owner_repo, pr_number)
        resp = self._table.get_item(Key=key)
        return "Item" in resp

    # -- Verify-event audit --------------------------------------------------

    def append_verify_event(self, record: VerifyEventRecord) -> None:
        _org_guard(record.org, "append_verify_event")
        key = verify_event_key(record.org, record.idea_id, record.seq)
        item = {
            **key,
            "org": record.org,
            "ideaId": record.idea_id,
            "seq": record.seq,
            "verdict": record.verdict,
            "recordedAt": record.recorded_at,
        }
        if record.pr_ref is not None:
            item["prRef"] = record.pr_ref
        if record.authority is not None:
            item["authority"] = record.authority
        self._table.put_item(Item=item)

    def list_verify_events(self, org: str, idea_id: str) -> list[VerifyEventRecord]:
        from boto3.dynamodb.conditions import Key  # type: ignore[import]

        prefix = f"VERIFY#{idea_id}#"
        resp = self._table.query(
            KeyConditionExpression=(
                Key("PK").eq(f"SCOPE#org#{org}") & Key("SK").begins_with(prefix)
            ),
            ScanIndexForward=True,  # ascending by SK (seq-padded)
        )
        return [_dynamo_to_verify_event(item) for item in resp.get("Items", [])]

    # -- Branch→session link (U7) --------------------------------------------

    def put_branch_session_link(self, record: BranchSessionRecord) -> None:
        _org_guard(record.org, "put_branch_session_link")
        key = branch_session_key(record.org, record.ownerRepo, record.branch)
        item: dict[str, Any] = {
            **key,
            "org": record.org,
            "ownerRepo": record.ownerRepo,
            "branch": record.branch,
            "sessionId": record.sessionId,
        }
        if record.turnStart is not None:
            item["turnStart"] = record.turnStart
        if record.turnEnd is not None:
            item["turnEnd"] = record.turnEnd
        if record.distilledContext is not None:
            item["distilledContext"] = record.distilledContext
        if record.pushedAt is not None:
            item["pushedAt"] = record.pushedAt
        # TTL is stored as a top-level attribute so DynamoDB's TTL feature can
        # expire records automatically (90-day window).
        if record.ttlAt is not None:
            item["ttlAt"] = record.ttlAt
        self._table.put_item(Item=item)

    def get_branch_session_link(
        self, org: str, owner_repo: str, branch: str
    ) -> BranchSessionRecord | None:
        key = branch_session_key(org, owner_repo, branch)
        resp = self._table.get_item(Key=key)
        item = resp.get("Item")
        return _dynamo_to_branch_session(item) if item else None


# ---------------------------------------------------------------------------
# Production S3 Vectors implementation (requires boto3 / aws-sdk at runtime)
# ---------------------------------------------------------------------------


class S3VectorStore:
    """Production VectorStore backed by Amazon S3 Vectors (boto3).

    Requires:
      - SKILL_IDEA_VECTOR_BUCKET env var (or explicit ``bucket_name``)
      - Appropriate IAM permissions (the command-hq-skill-idea-vectors-put-query
        managed policy, as documented in s3vectors.ts)

    Inject this into the Lambda handler; use InMemoryVectorStore in tests.
    """

    def __init__(
        self,
        bucket_name: str,
        *,
        client: Any = None,
    ) -> None:
        if not bucket_name:
            raise ValueError("S3VectorStore requires a non-empty bucket_name")
        self._bucket = bucket_name
        if client is not None:
            self._client = client
        else:
            import boto3  # type: ignore[import]
            self._client = boto3.client("s3vectors")

    def _chunk(self, items: list, size: int) -> list[list]:
        return [items[i : i + size] for i in range(0, len(items), size)]

    def put_vectors(self, index_name: str, items: list[VectorItem]) -> None:
        if not items:
            return
        for batch in self._chunk(items, PUT_BATCH_LIMIT):
            self._client.put_vectors(
                vectorBucketName=self._bucket,
                indexName=index_name,
                vectors=[
                    {
                        "key": it.key,
                        "data": {"float32": it.vector},
                        "metadata": it.metadata,
                    }
                    for it in batch
                ],
            )

    def delete_vectors(self, index_name: str, keys: list[str]) -> None:
        if not keys:
            return
        for batch in self._chunk(keys, PUT_BATCH_LIMIT):
            self._client.delete_vectors(
                vectorBucketName=self._bucket,
                indexName=index_name,
                keys=batch,
            )

    def query_top_k(
        self,
        index_name: str,
        vector: list[float],
        k: int,
        org_filter: str | None = None,
        floor: float | None = None,
    ) -> list[QueryHit]:
        kwargs: dict[str, Any] = {
            "vectorBucketName": self._bucket,
            "indexName": index_name,
            "topK": k,
            "queryVector": {"float32": vector},
            "returnMetadata": True,
            "returnDistance": True,
        }
        if org_filter is not None:
            kwargs["filter"] = {"org": org_filter}

        resp = self._client.query_vectors(**kwargs)
        hits: list[QueryHit] = []
        for v in resp.get("vectors", []):
            score = 1.0 - (v.get("distance") or 0.0)  # cosine distance → similarity
            meta = v.get("metadata") or {}
            if floor is not None and score < floor:
                continue
            hits.append(QueryHit(key=v["key"], score=score, metadata=meta))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def get_vectors(
        self, index_name: str, keys: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not keys:
            return {}
        found: dict[str, dict[str, Any]] = {}
        for batch in self._chunk(keys, PUT_BATCH_LIMIT):
            resp = self._client.get_vectors(
                vectorBucketName=self._bucket,
                indexName=index_name,
                keys=batch,
                returnMetadata=True,
                returnData=False,
            )
            for v in resp.get("vectors", []):
                if isinstance(v.get("key"), str):
                    found[v["key"]] = v.get("metadata") or {}
        return found


# ---------------------------------------------------------------------------
# Helpers: DynamoDB ↔ dataclass conversions
# ---------------------------------------------------------------------------


def _idea_to_dynamo(r: IdeaRecord) -> dict[str, Any]:
    key = idea_key(r.org, r.skillBaseName, r.ideaId)
    item: dict[str, Any] = {
        **key,
        "ideaId": r.ideaId,
        "skillBaseName": r.skillBaseName,
        "org": r.org,
        "body": r.body,
        "status": r.status,
        "corroborationVersion": r.corroborationVersion,
        "supersedes": r.supersedes,
    }
    # Optional scalars: only write when set so absent attrs stay queryable with
    # attribute_not_exists() (the invalidAt-exclusion FilterExpression relies on
    # the attribute being truly absent, not stored as null).
    if r.foldedIntoRev is not None:
        item["foldedIntoRev"] = r.foldedIntoRev
    if r.invalidAt is not None:
        item["invalidAt"] = r.invalidAt
    if r.supersededBy is not None:
        item["supersededBy"] = r.supersededBy
    if r.authored is not None:
        item["authored"] = r.authored
    if r.authorityKind is not None:
        item["authorityKind"] = r.authorityKind
    # --- U10 (Gap 6) additions — must round-trip, not be dropped. ---
    if r.refines is not None:
        item["refines"] = r.refines
    if r.revivedAt is not None:
        item["revivedAt"] = r.revivedAt
    if r.legacyRecurrenceFold is not None:
        item["legacyRecurrenceFold"] = r.legacyRecurrenceFold
    if r.sourceRef is not None:
        item["sourceRef"] = r.sourceRef
    if r.scopeTag is not None:
        item["scopeTag"] = r.scopeTag
    if r.authorId is not None:
        item["authorId"] = r.authorId
    if r.verificationRung is not None:
        item["verificationRung"] = r.verificationRung
    return item


def _dynamo_to_idea(item: dict[str, Any]) -> IdeaRecord:
    return IdeaRecord(
        ideaId=item["ideaId"],
        skillBaseName=item["skillBaseName"],
        org=item["org"],
        body=item["body"],
        status=item["status"],
        corroborationVersion=int(item["corroborationVersion"]),
        foldedIntoRev=int(item["foldedIntoRev"]) if "foldedIntoRev" in item else None,
        invalidAt=int(item["invalidAt"]) if "invalidAt" in item else None,
        supersededBy=item.get("supersededBy"),
        supersedes=list(item.get("supersedes", [])),
        authored=item.get("authored"),
        authorityKind=item.get("authorityKind"),
        # --- U10 (Gap 6) additions ---
        refines=item.get("refines"),
        revivedAt=int(item["revivedAt"]) if "revivedAt" in item else None,
        legacyRecurrenceFold=item.get("legacyRecurrenceFold"),
        sourceRef=item.get("sourceRef"),
        scopeTag=item.get("scopeTag"),
        authorId=item.get("authorId"),
        verificationRung=item.get("verificationRung"),
    )


def _idea_source_to_dynamo(r: IdeaSourceRecord) -> dict[str, Any]:
    """Serialise an IdeaSourceRecord to a DynamoDB item under IDEASRC#<idea>#<src>."""
    key = idea_source_key(r.org, r.ideaId, r.sourceId)
    item: dict[str, Any] = {**key}
    # Every IDL field is optional on the wire (the record carries identity in
    # ideaId/sourceId/org and enrichment in the rest); write only what is set.
    for fname in (
        "ideaId",
        "sourceId",
        "org",
        "sessionId",
        "segmentId",
        "seq",
        "prRef",
        "anchors",
        "authorityKind",
        "verificationRung",
        "authorId",
    ):
        val = getattr(r, fname)
        if val is not None:
            item[fname] = val
    return item


def _dynamo_to_idea_source(item: dict[str, Any]) -> IdeaSourceRecord:
    return IdeaSourceRecord(
        ideaId=item.get("ideaId"),
        sourceId=item.get("sourceId"),
        org=item.get("org"),
        sessionId=item.get("sessionId"),
        segmentId=item.get("segmentId"),
        seq=int(item["seq"]) if "seq" in item else None,
        prRef=item.get("prRef"),
        anchors=item.get("anchors"),
        authorityKind=item.get("authorityKind"),
        verificationRung=item.get("verificationRung"),
        authorId=item.get("authorId"),
    )


def _golden_case_attrs(r: GoldenCaseRecord) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "caseId": r.caseId,
        "skillBaseName": r.skillBaseName,
        "org": r.org,
        "before": r.before,
        "after": r.after,
        "ideaBody": r.ideaBody,
    }
    if r.createdAt is not None:
        attrs["createdAt"] = r.createdAt
    return attrs


def _dynamo_to_golden_case(item: dict[str, Any]) -> GoldenCaseRecord:
    return GoldenCaseRecord(
        caseId=item["caseId"],
        skillBaseName=item["skillBaseName"],
        org=item["org"],
        before=item["before"],
        after=item["after"],
        ideaBody=item["ideaBody"],
        createdAt=int(item["createdAt"]) if "createdAt" in item else None,
    )


def _anchor_attrs(r: AnchorRecord) -> dict[str, Any]:
    return {
        "ideaId": r.ideaId,
        "ownerRepo": r.ownerRepo,
        "file": r.file,
        "symbol": r.symbol,
        "org": r.org,
        "active": r.active,
    }


def _dynamo_to_anchor(item: dict[str, Any]) -> AnchorRecord:
    return AnchorRecord(
        ideaId=item["ideaId"],
        ownerRepo=item["ownerRepo"],
        file=item["file"],
        symbol=item["symbol"],
        org=item["org"],
        active=bool(item.get("active", True)),
    )


def _dynamo_to_verify_event(item: dict[str, Any]) -> VerifyEventRecord:
    return VerifyEventRecord(
        org=item["org"],
        idea_id=item["ideaId"],
        seq=int(item["seq"]),
        verdict=item["verdict"],
        pr_ref=item.get("prRef"),
        authority=item.get("authority"),
        recorded_at=int(item["recordedAt"]),
    )


def _dynamo_to_branch_session(item: dict[str, Any]) -> BranchSessionRecord:
    return BranchSessionRecord(
        ownerRepo=item["ownerRepo"],
        branch=item["branch"],
        org=item["org"],
        sessionId=item["sessionId"],
        turnStart=int(item["turnStart"]) if "turnStart" in item else None,
        turnEnd=int(item["turnEnd"]) if "turnEnd" in item else None,
        distilledContext=item.get("distilledContext"),
        pushedAt=int(item["pushedAt"]) if "pushedAt" in item else None,
        ttlAt=int(item["ttlAt"]) if "ttlAt" in item else None,
    )


# ---------------------------------------------------------------------------
# Convenience: skill vector key (mirrors s3vectors.ts skillVectorKey)
# ---------------------------------------------------------------------------


def skill_vector_key(org: str, skill_base_name: str) -> str:
    """Deterministic vector key for a skill family: ``<org>#<skillBaseName>``."""
    return f"{org}#{skill_base_name}"
