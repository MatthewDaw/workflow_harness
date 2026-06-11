"""Family router + routing-decision log + boundary-ticket refinement (plan-005 U4).

Two mechanisms the design's self-reorganization is finally fed by (DESIGN §4, §6;
R12, R14c):

1. **The family router** (R12) — per request, a judge-seam call over the family's
   agent *descriptions* selects the specialist. **Every routing decision is
   logged** (request, candidates, choice, confidence) in this module's own
   ``routing_decisions_log`` table, and the chosen agent's ``routing_decisions``
   counter is incremented. That log is the data agent splitting and the §6 routing
   replay require — splitting was deliberately starved until it exists. The log
   additionally records, when the caller supplies skill vectors, the request
   embedding and the *top skill* that served the request: the substrate routing
   replay re-scores a candidate split against (``reflector.agent_split``).

   Like ``library/retrieval.py``, this module is **vector-agnostic at the seam**:
   the caller embeds (``EmbeddingService.embed_query`` live; a fake in the suite)
   and the judge call goes through the same record/replay seam as every other
   ``claude -p`` call. Offline by construction — zero quota, no ``claude`` on PATH.

   The log is **self-owned** (the ``VecIndex.migrate`` precedent): a
   ``CREATE TABLE IF NOT EXISTS`` in :func:`ensure_routing_log`, not a core
   migration — Plan 5's table never edits a shipped Plan 0-4 migration.

2. **Boundary-ticket multi-persona refinement** (R14c, DESIGN §4 "Boundary
   tickets") — a ticket is flagged cross-cutting **only** when routing is ambiguous
   *or* its retrieved insights span >=2 agent clusters (rare). Such a ticket runs
   as Ralph iterations over the shared artifact: the primary persona owns and
   drafts it; **at most one or two** other personas each read the *committed*
   artifact and refine it; the verifier gates. **Artifact-mediated only** — a
   refining persona receives the committed work product and nothing else (no
   context relay, no hidden reasoning — the MAST/Cognition failure this design
   rejects). The hard terminator is the 1-2 cross-persona cap **+** verifier
   acceptance as arbiter **+** the §7 no-progress tripwire (a repeated artifact
   state halts the loop). Not a task split: one persona owns the ticket.

Tunables ride in caller-supplied params (the U2/U5/U6 precedent); the carried
defaults record PROVENANCE in code and are NOT added to ``thresholds.toml`` (its
loader is closed — the same in-code-default discipline as ``MaintenanceParams``).

## Conformance

Routing (R12) test-scenario -> test (in ``tests/test_router.py``):

- routing decisions logged with full candidates -> ``test_routing_decision_logged_with_candidates``
- the chosen agent's routing_decisions counter increments -> ``test_route_increments_chosen_agent_counter``
- routing replay substrate (request vector + top skill) is recorded ->
  ``test_route_records_replay_substrate``
- ambiguous routing (near-tied confidence) is flagged -> ``test_ambiguous_routing_flagged``

Boundary refinement (R14c) — the required acceptance tests (in
``tests/test_boundary_ticket.py``) -> the invariant each enforces:

- ``test_boundary_trigger_is_gated`` — a non-boundary ticket runs exactly one
  persona pass; only a >=2-cluster (or ambiguous-routing) ticket enters the
  multi-persona path. [enforced by :func:`is_boundary_ticket` + :func:`refine_with_personas`]
- ``test_boundary_pass_cap`` — the multi-persona loop performs <=2 cross-persona
  passes, never more, under any fixture. [enforced by :func:`refine_with_personas`]
- ``test_boundary_oscillation_halts`` — an oscillation fixture halts via the cap +
  §7 no-progress tripwire within bounded passes; it does not loop. [enforced by
  the repeated-progress-key detector in :func:`refine_with_personas`]
- ``test_boundary_is_artifact_mediated`` — a refining persona's input is the
  committed artifact from the prior pass; no persona receives another's hidden
  reasoning/context. [enforced by :func:`refine_with_personas` passing only the
  committed artifact to ``refine_fn``]
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_families.judge import request_hash, run_judge
from agent_families.store import Store

ROUTING_LOG_TABLE = "routing_decisions_log"

# A judge seam (the ``stage_b`` precedent): returns an object with ``.output``.
# Live binding is ``run_judge``; the suite injects a deterministic fake.
JudgeFn = Callable[..., object]


class RouterError(Exception):
    """Router misuse or invariant breach with an actionable message."""


# --- tunables (caller-supplied, carried defaults; PROVENANCE per DESIGN §17) ---


@dataclass(frozen=True)
class RouterParams:
    """Caller-supplied router tunables (routed from config by run-assembly;
    never read from ``thresholds.toml`` here — its loader is closed)."""

    # PROVENANCE: §6 routing — a routing call whose top two candidates sit within
    # this confidence margin is "ambiguous" (the R14c boundary trigger / the §17
    # routing-contention split signal). TUNING METRIC: boundary-flag precision vs
    # cross-cutting-ticket recall.
    ambiguity_margin: float = 0.15
    # PROVENANCE: §4 "Boundary tickets" — a ticket is cross-cutting only when its
    # retrieved insights span at least this many agent clusters. TUNING METRIC:
    # boundary-trigger rate (must stay rare).
    boundary_min_clusters: int = 2
    # PROVENANCE: §4 "at most one or two additional personas" — the hard cross-
    # persona cap (terminator). TUNING METRIC: negotiated-artifact quality vs spend.
    max_cross_passes: int = 2

    def __post_init__(self) -> None:
        if not (0.0 <= self.ambiguity_margin <= 1.0):
            raise RouterError(
                f"ambiguity_margin must be in [0.0, 1.0], got {self.ambiguity_margin}"
            )
        if self.boundary_min_clusters < 2:
            raise RouterError(
                "boundary_min_clusters must be >= 2 (a boundary spans clusters),"
                f" got {self.boundary_min_clusters}"
            )
        if self.max_cross_passes < 1:
            raise RouterError(
                f"max_cross_passes must be >= 1, got {self.max_cross_passes}"
            )


# --- the routing-decision log (self-owned table; VecIndex.migrate precedent) ---


def ensure_routing_log(store: Store) -> None:
    """Create the routing-decision log table if absent (idempotent).

    Self-owned, like ``VecIndex.migrate`` — a ``CREATE TABLE IF NOT EXISTS`` rather
    than a core migration, so Plan 5 never edits a shipped Plan 0-4 migration. The
    row records the full decision (R12): request + candidates + choice + confidence,
    plus the optional replay substrate (request vector + top skill) agent splitting
    re-scores against (§6).
    """
    store.conn.execute(
        f"CREATE TABLE IF NOT EXISTS {ROUTING_LOG_TABLE} ("
        "  id                  INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  family_id           INTEGER NOT NULL,"
        "  request_hash        TEXT NOT NULL,"
        "  request             TEXT NOT NULL,"
        "  candidates_json     TEXT NOT NULL,"
        "  chosen_agent_id     INTEGER NOT NULL,"
        "  confidence          REAL,"
        "  request_vector_json TEXT,"
        "  top_skill_id        INTEGER,"
        "  snapshot_id         INTEGER,"
        "  created_at          TEXT NOT NULL"
        ")"
    )


@dataclass(frozen=True)
class RoutingCandidate:
    """One agent the router chooses among: its id, name, and description."""

    agent_id: int
    name: str
    description: str


@dataclass(frozen=True)
class RoutingDecision:
    """One logged routing decision (R12)."""

    decision_id: int
    request_hash: str
    family_id: int
    candidate_agent_ids: tuple[int, ...]
    chosen_agent_id: int
    confidence: float
    ambiguous: bool
    top_skill_id: int | None


# --- routing schema -----------------------------------------------------------


def _routing_schema(candidate_names: Sequence[str]) -> dict:
    """A closed schema: the choice is one of the candidate names; confidence is a
    number. Candidate names (not ids) keep the judge prompt human-legible; the
    caller maps the name back to the id."""
    return {
        "type": "object",
        "properties": {
            "chosen_agent": {"type": "string", "enum": list(candidate_names)},
            "confidence": {"type": "number"},
            "runner_up_confidence": {"type": "number"},
        },
        "required": ["chosen_agent", "confidence"],
        "additionalProperties": False,
    }


def build_routing_prompt(request: str, candidates: Sequence[RoutingCandidate]) -> str:
    """The routing prompt: the request + the family's agent descriptions (R12)."""
    lines = [
        "Route this request to the single best-matching specialist agent.",
        "",
        f"Request:\n{request}",
        "",
        "Candidate agents (name — description):",
    ]
    for c in candidates:
        lines.append(f"- {c.name} — {c.description}")
    lines.append(
        "\nReturn the chosen agent's name, your confidence (0-1), and the"
        " runner-up's confidence."
    )
    return "\n".join(lines)


