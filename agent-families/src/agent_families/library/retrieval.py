"""Runtime retrieval into pipeline prompts — R3 §4 insight-level whole-store rewrite (plan-009 U6).

The R2 retrieval was **family-scoped and skill-ranked**: each pipeline family
embedded a query, ranked *its own family's skills* by max-member cosine, reserved
most of the budget for the working agent's own skills (the ``own_skills_share``
prior), and filled the budget with **whole skills**. R3 §4 ("ownership ≠
reachability, total") deletes that whole posture. Retrieval is now **whole-store
and insight-level**:

- the candidate universe is **every active insight in the store**, not one
  family's pool — an insight owned by any module is reachable purely by relevance
  (the R13 invariant ``test_ownership_not_reachability_total`` pins);
- ranking is by **per-insight cosine** on the dedicated retrieval vector
  (``insight_vectors.retrieval_embedding`` — the ``search_document:`` geometry
  re-embedded by ``VecIndex.rebuild_retrieval_vectors``, 010 U2/R4; this replaced
  the plan-008/009 stopgap that ranked on the legacy/clustering ``embedding``
  column), *not* whole-skill max-member cosine;
- the budget is filled at **insight granularity** — whole insight blocks, never
  mid-insight, but no skill-level aggregation node sits in the ranked output;
- the **own-skills prior is gone** (``own_skills_share`` / ``DEFAULT_OWN_SKILLS_SHARE``
  and the own/sibling budget split are deleted) — two insights that tie on cosine
  rank in stable cosine/id order regardless of which module owns them;
- the **R14c boundary-ticket subsystem** (``classify_boundary`` /
  ``run_boundary_ticket`` / the persona-refinement machinery, R2 ``retrieval.py``
  lines 420-658) is **deleted** — boundary refinement belonged to the family-router
  world this plan demotes.

Quarantine visibility stays **mode-keyed** (carried from R2): a quarantined insight
is invisible in ``training`` / ``benchmark`` runs and visible only in a ``trial``
run for its own batch. Status is resolved as-of the requested snapshot via
:meth:`Store.status_at`, so retrieval at an old ``--snapshot`` reproduces old bytes.

Tunables are caller-supplied (:class:`RetrievalParams`): the run-assembly wiring
routes ``budget_tokens`` / ``relevance_floor`` from ``thresholds.toml`` — nothing
here reads config.

## Plan-009 U6 deviation — backward-compatibility seam (documented, not hidden)

R13 specifies *removing* ``family_id`` / ``working_agent_id`` from the signature and
the ``skills`` field from the result. Plan 009 U6 lands **independently of its
downstream callers** (the planner/worker run-assembly and the run-memory
composer), whose migration belongs to the U7 demotion sweep. So this module keeps
two **inert** compatibility affordances until that sweep:

- ``retrieve`` still *accepts* ``family_id`` / ``working_agent_id`` but **ignores
  them for reachability** — passing any value (even an unrelated family) cannot
  gate the whole-store result (asserted by ``test_ownership_not_reachability_total``).
  They are no-ops, not scope filters.
- :class:`RetrievalResult` keeps a **derived** ``skills`` provenance tuple (the
  owning skills of the *retrieved insights*) so unmigrated callers keep working.
  It is provenance, **not** a ranking node — the ranked, budget-filled output is
  ``insights`` (insight ids). ``test_ranks_insights_not_skills`` pins that the
  granularity is insight-level.

Neither affordance restores R2 behavior; both are slated for deletion when U7
migrates the callers. The smallest faithful adaptation under the wave's
"one unit, don't touch other units' files" constraint.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from agent_families.rendering import Renderer
from agent_families.store import RUN_MODES, Store
from agent_families.vecindex import VEC_TABLE

# Drop reasons logged for every insight that does NOT make it into the injection
# (R3: "log every drop with rank and size").
DROP_BUDGET = "budget"
DROP_RELEVANCE_GATE = "relevance_gate"

_TOKEN_RE = re.compile(r"\S+")


class RetrievalError(Exception):
    """Retrieval misuse or invariant breach with an actionable message."""


def count_tokens(text: str) -> int:
    """Deterministic, offline token count: whitespace-delimited runs.

    A coarse proxy (no model load, no quota) that is monotone in real token
    count — all the budget invariant needs. The *budget value* is the tunable
    (thresholds.toml); the counting method is fixed.
    """
    return len(_TOKEN_RE.findall(text))


# --- query construction (R2) -------------------------------------------------
# Per-family query TEXT builders. Embedding (the literal ``search_query:`` prefix)
# is the caller's job via EmbeddingService.embed_query — these stay offline and
# string-only so they are trivially testable and the embed seam owns the prefix.


def planner_query(increment_request_msgs: list[str], qa_transcript: list[str]) -> str:
    """Planner query (R2): increment-request MSGs + the Q&A transcript."""
    return "\n".join([*increment_request_msgs, *qa_transcript]).strip()


def worker_query(ticket: dict) -> str:
    """Worker query (R2): ticket text + its acceptance criteria."""
    parts = [
        ticket.get("title", ""),
        ticket.get("description", ""),
        *(ac.get("text", "") for ac in ticket.get("acceptance_criteria", ())),
    ]
    return "\n".join(p for p in parts if p).strip()


def verifier_query(ticket: dict, typed_failures: list[str]) -> str:
    """Verifier query (R2): ACs + the latest typed-failure records."""
    parts = [
        *(ac.get("text", "") for ac in ticket.get("acceptance_criteria", ())),
        *typed_failures,
    ]
    return "\n".join(p for p in parts if p).strip()


# --- tunables ----------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalParams:
    """Caller-supplied retrieval tunables (routed from thresholds.toml by the
    run-assembly wiring; never read from config here).

    R13: the ``own_skills_share`` prior is **gone** — whole-store retrieval has no
    own/sibling budget split.
    """

    budget_tokens: int
    relevance_floor: float

    def __post_init__(self) -> None:
        if self.budget_tokens <= 0:
            raise RetrievalError(
                f"budget_tokens must be a positive token budget, got"
                f" {self.budget_tokens}"
            )
        if not (0.0 <= self.relevance_floor <= 1.0):
            raise RetrievalError(
                f"relevance_floor must be in [0.0, 1.0], got {self.relevance_floor}"
            )


# --- result shapes -----------------------------------------------------------


@dataclass(frozen=True)
class InsightCandidate:
    """One ranked insight: its cosine to the query and its rendered size."""

    insight_id: int
    score: float  # cosine to the query on the retrieval vector
    token_count: int
    content: bytes  # the insight's rendered block bytes
    skill_ids: tuple[int, ...]  # owning skills (provenance, id-ordered; may be empty)


@dataclass(frozen=True)
class RetrievalDrop:
    """An insight that did not make the injection — logged with rank and size (R3)."""

    insight_id: int
    rank: int
    token_count: int
    score: float
    reason: str  # DROP_BUDGET | DROP_RELEVANCE_GATE


@dataclass(frozen=True)
class RetrievalResult:
    """The injected section plus the full audit trail behind it.

    ``insights`` is the ranked, budget-filled output at **insight** granularity —
    the load-bearing field. ``skills`` is a *derived* provenance convenience (the
    owning skills of the retrieved insights), retained only for unmigrated callers
    (see the module-level U6 deviation note); it never participates in ranking.
    """

    insights: tuple[int, ...]  # injected insight ids, in rank order
    injected_bytes: bytes
    injected_text: str
    injected_token_count: int
    budget_tokens: int
    drops: tuple[RetrievalDrop, ...]
    candidates: tuple[InsightCandidate, ...]  # every scored visible insight, ranked
    pool_insight_ids: frozenset[int]  # the whole visible store (R4 visibility)
    mode: str
    skills: tuple[int, ...]  # DERIVED provenance: owning skills of retrieved insights


# --- cosine over stored vectors ----------------------------------------------


def _unpack(blob) -> list[float]:
    raw = bytes(blob)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise RetrievalError(
            f"query vector has {len(a)} dims but a stored vector has {len(b)};"
            " the index dim is pinned (thresholds.toml [embedding] dim)"
        )
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _load_vectors(store: Store, insight_ids: set[int]) -> dict[int, list[float]]:
    """Load the retrieval vectors for ``insight_ids`` from the vec0 table.

    Reads the dedicated ``retrieval_embedding`` column (010 U2/R4) — the
    ``search_document:`` geometry re-embedded by
    ``VecIndex.rebuild_retrieval_vectors``. This replaced the plan-008/009 stopgap
    that read the legacy/clustering ``embedding`` column. The column is a fixed
    literal here (never caller-derived), matching ``VecIndex._VIEW_COLUMN``'s
    ``"retrieval"`` view.
    """
    if not insight_ids:
        return {}
    placeholders = ", ".join("?" for _ in insight_ids)
    rows = store.conn.execute(
        f"SELECT insight_id, retrieval_embedding FROM {VEC_TABLE}"
        f" WHERE insight_id IN ({placeholders})",
        tuple(insight_ids),
    ).fetchall()
    return {row["insight_id"]: _unpack(row["retrieval_embedding"]) for row in rows}


# --- whole-store visibility (R4) ---------------------------------------------


def _all_insight_ids(store: Store) -> list[int]:
    rows = store.conn.execute("SELECT id FROM insights ORDER BY id ASC").fetchall()
    return [row["id"] for row in rows]


def _insight_visible(
    store: Store, insight_id: int, snapshot_id: int, mode: str, batch_id: int | None
) -> bool:
    """Whether ``insight_id`` is visible at ``snapshot_id`` under ``mode`` (R4).

    training / benchmark: active only. trial: active + quarantined insights of the
    batch under trial (the mode-keyed visibility-matrix exception, carried verbatim
    from the R2 retrieval but now applied per-insight over the whole store).
    """
    status = store.status_at(insight_id, snapshot_id)
    if status == "active":
        return True
    if mode == "trial" and status == "quarantined":
        row = store.get_insight(insight_id)
        return batch_id is not None and row is not None and row["batch_id"] == batch_id
    return False


def _owning_skills(store: Store, insight_id: int) -> list[tuple[int, str]]:
    """Skills that own ``insight_id`` (provenance), id-ordered. Usually 0 or 1."""
    rows = store.conn.execute(
        "SELECT s.id AS skill_id, s.name AS name FROM skill_members sm"
        " JOIN skills s ON s.id = sm.skill_id WHERE sm.insight_id = ?"
        " ORDER BY s.id ASC",
        (insight_id,),
    ).fetchall()
    return [(row["skill_id"], row["name"]) for row in rows]


def _render_insight(row, skill_names: list[str]) -> bytes:
    """Render one insight as a self-contained, byte-stable block.

    A provenance ``# Skill: <name>`` header names each owning skill (display /
    routing context); the block body is the rule triple. ``newline='\\n'`` is
    pinned implicitly (no platform newline ever appears) so equality means byte
    equality on every platform — the byte-stability invariant the suite pins.
    """
    parts: list[str] = []
    for name in skill_names:
        parts.append(f"# Skill: {name}\n")
    if parts:
        parts.append("\n")
    parts.append(
        f"## Insight {row['id']}\n\n"
        f"- Precondition: {row['precondition']}\n"
        f"- Action: {row['action']}\n"
        f"- Expected outcome: {row['expected_outcome']}\n"
    )
    return "".join(parts).encode("utf-8")


# --- the retrieval entry point -----------------------------------------------


def retrieve(
    store: Store,
    *,
    query_vector: list[float],
    params: RetrievalParams,
    mode: str = "training",
    batch_id: int | None = None,
    snapshot_id: int | None = None,
    renderer: Renderer | None = None,
    family_id: int | None = None,  # U6 deviation: inert no-op (see module docstring)
    working_agent_id: int | None = None,  # U6 deviation: inert no-op
) -> RetrievalResult:
    """Retrieve the budget-bounded **insight-level** injection over the whole store.

    ``query_vector`` is the already-embedded query (callers embed the R2 query text
    with ``EmbeddingService.embed_query``). Every active insight in the store is a
    candidate — ``family_id`` / ``working_agent_id`` are accepted for caller
    compatibility but **do not** scope reachability (R13: ownership ≠ reachability,
    total). ``mode`` keys quarantine visibility (R4): ``trial`` additionally
    requires ``batch_id``.
    """
    if mode not in RUN_MODES:
        raise RetrievalError(
            f"unknown run mode '{mode}' (expected one of {RUN_MODES})"
        )
    if mode == "trial" and batch_id is None:
        raise RetrievalError(
            "trial-mode retrieval requires the batch_id under trial (R4: quarantined"
            " insights are visible only in their batch's trial run)"
        )
    snap = store.current_snapshot_id() if snapshot_id is None else snapshot_id
    if renderer is None:
        renderer = Renderer(store)

    # 1. The candidate universe is every VISIBLE insight in the store (R13) — no
    #    family pool, no ownership scope. Visibility is mode-keyed (R4).
    visible_ids = [
        iid
        for iid in _all_insight_ids(store)
        if _insight_visible(store, iid, snap, mode, batch_id)
    ]
    vectors = _load_vectors(store, set(visible_ids))

    candidates: list[InsightCandidate] = []
    for iid in visible_ids:
        if iid not in vectors:
            continue
        row = store.get_insight(iid)
        if row is None:
            continue
        owners = _owning_skills(store, iid)
        skill_names = [name for _sid, name in owners]
        content = _render_insight(row, skill_names)
        candidates.append(
            InsightCandidate(
                insight_id=iid,
                score=_cosine(query_vector, vectors[iid]),
                token_count=count_tokens(content.decode("utf-8")),
                content=content,
                skill_ids=tuple(sid for sid, _name in owners),
            )
        )

    # 2. Rank by descending cosine, then insight id for determinism. There is NO
    #    own-skills prior and NO skill aggregation — a flat per-insight order, so
    #    two insights that tie on cosine sort by id regardless of owning module.
    ranked = sorted(candidates, key=lambda c: (-c.score, c.insight_id))

    # 3. Relevance gate + budget fill at INSIGHT granularity: an insight below the
    #    floor is dropped; an insight that fits the remaining budget is injected
    #    whole; an over-budget insight is dropped (logged), never truncated. The
    #    scan continues so a smaller later insight can still claim leftover budget.
    budget = params.budget_tokens
    included: list[InsightCandidate] = []
    drops: list[RetrievalDrop] = []
    used = 0
    rank = 0
    for c in ranked:
        rank += 1
        if c.score < params.relevance_floor:
            drops.append(
                RetrievalDrop(
                    c.insight_id, rank, c.token_count, c.score, DROP_RELEVANCE_GATE
                )
            )
        elif used + c.token_count <= budget:
            included.append(c)
            used += c.token_count
        else:
            drops.append(
                RetrievalDrop(c.insight_id, rank, c.token_count, c.score, DROP_BUDGET)
            )

    injected_bytes = b"\n".join(c.content for c in included)
    # Derived provenance only (see the U6 deviation note): the owning skills of the
    # retrieved insights, id-ordered. Empty when nothing was injected.
    retrieved_skills = sorted({sid for c in included for sid in c.skill_ids})
    return RetrievalResult(
        insights=tuple(c.insight_id for c in included),
        injected_bytes=injected_bytes,
        injected_text=injected_bytes.decode("utf-8"),
        injected_token_count=used,
        budget_tokens=budget,
        drops=tuple(drops),
        candidates=tuple(ranked),
        pool_insight_ids=frozenset(visible_ids),
        mode=mode,
        skills=tuple(retrieved_skills),
    )


# --- the retrieval-geometry worth-it check (010 U2/R5) -----------------------


@dataclass(frozen=True)
class GeometrySplitVerdict:
    """The measured verdict for "is the dedicated retrieval vector worth it?" (R5).

    Over a hand-labeled ``(query, document)`` pair set we measure how tightly each
    geometry binds a query to its true document: ``dedicated_mean`` pairs
    ``embed_query`` (``search_query:``) with ``embed_retrieval`` (``search_document:``);
    ``stopgap_mean`` pairs the same query with ``embed_full`` (``clustering:`` — the
    pre-010 stopgap). A higher mean means true documents sit closer to their query
    in absolute cosine, which is what clears ``retrieve``'s ``relevance_floor`` gate.

    ``margin > 0`` is the measured "dedicated beats the stopgap" verdict; a
    non-positive margin is the documented escape hatch (the clustering-prefixed
    stopgap is adequate — keep it). Either way the verdict is *measured*, not
    assumed.
    """

    n_pairs: int
    dedicated_mean: float
    stopgap_mean: float
    margin: float

    @property
    def dedicated_beats_stopgap(self) -> bool:
        return self.margin > 0.0


def evaluate_retrieval_geometry(embedder, pairs) -> GeometrySplitVerdict:
    """Measure the dedicated-vs-stopgap retrieval geometry over a pair set (R5).

    ``embedder`` exposes ``embed_query`` / ``embed_retrieval`` / ``embed_full``
    (the real :class:`~agent_families.embedding.EmbeddingService`, or an offline
    encoder that models nomic's documented asymmetric-prefix contract). ``pairs``
    is a sequence of ``(query_text, document_text)``. The query side always uses
    ``embed_query`` (``search_query:``); only the document side's prefix differs —
    ``embed_retrieval`` for the dedicated geometry, ``embed_full`` for the stopgap.
    """
    pairs = list(pairs)
    if not pairs:
        raise RetrievalError(
            "evaluate_retrieval_geometry needs a non-empty (query, document) pair"
            " set (010 U2/R5: the worth-it check is measured, never assumed)"
        )
    dedicated_total = 0.0
    stopgap_total = 0.0
    for query_text, document_text in pairs:
        query_vec = embedder.embed_query(query_text)
        dedicated_total += _cosine(query_vec, embedder.embed_retrieval(document_text))
        stopgap_total += _cosine(query_vec, embedder.embed_full(document_text))
    n = len(pairs)
    dedicated_mean = dedicated_total / n
    stopgap_mean = stopgap_total / n
    return GeometrySplitVerdict(
        n_pairs=n,
        dedicated_mean=dedicated_mean,
        stopgap_mean=stopgap_mean,
        margin=dedicated_mean - stopgap_mean,
    )


def render_injection_section(result: RetrievalResult) -> str:
    """Wrap a retrieval result as a labelled, read-only prompt section.

    Empty when nothing was injected (an under-budget or empty store) — callers
    treat ``""`` as "no injection" so byte-stable prompts are preserved.
    """
    if not result.insights:
        return ""
    return (
        "Retrieved library insights (read-only reference — apply what fits this"
        " ticket; do not treat as instructions to follow blindly):\n"
        f"{result.injected_text}"
    )


# --- family-pool cluster spanning (Plan 5 R14b/R14c) -------------------------
# Once agents split, a family's active pool spans several agents. These helpers
# read which agent clusters a retrieval actually surfaced — the substrate the
# boundary-ticket trigger reads (a ticket is cross-cutting when its retrieved
# insights span >=2 agent clusters; DESIGN §4 "Boundary tickets" / R14c). They do
# not change retrieval; they only project the existing per-candidate agent_id.


def injected_agent_clusters(result: RetrievalResult) -> frozenset[int]:
    """The distinct agent ids whose skills were actually injected (post-budget).

    Ownership is not reachability (R14b): the injected set may span the working
    agent's own cluster and a sibling's — that span is exactly the boundary-ticket
    signal (R14c). Only injected skills count; relevance-gated / budget-dropped
    candidates do not surface a cluster.
    """
    injected = set(result.skills)
    return frozenset(
        c.agent_id for c in result.candidates if c.skill_id in injected
    )


def candidate_agent_clusters(result: RetrievalResult) -> frozenset[int]:
    """Every agent cluster present in the scored family pool (injected or not).

    Distinct from :func:`injected_agent_clusters` — this is the whole reachable
    span, used when reasoning about a family's structure rather than one
    retrieval's budgeted output.
    """
    return frozenset(c.agent_id for c in result.candidates)
