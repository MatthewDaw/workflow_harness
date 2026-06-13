"""authored.py — U8: Authored ingestion (user directives + pasted-text decomposition).

The second ingestion lane.  A human puts knowledge into the memory graph
directly, bypassing the PR gate:

  (a) **User directives** — stated in-session ("remember: no dashes") or via
      the Memories tab (REST PUT).  One source → one ``user_directive`` idea,
      immediately active, necessity-exempt.

  (b) **Authored text ingestion** — paste arbitrary prose (a good skill, a
      doc).  Split into atomic paragraph-ideas → N ``authored_import`` nodes,
      top authority, immediately active, necessity-exempt.

Both paths:
  1. Persist the source to the MEM# KV store (DynamoMemKvStore in production;
     the real DynamoDB ``MEM#<userId>#<name>`` key used by ``rest/memories.ts``,
     sharing the same table + key format; MemKvStore for offline tests).
  2. Bridge each node into the idea graph as an authored idea.
  3. Run a cosine+NLI semantic scan over active inferred ideas (org+scope-
     filtered) — any ``supersede`` verdict routes into U5 with the authored
     idea as the predetermined winner (authority invariant).

Key invariants
--------------
* **Immediately active**: no verified_K, no golden case.
* **Necessity-exempt**: authorityKind in {user_directive, authored_import}
  → never ablated by the necessity gate.
* **Authority safety**: authored > inferred; a later authored directive
  supersedes an earlier one (recency, same tier).
* **Two-store coherence**: editing the source re-decomposes + reconciles;
  deleting it un-bridges all child nodes.
* **Dedup by contentHash**: re-pasting the same text dedupes (no duplicate
  authored nodes for the same content).
* **Input hygiene**: size cap + U1 secret-scrub on all authored text before
  decomposition; no URL fetch (no SSRF surface).
* **Cross-org guard**: mirrors the org guard on every write path.

Production adapter
------------------
:func:`make_supersede_fn` builds the ``supersede_fn`` callable that bridges
the authored semantic scan's keyword-argument API into the real
``supersession.supersede(req: SupersedeRequest, store, ...)`` call.  Wire this
in ``authored_ingestion.py``'s handler to reach the real U5 path in production.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from learning_service.db.store import LearningStore
    from learning_service.telemetry import TelemetryAccumulator

from learning_service.db.store import OrgGuardError, VersionConflictError
from learning_service.schema.generated.py_types import IdeaRecord, IdeaSourceRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / limits
# ---------------------------------------------------------------------------

# Maximum bytes of pasted text accepted before truncation.
AUTHORED_TEXT_SIZE_CAP: int = 64_000  # ~64 KB

# Authority kinds that this module writes.
AUTHORITY_USER_DIRECTIVE = "user_directive"
AUTHORITY_AUTHORED_IMPORT = "authored_import"

# Authority kinds considered "authored" (exempt from necessity, can supersede
# inferred ideas but not each other without recency ordering).
_AUTHORED_KINDS: frozenset[str] = frozenset(
    {AUTHORITY_USER_DIRECTIVE, AUTHORITY_AUTHORED_IMPORT}
)


# ---------------------------------------------------------------------------
# In-memory MEM# KV store (standing in for DynamoDB MEM#<userId>#<name>)
# ---------------------------------------------------------------------------


@dataclass
class MemEntry:
    """One named memory entry (a directive or a paste source)."""
    user_id: str
    project_id: str
    name: str
    content: str
    kind: str           # "directive" | "paste"
    updated_at: int     # epoch-ms


@dataclass
class MemKvStore:
    """In-memory simulation of the DynamoDB MEM#<userId>#<name> KV store.

    Key = (project_id, user_id, name).  Overwrites on the same key (the
    Memories-tab reconcile semantics — last-writer-wins per name).
    """
    _entries: dict[tuple[str, str, str], MemEntry] = field(default_factory=dict)

    def put(self, entry: MemEntry) -> None:
        """Write (or overwrite) an entry."""
        self._entries[(entry.project_id, entry.user_id, entry.name)] = entry

    def get(self, project_id: str, user_id: str, name: str) -> MemEntry | None:
        """Return the entry or None."""
        return self._entries.get((project_id, user_id, name))

    def delete(self, project_id: str, user_id: str, name: str) -> bool:
        """Delete an entry.  Returns True if it existed."""
        return self._entries.pop((project_id, user_id, name), None) is not None

    def list_for_project(self, project_id: str) -> list[MemEntry]:
        """Return all entries for a project (all authors)."""
        return [e for (pid, _, _), e in self._entries.items() if pid == project_id]

    def list_for_user(self, project_id: str, user_id: str) -> list[MemEntry]:
        """Return all entries for a specific author in a project."""
        return [
            e for (pid, uid, _), e in self._entries.items()
            if pid == project_id and uid == user_id
        ]


# ---------------------------------------------------------------------------
# DynamoDB-backed MEM# KV store (production)
# ---------------------------------------------------------------------------


class DynamoMemKvStore:
    """Production MEM# KV store backed by DynamoDB.

    Key format matches ``rest/memories.ts`` exactly:
      PK = ``PROJ#<projectId>``
      SK = ``MEM#<userId>#<name>``

    This is the same partition and key format the TS ``replaceUserMemories``
    and ``listMemories`` calls write/read, so the two stores are coherent:
    a TS machine-sync PUT and a Python authored-ingestion PUT write to the
    same DynamoDB item — the two-store coherence guarantee.
    """

    def __init__(
        self,
        table_name: str,
        *,
        dynamodb_resource: Any = None,
    ) -> None:
        if not table_name:
            raise ValueError("DynamoMemKvStore requires a non-empty table_name")
        self._table_name = table_name
        if dynamodb_resource is not None:
            self._table = dynamodb_resource.Table(table_name)
        else:
            import boto3  # type: ignore[import]
            self._table = boto3.resource("dynamodb").Table(table_name)

    # -- Key helpers ----------------------------------------------------------

    @staticmethod
    def _pk(project_id: str) -> str:
        return f"PROJ#{project_id}"

    @staticmethod
    def _sk(user_id: str, name: str) -> str:
        return f"MEM#{user_id}#{name}"

    # -- CRUD -----------------------------------------------------------------

    def put(self, entry: MemEntry) -> None:
        """Write (or overwrite) a MEM# entry.  Mirrors TS replaceUserMemories PUT."""
        item = {
            "PK": self._pk(entry.project_id),
            "SK": self._sk(entry.user_id, entry.name),
            "projectId": entry.project_id,
            "userId": entry.user_id,
            "name": entry.name,
            "content": entry.content,
            "kind": entry.kind,
            "updatedAt": entry.updated_at,
        }
        self._table.put_item(Item=item)

    def get(self, project_id: str, user_id: str, name: str) -> MemEntry | None:
        """Return the entry or None."""
        resp = self._table.get_item(
            Key={"PK": self._pk(project_id), "SK": self._sk(user_id, name)}
        )
        item = resp.get("Item")
        return self._item_to_entry(item) if item else None

    def delete(self, project_id: str, user_id: str, name: str) -> bool:
        """Delete an entry.  Returns True if it existed."""
        resp = self._table.get_item(
            Key={"PK": self._pk(project_id), "SK": self._sk(user_id, name)}
        )
        if "Item" not in resp:
            return False
        self._table.delete_item(
            Key={"PK": self._pk(project_id), "SK": self._sk(user_id, name)}
        )
        return True

    def list_for_project(self, project_id: str) -> list[MemEntry]:
        """Return all entries for a project (all authors)."""
        from boto3.dynamodb.conditions import Key  # type: ignore[import]

        resp = self._table.query(
            KeyConditionExpression=(
                Key("PK").eq(self._pk(project_id)) & Key("SK").begins_with("MEM#")
            )
        )
        return [self._item_to_entry(item) for item in resp.get("Items", [])]

    def list_for_user(self, project_id: str, user_id: str) -> list[MemEntry]:
        """Return all entries for a specific author in a project."""
        from boto3.dynamodb.conditions import Key  # type: ignore[import]

        resp = self._table.query(
            KeyConditionExpression=(
                Key("PK").eq(self._pk(project_id))
                & Key("SK").begins_with(f"MEM#{user_id}#")
            )
        )
        return [self._item_to_entry(item) for item in resp.get("Items", [])]

    @staticmethod
    def _item_to_entry(item: dict[str, Any]) -> MemEntry:
        return MemEntry(
            user_id=item["userId"],
            project_id=item["projectId"],
            name=item["name"],
            content=item["content"],
            kind=item.get("kind", "directive"),
            updated_at=int(item.get("updatedAt", 0)),
        )