# --- candidate + skill-affinity helpers ---------------------------------------


def family_candidates(store: Store, family_id: int) -> list[RoutingCandidate]:
    """The family's agents as routing candidates, id-ordered (R12).

    Retired-by-split parents are excluded — routing goes to live specialists, not
    frozen lineage anchors (their ``lineage_status`` marks them)."""
    rows = store.conn.execute(
        "SELECT id, name, description, lineage_status FROM agents"
        " WHERE family_id = ? ORDER BY id ASC",
        (family_id,),
    ).fetchall()
    return [
        RoutingCandidate(r["id"], r["name"], r["description"])
        for r in rows
        if r["lineage_status"] != "retired_by_split"
    ]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise RouterError(
            f"vector dim mismatch: {len(a)} vs {len(b)} (the embedder dim is pinned)"
        )
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _top_skill(
    request_vector: Sequence[float],
    skill_vectors: dict[int, Sequence[float]],
) -> int | None:
    """The skill whose description vector is most affine to the request — the
    replay substrate (§6): the skill that *served* the request pre-split. Ties
    break on skill id for determinism."""
    if not skill_vectors:
        return None
    best_id = None
    best_score = None
    for skill_id in sorted(skill_vectors):
        score = _cosine(request_vector, skill_vectors[skill_id])
        if best_score is None or score > best_score:
            best_score = score
            best_id = skill_id
    return best_id