# ---------------------------------------------------------------------------
# Production adapter: authored supersede_fn → real supersession.supersede()
# ---------------------------------------------------------------------------


def make_supersede_fn(
    store: "LearningStore",
    *,
    unfold_mode: str = "shadow",
) -> Callable[..., None]:
    """Return a ``supersede_fn`` that routes through the REAL U5 supersession path.

    The authored semantic scan calls::

        supersede_fn(
            org=...,
            incumbent_idea_id=...,
            incumbent_skill_base_name=...,
            challenger_idea_id=...,
            challenger_authority_kind=...,
            now_ms=...,
        )

    This adapter translates that keyword-argument call into the real
    ``supersession.supersede(req: SupersedeRequest, store, ...)`` signature,
    building a ``SupersedeRequest`` with sentinel values for fields that don't
    apply to the authored (anchor-less) path:

    * ``challenger_pr_number = 0`` — authored directives have no PR number;
      the authority invariant (authored > inferred) supersedes evidence ranking
      so this value is never used as a tiebreaker.
    * ``challenger_owner_repo = "authored"`` — sentinel; no real repo.
    * ``idea_body_to_remove = None`` — un-fold uses the idea's own body.

    The ``unfold_mode`` defaults to ``"shadow"`` (log only; the un-fold write is
    the sharpest behavioral change and requires the FP calibration gate to open
    first).  Pass ``"enforce"`` to execute real un-fold writes in production
    after the gate is open.

    Parameters
    ----------
    store:
        The production LearningStore (the same instance used by the handler).
    unfold_mode:
        ``"shadow"`` (default) or ``"enforce"``.

    Returns
    -------
    Callable that matches the ``supersede_fn`` signature expected by
    ``_run_authored_semantic_scan``.
    """
    from learning_service.supersession import SupersedeRequest, supersede

    def _supersede_fn(
        org: str,
        incumbent_idea_id: str,
        incumbent_skill_base_name: str,
        challenger_idea_id: str,
        challenger_authority_kind: str,
        now_ms: int,
    ) -> None:
        req = SupersedeRequest(
            org=org,
            incumbent_idea_id=incumbent_idea_id,
            incumbent_skill_base_name=incumbent_skill_base_name,
            challenger_idea_id=challenger_idea_id,
            challenger_skill_base_name=None,   # not needed for authority-wins path
            challenger_pr_number=0,             # authored: no PR number
            challenger_owner_repo="authored",   # sentinel for authored lane
            challenger_authority_kind=challenger_authority_kind,
            idea_body_to_remove=None,           # un-fold uses the idea's own body
        )
        result = supersede(
            req,
            store,
            unfold_mode=unfold_mode,
            now_ms=now_ms,
        )
        logger.info(
            "make_supersede_fn: supersede result action=%r incumbent=%r",
            result.action,
            incumbent_idea_id,
        )

    return _supersede_fn