# --- the routing entry point (R12) --------------------------------------------


def route(
    store: Store,
    *,
    family_id: int,
    request: str,
    judge_fn: JudgeFn | None = None,
    judge_model: str = "sonnet",
    judge_max_retries: int = 0,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
    params: RouterParams = RouterParams(),
    request_vector: Sequence[float] | None = None,
    skill_vectors: dict[int, Sequence[float]] | None = None,
    snapshot_id: int | None = None,
) -> RoutingDecision:
    """Route one request to a family specialist and log the decision (R12).

    The judge seam selects over agent *descriptions*; the decision (request,
    candidates, choice, confidence) is logged and the chosen agent's
    ``routing_decisions`` counter incremented. When ``request_vector`` +
    ``skill_vectors`` are supplied, the request embedding and the top-affinity
    skill are recorded too — the routing-replay substrate agent splitting consumes
    (§6). ``ambiguous`` is set when the top-two confidence margin is within
    ``ambiguity_margin`` (the R14c boundary signal).
    """
    ensure_routing_log(store)
    candidates = family_candidates(store, family_id)
    if not candidates:
        raise RouterError(
            f"family {family_id} has no routable agents (all retired-by-split?)"
        )
    by_name = {c.name: c for c in candidates}

    if len(candidates) == 1:
        # A single generic agent (Phase 3a reality): routing is trivial, but the
        # decision is still logged — that is how the log accumulates toward the
        # min_routing_decisions split gate. Confidence 1.0, unambiguous.
        chosen = candidates[0]
        confidence = 1.0
        ambiguous = False
    else:
        judge = judge_fn if judge_fn is not None else run_judge
        schema = _routing_schema([c.name for c in candidates])
        result = judge(
            build_routing_prompt(request, candidates),
            schema,
            judge_model,
            max_retries=judge_max_retries,
            mode=judge_mode,
            fixtures_dir=judge_fixtures_dir,
        )
        output = result.output
        chosen_name = output.get("chosen_agent")
        if chosen_name not in by_name:
            raise RouterError(
                f"router chose '{chosen_name}', not one of the candidate agents"
                f" {sorted(by_name)} (the closed schema should have prevented this)"
            )
        chosen = by_name[chosen_name]
        confidence = float(output.get("confidence", 0.0))
        runner_up = output.get("runner_up_confidence")
        ambiguous = (
            runner_up is not None
            and abs(confidence - float(runner_up)) <= params.ambiguity_margin
        )

    top_skill_id = None
    vector_json = None
    if request_vector is not None and skill_vectors:
        top_skill_id = _top_skill(request_vector, skill_vectors)
        vector_json = json.dumps([float(x) for x in request_vector])

    snap = store.current_snapshot_id() if snapshot_id is None else snapshot_id
    h = request_hash(request, {"family_id": family_id}, judge_model)
    with store.transaction():
        cur = store.conn.execute(
            f"INSERT INTO {ROUTING_LOG_TABLE} (family_id, request_hash, request,"
            " candidates_json, chosen_agent_id, confidence, request_vector_json,"
            " top_skill_id, snapshot_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                family_id,
                h,
                request,
                json.dumps([c.agent_id for c in candidates]),
                chosen.agent_id,
                confidence,
                vector_json,
                top_skill_id,
                snap,
            ),
        )
        decision_id = cur.lastrowid
        store.conn.execute(
            "UPDATE agents SET routing_decisions = routing_decisions + 1"
            " WHERE id = ?",
            (chosen.agent_id,),
        )
    return RoutingDecision(
        decision_id=decision_id,
        request_hash=h,
        family_id=family_id,
        candidate_agent_ids=tuple(c.agent_id for c in candidates),
        chosen_agent_id=chosen.agent_id,
        confidence=confidence,
        ambiguous=ambiguous,
        top_skill_id=top_skill_id,
    )