# ---------------------------------------------------------------------------
# Input hygiene
# ---------------------------------------------------------------------------


def _scrub_secrets(text: str) -> str:
    """Apply the six secret regexes ported from wrapper/internal/topic/fold.go.

    Patterns:
      1. AWS AKIA key IDs
      2. key/secret/token assignment (value redacted)
      3. Bearer tokens
      4. JWT (three base64url segments)
      5. PEM headers
      6. GitHub PATs (ghp_/gho_/github_pat_ prefixes)
    """
    import re
    # AWS AKIA key IDs
    text = re.sub(r'AKIA[0-9A-Z]{16}', '<AWS_KEY>', text)
    # key/secret/token assignment
    text = re.sub(
        r'(?i)(key|secret|token|password|passwd|pwd)\s*[:=]\s*\S+',
        r'\1: <REDACTED>',
        text,
    )
    # Bearer tokens
    text = re.sub(r'(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*', 'Bearer <TOKEN>', text)
    # JWT (three dot-separated base64url segments)
    text = re.sub(
        r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',
        '<JWT>',
        text,
    )
    # PEM headers
    text = re.sub(r'-----BEGIN [A-Z ]+-----', '<PEM_HEADER>', text)
    # GitHub PATs
    text = re.sub(r'(ghp_|gho_|github_pat_)[A-Za-z0-9_]+', '<GITHUB_PAT>', text)
    return text


def _apply_size_cap(text: str, cap: int = AUTHORED_TEXT_SIZE_CAP) -> str:
    """Truncate text to *cap* bytes (UTF-8).  Returns the (possibly truncated) text."""
    encoded = text.encode("utf-8")
    if len(encoded) > cap:
        logger.warning(
            "authored ingestion: text truncated from %d to %d bytes",
            len(encoded),
            cap,
        )
        return encoded[:cap].decode("utf-8", errors="replace")
    return text


def sanitize_authored_text(text: str) -> str:
    """Apply size cap then secret scrub.  Returns the sanitized text."""
    text = _apply_size_cap(text)
    return _scrub_secrets(text)


# ---------------------------------------------------------------------------
# Decomposition helpers
# ---------------------------------------------------------------------------


def _content_hash(text: str) -> str:
    """Deterministic SHA-256 hex digest of the UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def decompose_text_into_paragraphs(text: str) -> list[str]:
    """Split prose text into atomic paragraph-level insights.

    Strategy:
    - Split on double newlines (blank-line-separated paragraphs).
    - Drop empty/trivial paragraphs (< 10 chars after stripping).
    - Trim each paragraph.

    This is a prose-tuned splitter — pasted docs are not diffs, so there are
    no hunk/anchor signals; we extract standalone claims from prose instead.
    """
    raw_paras = text.split("\n\n")
    result: list[str] = []
    for para in raw_paras:
        stripped = para.strip()
        if len(stripped) >= 10:
            result.append(stripped)
    return result


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass
class AuthoredNode:
    """One authored idea node written into the graph."""
    idea_id: str
    authority_kind: str     # user_directive | authored_import
    body: str
    content_hash: str
    source_name: str        # the MEM# entry name that produced this node
    is_new: bool            # False if a node with the same content_hash already existed


@dataclass
class AuthoredIngestionResult:
    """Result of one authored ingestion call."""
    source_name: str
    kind: str               # "directive" | "paste"
    nodes_written: list[AuthoredNode]
    nodes_deduped: int      # nodes skipped due to contentHash match
    supersessions: list[str]  # idea IDs superseded by the authored nodes (semantic scan)


# ---------------------------------------------------------------------------
# Core: write one authored idea into the graph
# ---------------------------------------------------------------------------


def _make_authored_idea(
    org: str,
    idea_id: str,
    skill_base_name: str,
    body: str,
    authority_kind: str,
    source_ref: dict[str, Any],
    scope_tag: str | None,
    now_ms: int,
) -> IdeaRecord:
    """Build an authored IdeaRecord (immediately active, necessity-exempt)."""
    return IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill_base_name,
        org=org,
        body=body,
        status="open",                      # immediately active — no verified_K
        corroborationVersion=0,
        authored=True,
        authorityKind=authority_kind,
        sourceRef=json.dumps(source_ref),
        scopeTag=scope_tag,
    )


def _find_existing_authored_node(
    org: str,
    skill_base_name: str,
    source_name: str,
    content_hash: str,
    store: "LearningStore",
) -> IdeaRecord | None:
    """Return the existing authored node for (source_name, contentHash) or None.

    Scans the current ideas for the skill family looking for an authored idea
    whose sourceRef matches the (source_name, content_hash) pair.  This is the
    dedup gate for re-pasting the same text.
    """
    ideas = store.list_current_ideas(org, skill_base_name)
    for idea in ideas:
        if not idea.authored:
            continue
        if idea.sourceRef is None:
            continue
        try:
            ref = json.loads(idea.sourceRef)
        except (json.JSONDecodeError, TypeError):
            continue
        if (
            ref.get("label") == source_name
            and ref.get("contentHash") == content_hash
        ):
            return idea
    return None


def _find_all_authored_nodes_for_source(
    org: str,
    skill_base_name: str,
    source_name: str,
    store: "LearningStore",
) -> list[IdeaRecord]:
    """Return all authored nodes for a given source name (for reconcile/delete)."""
    ideas = store.list_all_ideas_for_org(org)
    result: list[IdeaRecord] = []
    for idea in ideas:
        if not idea.authored:
            continue
        if idea.sourceRef is None:
            continue
        try:
            ref = json.loads(idea.sourceRef)
        except (json.JSONDecodeError, TypeError):
            continue
        if ref.get("label") == source_name and idea.skillBaseName == skill_base_name:
            result.append(idea)
    return result


# ---------------------------------------------------------------------------
# Semantic scan (anchor-less supersession entry into U5)
# ---------------------------------------------------------------------------


def _run_authored_semantic_scan(
    org: str,
    authored_idea: IdeaRecord,
    store: "LearningStore",
    nli_classify_fn: Callable[..., Any] | None,
    supersede_fn: Callable[..., Any] | None,
    now_ms: int,
) -> list[str]:
    """Scan active inferred ideas (authorityKind='merged') for supersession.

    For each active inferred idea in the same org:
      1. Quick word-Jaccard filter (skip obviously unrelated ideas).
      2. NLI classify (authored as premise, inferred as hypothesis).
      3. If 'contradiction' (supersede) → route into U5 with authored as winner.

    Returns the list of superseded idea IDs.
    """
    if nli_classify_fn is None or supersede_fn is None:
        return []

    superseded_ids: list[str] = []

    # Fetch all current inferred ideas in the org.
    all_org_ideas = store.list_all_ideas_for_org(org)
    inferred_active = [
        i for i in all_org_ideas
        if i.authorityKind == "merged"
        and i.invalidAt is None
    ]

    for candidate in inferred_active:
        # Quick Jaccard pre-filter to avoid NLI on obviously unrelated ideas.
        if not _quick_overlap(authored_idea.body, candidate.body):
            continue

        try:
            result = nli_classify_fn(authored_idea.body, candidate.body)
            label = getattr(result, "label", None) or result.get("label", "neutral")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "authored semantic scan: NLI failed for candidate=%r: %s",
                candidate.ideaId,
                exc,
            )
            continue

        if label == "contradiction":
            # Route into U5 with the authored idea as the predetermined winner.
            logger.info(
                "authored semantic scan: supersede candidate=%r via authored=%r",
                candidate.ideaId,
                authored_idea.ideaId,
            )
            if supersede_fn is not None:
                try:
                    supersede_fn(
                        org=org,
                        incumbent_idea_id=candidate.ideaId,
                        incumbent_skill_base_name=candidate.skillBaseName,
                        challenger_idea_id=authored_idea.ideaId,
                        challenger_authority_kind=authored_idea.authorityKind or AUTHORITY_USER_DIRECTIVE,
                        now_ms=now_ms,
                    )
                    superseded_ids.append(candidate.ideaId)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "authored semantic scan: supersede call failed for %r: %s",
                        candidate.ideaId,
                        exc,
                    )

    return superseded_ids


def _quick_overlap(body_a: str, body_b: str, threshold: float = 0.15) -> bool:
    """Return True if the two bodies share enough words to be worth NLI-classifying.

    Threshold is deliberately low (0.15) to avoid missing related ideas that
    use different vocabulary — this is a coarse pre-filter, not the NLI itself.
    """
    words_a = set(body_a.lower().split())
    words_b = set(body_b.lower().split())
    if not words_a or not words_b:
        return False
    inter = words_a & words_b
    union = words_a | words_b
    return len(inter) / len(union) >= threshold


# ---------------------------------------------------------------------------
# Directive supersession (authored-over-authored, recency wins)
# ---------------------------------------------------------------------------


def _supersede_earlier_directive(
    org: str,
    new_idea: IdeaRecord,
    source_name: str,
    store: "LearningStore",
    now_ms: int,
) -> list[str]:
    """A later directive supersedes an earlier one for the same source name.

    Strategy: find any existing authored node with the same source label and
    a different content hash (i.e. the same "slot" was updated), stamp it with
    invalidAt, and mark the new idea as the superseder.
    """
    superseded: list[str] = []
    existing = _find_all_authored_nodes_for_source(
        org, new_idea.skillBaseName, source_name, store
    )
    for old_idea in existing:
        if old_idea.ideaId == new_idea.ideaId:
            continue
        if old_idea.invalidAt is not None:
            continue  # already retired

        # Check if this is the same "slot" (same source, different content).
        try:
            ref = json.loads(old_idea.sourceRef or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        if ref.get("label") != source_name:
            continue

        # Stamp the old directive as superseded by the new one.
        updated = IdeaRecord(
            ideaId=old_idea.ideaId,
            skillBaseName=old_idea.skillBaseName,
            org=old_idea.org,
            body=old_idea.body,
            status=old_idea.status,
            corroborationVersion=old_idea.corroborationVersion + 1,
            foldedIntoRev=old_idea.foldedIntoRev,
            invalidAt=now_ms,
            supersededBy=new_idea.ideaId,
            supersedes=old_idea.supersedes,
            authored=old_idea.authored,
            authorityKind=old_idea.authorityKind,
            refines=old_idea.refines,
            revivedAt=old_idea.revivedAt,
            legacyRecurrenceFold=old_idea.legacyRecurrenceFold,
            sourceRef=old_idea.sourceRef,
            scopeTag=old_idea.scopeTag,
            authorId=old_idea.authorId,
            verificationRung=old_idea.verificationRung,
        )
        try:
            store.put_idea_conditional(updated, old_idea.corroborationVersion)
            superseded.append(old_idea.ideaId)
            logger.info(
                "authored directive: superseded earlier directive=%r by newer=%r",
                old_idea.ideaId,
                new_idea.ideaId,
            )
        except VersionConflictError:
            logger.warning(
                "authored directive: OCC conflict retiring old directive=%r — skipping",
                old_idea.ideaId,
            )

    return superseded


# ---------------------------------------------------------------------------
# Core: ingest a user directive
# ---------------------------------------------------------------------------


def ingest_directive(
    org: str,
    project_id: str,
    user_id: str,
    name: str,
    content: str,
    skill_base_name: str,
    store: "LearningStore",
    mem_store: MemKvStore,
    *,
    scope_tag: str | None = "project",
    nli_classify_fn: Callable[..., Any] | None = None,
    supersede_fn: Callable[..., Any] | None = None,
    now_ms: int | None = None,
    idea_id_override: str | None = None,
    telemetry: "TelemetryAccumulator | None" = None,
) -> AuthoredIngestionResult:
    """Ingest a user directive: persist to MEM# KV + bridge into the graph.

    Parameters
    ----------
    org:                Owning org slug (cross-org guard applied).
    project_id:         Project id (MEM# partition key component).
    user_id:            User id (MEM# author key).
    name:               Memory name (the MEM#<userId>#<name> slot).
    content:            The directive text (sanitized before write).
    skill_base_name:    Skill family to co-locate the idea with.
    store:              LearningStore (idea writes).
    mem_store:          MemKvStore (MEM# KV persistence).
    scope_tag:          Scope (project / user / org / repo / skill).
    nli_classify_fn:    NLI classify callable for semantic scan (optional).
    supersede_fn:       U5 supersede callable (optional).
    now_ms:             Clock injection for tests.
    idea_id_override:   Force a specific idea_id (for deterministic tests).

    Returns
    -------
    AuthoredIngestionResult
    """
    if not org or not org.strip():
        raise OrgGuardError("ingest_directive")

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    # Input hygiene.
    sanitized = sanitize_authored_text(content)
    content_hash = _content_hash(sanitized)

    # 1. Persist to MEM# KV store (the Memories-tab contract).
    mem_entry = MemEntry(
        user_id=user_id,
        project_id=project_id,
        name=name,
        content=sanitized,
        kind="directive",
        updated_at=now_ms,
    )
    mem_store.put(mem_entry)

    # 2. Dedup: check if an identical node already exists in the graph.
    existing = _find_existing_authored_node(
        org, skill_base_name, name, content_hash, store
    )
    if existing is not None:
        logger.info(
            "ingest_directive: dedup hit for source=%r contentHash=%s — no-op",
            name,
            content_hash,
        )
        return AuthoredIngestionResult(
            source_name=name,
            kind="directive",
            nodes_written=[],
            nodes_deduped=1,
            supersessions=[],
        )

    # 3. Bridge into the graph.
    idea_id = idea_id_override or str(uuid.uuid4())
    source_ref = {
        "kind": AUTHORITY_USER_DIRECTIVE,
        "label": name,
        "contentHash": content_hash,
    }
    idea = _make_authored_idea(
        org=org,
        idea_id=idea_id,
        skill_base_name=skill_base_name,
        body=sanitized,
        authority_kind=AUTHORITY_USER_DIRECTIVE,
        source_ref=source_ref,
        scope_tag=scope_tag,
        now_ms=now_ms,
    )
    store.put_idea(idea)

    # Write a source record for the authored idea.
    src = IdeaSourceRecord(
        ideaId=idea_id,
        sourceId=f"directive#{content_hash}",
        org=org,
        authorityKind=AUTHORITY_USER_DIRECTIVE,
    )
    store.put_idea_source(src)

    # --- Telemetry: record authored idea -----------------------------------------
    if telemetry is not None:
        telemetry.record_idea_authority(AUTHORITY_USER_DIRECTIVE)

    node = AuthoredNode(
        idea_id=idea_id,
        authority_kind=AUTHORITY_USER_DIRECTIVE,
        body=sanitized,
        content_hash=content_hash,
        source_name=name,
        is_new=True,
    )

    # 4. A later directive supersedes an earlier one (recency, same tier).
    superseded = _supersede_earlier_directive(
        org=org,
        new_idea=idea,
        source_name=name,
        store=store,
        now_ms=now_ms,
    )

    # 5. Authored semantic scan: supersede colliding inferred ideas.
    inferred_superseded = _run_authored_semantic_scan(
        org=org,
        authored_idea=idea,
        store=store,
        nli_classify_fn=nli_classify_fn,
        supersede_fn=supersede_fn,
        now_ms=now_ms,
    )

    all_superseded = superseded + inferred_superseded

    logger.info(
        "ingest_directive: org=%r source=%r idea=%r supersessions=%d",
        org, name, idea_id, len(all_superseded),
    )

    return AuthoredIngestionResult(
        source_name=name,
        kind="directive",
        nodes_written=[node],
        nodes_deduped=0,
        supersessions=all_superseded,
    )


# ---------------------------------------------------------------------------
# Core: ingest pasted text
# ---------------------------------------------------------------------------


def ingest_pasted_text(
    org: str,
    project_id: str,
    user_id: str,
    source_name: str,
    text: str,
    skill_base_name: str,
    store: "LearningStore",
    mem_store: MemKvStore,
    *,
    scope_tag: str | None = "project",
    nli_classify_fn: Callable[..., Any] | None = None,
    supersede_fn: Callable[..., Any] | None = None,
    now_ms: int | None = None,
    idea_id_factory: Callable[[], str] | None = None,
    telemetry: "TelemetryAccumulator | None" = None,
) -> AuthoredIngestionResult:
    """Ingest pasted prose text: split into N atomic authored_import nodes.

    Parameters
    ----------
    org:                Owning org slug.
    project_id:         Project id.
    user_id:            User id.
    source_name:        The MEM# entry name (the "paste" slot).
    text:               The pasted prose.
    skill_base_name:    Skill family to co-locate the nodes with.
    store:              LearningStore.
    mem_store:          MemKvStore.
    scope_tag:          Scope tag.
    nli_classify_fn:    NLI classify callable (for semantic scan).
    supersede_fn:       U5 supersede callable (for semantic scan).
    now_ms:             Clock injection.
    idea_id_factory:    Factory for idea IDs (for deterministic tests).

    Returns
    -------
    AuthoredIngestionResult
    """
    if not org or not org.strip():
        raise OrgGuardError("ingest_pasted_text")

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    if idea_id_factory is None:
        idea_id_factory = lambda: str(uuid.uuid4())  # noqa: E731

    # Input hygiene.
    sanitized = sanitize_authored_text(text)

    # Persist to MEM# KV store.
    mem_entry = MemEntry(
        user_id=user_id,
        project_id=project_id,
        name=source_name,
        content=sanitized,
        kind="paste",
        updated_at=now_ms,
    )
    mem_store.put(mem_entry)

    # Decompose into paragraph-level nodes.
    paragraphs = decompose_text_into_paragraphs(sanitized)

    nodes_written: list[AuthoredNode] = []
    nodes_deduped = 0
    all_superseded: list[str] = []

    for para in paragraphs:
        content_hash = _content_hash(para)

        # Dedup by contentHash.
        existing = _find_existing_authored_node(
            org, skill_base_name, source_name, content_hash, store
        )
        if existing is not None:
            nodes_deduped += 1
            logger.info(
                "ingest_pasted_text: dedup hit for source=%r hash=%s — skipping",
                source_name,
                content_hash,
            )
            continue

        idea_id = idea_id_factory()
        source_ref = {
            "kind": AUTHORITY_AUTHORED_IMPORT,
            "label": source_name,
            "contentHash": content_hash,
        }
        idea = _make_authored_idea(
            org=org,
            idea_id=idea_id,
            skill_base_name=skill_base_name,
            body=para,
            authority_kind=AUTHORITY_AUTHORED_IMPORT,
            source_ref=source_ref,
            scope_tag=scope_tag,
            now_ms=now_ms,
        )
        store.put_idea(idea)

        src = IdeaSourceRecord(
            ideaId=idea_id,
            sourceId=f"authored#{content_hash}",
            org=org,
            authorityKind=AUTHORITY_AUTHORED_IMPORT,
        )
        store.put_idea_source(src)

        nodes_written.append(AuthoredNode(
            idea_id=idea_id,
            authority_kind=AUTHORITY_AUTHORED_IMPORT,
            body=para,
            content_hash=content_hash,
            source_name=source_name,
            is_new=True,
        ))

        # --- Telemetry: record authored_import idea --------------------------------
        if telemetry is not None:
            telemetry.record_idea_authority(AUTHORITY_AUTHORED_IMPORT)

        # Semantic scan for each new node.
        inferred_superseded = _run_authored_semantic_scan(
            org=org,
            authored_idea=idea,
            store=store,
            nli_classify_fn=nli_classify_fn,
            supersede_fn=supersede_fn,
            now_ms=now_ms,
        )
        all_superseded.extend(inferred_superseded)

    logger.info(
        "ingest_pasted_text: org=%r source=%r paragraphs=%d written=%d deduped=%d supersessions=%d",
        org, source_name, len(paragraphs),
        len(nodes_written), nodes_deduped, len(all_superseded),
    )

    return AuthoredIngestionResult(
        source_name=source_name,
        kind="paste",
        nodes_written=nodes_written,
        nodes_deduped=nodes_deduped,
        supersessions=all_superseded,
    )


# ---------------------------------------------------------------------------
# Two-store coherence: delete un-bridges all child nodes
# ---------------------------------------------------------------------------


def delete_authored_source(
    org: str,
    project_id: str,
    user_id: str,
    source_name: str,
    skill_base_name: str,
    store: "LearningStore",
    mem_store: MemKvStore,
    *,
    now_ms: int | None = None,
) -> list[str]:
    """Delete a MEM# source and un-bridge all its graph projections.

    Un-bridging stamps ``invalidAt`` on all authored nodes whose sourceRef
    label matches the source name.  The nodes are kept in history (never
    deleted) but excluded from the current active set.

    Returns the list of un-bridged idea IDs.
    """
    if not org or not org.strip():
        raise OrgGuardError("delete_authored_source")

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    # Remove from MEM# KV store.
    mem_store.delete(project_id, user_id, source_name)

    # Un-bridge graph projections.
    nodes = _find_all_authored_nodes_for_source(org, skill_base_name, source_name, store)
    un_bridged: list[str] = []

    for idea in nodes:
        if idea.invalidAt is not None:
            continue  # already retired

        updated = IdeaRecord(
            ideaId=idea.ideaId,
            skillBaseName=idea.skillBaseName,
            org=idea.org,
            body=idea.body,
            status=idea.status,
            corroborationVersion=idea.corroborationVersion + 1,
            foldedIntoRev=idea.foldedIntoRev,
            invalidAt=now_ms,
            supersededBy=idea.supersededBy,
            supersedes=idea.supersedes,
            authored=idea.authored,
            authorityKind=idea.authorityKind,
            refines=idea.refines,
            revivedAt=idea.revivedAt,
            legacyRecurrenceFold=idea.legacyRecurrenceFold,
            sourceRef=idea.sourceRef,
            scopeTag=idea.scopeTag,
            authorId=idea.authorId,
            verificationRung=idea.verificationRung,
        )
        try:
            store.put_idea_conditional(updated, idea.corroborationVersion)
            un_bridged.append(idea.ideaId)
            logger.info(
                "delete_authored_source: un-bridged idea=%r source=%r",
                idea.ideaId,
                source_name,
            )
        except VersionConflictError:
            logger.warning(
                "delete_authored_source: OCC conflict retiring idea=%r — skipping",
                idea.ideaId,
            )

    return un_bridged


# ---------------------------------------------------------------------------
# Agent tool ("remember") == REST parity
# ---------------------------------------------------------------------------


def remember_tool(
    org: str,
    project_id: str,
    user_id: str,
    name: str,
    content: str,
    skill_base_name: str,
    store: "LearningStore",
    mem_store: MemKvStore,
    *,
    scope_tag: str | None = "project",
    nli_classify_fn: Callable[..., Any] | None = None,
    supersede_fn: Callable[..., Any] | None = None,
    now_ms: int | None = None,
    idea_id_override: str | None = None,
    telemetry: "TelemetryAccumulator | None" = None,
) -> AuthoredIngestionResult:
    """Agent-native 'remember' tool — identical to ingest_directive.

    This is the parity path: anything the user can do in the Memories tab
    (REST PUT) the agent can do via this tool call.  The implementation
    delegates to ingest_directive — one code path, two call sites.
    """
    return ingest_directive(
        org=org,
        project_id=project_id,
        user_id=user_id,
        name=name,
        content=content,
        skill_base_name=skill_base_name,
        store=store,
        mem_store=mem_store,
        scope_tag=scope_tag,
        nli_classify_fn=nli_classify_fn,
        supersede_fn=supersede_fn,
        now_ms=now_ms,
        idea_id_override=idea_id_override,
        telemetry=telemetry,
    )