# --- routing-log queries (the agent-split replay substrate) -------------------


def routing_decision_count(store: Store, agent_id: int) -> int:
    """How many logged decisions routed to ``agent_id`` — the §6 split gate's
    volume reading (mirrors the ``agents.routing_decisions`` counter)."""
    ensure_routing_log(store)
    row = store.conn.execute(
        f"SELECT COUNT(*) AS n FROM {ROUTING_LOG_TABLE} WHERE chosen_agent_id = ?",
        (agent_id,),
    ).fetchone()
    return row["n"]


def logged_decisions_for_agent(store: Store, agent_id: int) -> list:
    """Every logged decision routed to ``agent_id``, id-ordered — the replay set."""
    ensure_routing_log(store)
    return store.conn.execute(
        f"SELECT * FROM {ROUTING_LOG_TABLE} WHERE chosen_agent_id = ?"
        " ORDER BY id ASC",
        (agent_id,),
    ).fetchall()


# --- boundary-ticket detection (R14c) -----------------------------------------


def is_boundary_ticket(
    *,
    agent_clusters: frozenset[int] | set[int] | Sequence[int],
    routing_ambiguous: bool,
    params: RouterParams = RouterParams(),
) -> bool:
    """A ticket is cross-cutting iff routing is ambiguous OR its retrieved insights
    span >= ``boundary_min_clusters`` agent clusters (R14c). Rare by construction —
    the AND-free OR keeps the trigger tight, not the default path."""
    return routing_ambiguous or len(set(agent_clusters)) >= params.boundary_min_clusters


# --- boundary-ticket multi-persona refinement (R14c) --------------------------

# Seams (artifact-mediated, no context relay):
#  - draft_fn():            the primary persona drafts the ticket -> artifact str.
#  - refine_fn(agent_id, committed_artifact): a refining persona reads the
#    COMMITTED artifact (and only that) and returns a refined artifact str.
#  - verify_fn(artifact):   the verifier gate -> True iff the artifact is accepted.
#  - progress_key_fn(artifact): canonicalizes an artifact to a no-progress key
#    (default: the artifact itself) — a repeated key fires the §7 tripwire.
DraftFn = Callable[[], str]
RefineFn = Callable[[int, str], str]
VerifyFn = Callable[[str], bool]
ProgressKeyFn = Callable[[str], str]


@dataclass(frozen=True)
class PersonaPass:
    """One pass over the shared artifact (artifact-mediation evidence)."""

    persona_agent_id: int
    is_primary: bool
    artifact_in: str | None  # the COMMITTED artifact this persona read (None for draft)
    artifact_out: str  # what this persona committed
    accepted: bool  # the verifier's verdict after this pass


# Halt reasons.
HALT_SINGLE_PERSONA = "single_persona"  # not a boundary ticket — one pass, done
HALT_VERIFIER_ACCEPTED = "verifier_accepted"
HALT_PASS_CAP = "pass_cap"
HALT_NO_PROGRESS = "no_progress"  # §7 tripwire: a repeated artifact state


@dataclass(frozen=True)
class BoundaryResult:
    """A boundary-ticket refinement's full audit trail (R14c)."""

    artifact: str
    is_boundary: bool
    passes: tuple[PersonaPass, ...]
    cross_pass_count: int
    accepted: bool
    halt_reason: str


def refine_with_personas(
    *,
    is_boundary: bool,
    primary_agent_id: int,
    secondary_agent_ids: Sequence[int],
    draft_fn: DraftFn,
    refine_fn: RefineFn,
    verify_fn: VerifyFn,
    params: RouterParams = RouterParams(),
    progress_key_fn: ProgressKeyFn | None = None,
) -> BoundaryResult:
    """Run the boundary-ticket refinement (R14c).

    A non-boundary ticket runs **exactly one** persona pass (the primary owner) and
    returns — the multi-persona path is never entered (``test_boundary_trigger_is_gated``).
    A boundary ticket runs the primary draft, then at most ``max_cross_passes``
    (1-2) cross-persona refinements; each refining persona reads only the
    **committed** artifact (artifact-mediation; no context relay) and the verifier
    gates after every pass. The loop halts on the first of: verifier acceptance, the
    cross-persona cap, or the §7 no-progress tripwire (a repeated artifact state).
    """
    progress_key = progress_key_fn if progress_key_fn is not None else (lambda a: a)

    # Pass 1: the primary persona owns and drafts the ticket.
    draft = draft_fn()
    accepted = verify_fn(draft)
    passes = [
        PersonaPass(
            persona_agent_id=primary_agent_id,
            is_primary=True,
            artifact_in=None,
            artifact_out=draft,
            accepted=accepted,
        )
    ]

    if not is_boundary:
        # Single-persona path: exactly one pass, no cross-persona refinement.
        return BoundaryResult(
            artifact=draft,
            is_boundary=False,
            passes=tuple(passes),
            cross_pass_count=0,
            accepted=accepted,
            halt_reason=HALT_SINGLE_PERSONA,
        )

    seen_keys = {progress_key(draft)}
    committed = draft
    cross = 0
    halt_reason = HALT_PASS_CAP
    if accepted:
        halt_reason = HALT_VERIFIER_ACCEPTED

    # The other side(s) of the boundary refine the COMMITTED artifact. Hard cap of
    # 1-2 cross-persona passes; the verifier arbitrates; a repeated artifact halts.
    while not accepted and cross < params.max_cross_passes and cross < len(
        secondary_agent_ids
    ):
        persona = secondary_agent_ids[cross]
        # Artifact-mediation: the persona receives ONLY the committed artifact.
        refined = refine_fn(persona, committed)
        accepted = verify_fn(refined)
        cross += 1
        passes.append(
            PersonaPass(
                persona_agent_id=persona,
                is_primary=False,
                artifact_in=committed,
                artifact_out=refined,
                accepted=accepted,
            )
        )
        committed = refined
        key = progress_key(refined)
        if accepted:
            halt_reason = HALT_VERIFIER_ACCEPTED
            break
        if key in seen_keys:
            # §7 no-progress tripwire: the artifact oscillated back to a prior
            # state — halt rather than thrash (anti-MAST).
            halt_reason = HALT_NO_PROGRESS
            break
        seen_keys.add(key)
    else:
        if not accepted:
            halt_reason = HALT_PASS_CAP

    return BoundaryResult(
        artifact=committed,
        is_boundary=True,
        passes=tuple(passes),
        cross_pass_count=cross,
        accepted=accepted,
        halt_reason=halt_reason,
    )
