"""The family router: per-request specialist selection + the decision log (plan-005 U4, R12).

**DEMOTED to dead code (plan-009 U7, R14).** The R3 reform replaces per-family routing
with whole-store insight-level retrieval (:mod:`agent_families.library.retrieval`) and
derives group structure rather than splitting agents by routing volume. This module's
sole production caller was ``agent_split.replay_agreement``, removed when the family
split engine was gutted (R15), so **no production code calls ``router.route`` anymore**.
The file and the ``routing_decisions`` table are deliberately retained (not deleted)
for reversibility — the §6 router can be re-activated without a migration. Everything
below is the original Plan 5 implementation, kept intact for that reason.

The router is the §6 mechanism that picks which specialist in a family handles a
request, **and** it is the routing-decision logger. Splitting was deliberately
starved until this data exists (DESIGN §6): every routing decision is appended to
the routing-decision log (request, candidate set, choice, confidence) so that

- ``agents.routing_decisions`` accumulates the volume the ``min_routing_decisions``
  agent-split gate reads (Plan 4 U8's lineage seam), and
- the §6 **routing replay** has the historical (request -> choice) pairs it re-runs
  against a proposed split's children (R13, :mod:`agent_families.reflector.agent_split`).

A route is a judge-seam call over the family's agent descriptions: the closed
schema forces the model to pick one candidate name + a confidence, riding the same
record/replay seam as every other LLM call (a scripted fake in the offline suite,
``run_judge`` live — zero quota, no ``claude`` on PATH). In Phase 3a a family has
exactly one generic agent, so a route trivially selects it (confidence 1.0, no
judge call) yet **still logs and increments** — that is how the lone generic agent
accumulates the routing decisions that eventually make it split-eligible.

## Decision-log ownership (smallest faithful adaptation)

U4's Files list is ``router.py`` / ``agent_split.py`` / ``retrieval.py`` (+ tests)
— it does **not** include ``store.py``, and Plan 5 has no schema-migration unit
(the lineage seam columns were pre-added in migration v4 precisely so this plan
adds no migration). The routing-decision log is therefore owned **here**: an
idempotent ``CREATE TABLE IF NOT EXISTS routing_decisions`` ensured on first use,
not a formal migration. ``test_store``'s table-set assertion is a subset check
(``EXPECTED_TABLES <= tables``), so the extra table is compatible; ``migrate()``
never recreates it.

## Conformance (plan-005 U4 router test scenarios -> tests in tests/test_router.py)

- routing decisions logged with full candidates: ``test_routing_decision_is_logged_with_candidates``
- the chosen agent's routing_decisions counter increments (feeds the split gate):
  ``test_route_increments_chosen_agent_counter``
- a lone generic agent routes-to-self and still accrues decisions:
  ``test_single_candidate_routes_to_self_and_logs``
- low confidence flags the decision ambiguous (the R14c boundary trigger):
  ``test_low_confidence_decision_is_ambiguous``
- replay re-routes a logged request over a new candidate set:
  ``test_route_request_over_candidates_is_deterministic_seam``
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from agent_families.judge import run_judge
from agent_families.store import Store

# PROVENANCE: §6 router "an LLM routing call over the family's agent descriptions";
# §15 model tiers — volume routing on Sonnet. Carried seam default; run-assembly
# routes the live value from thresholds.toml [judge].model.
DEFAULT_ROUTER_MODEL = "sonnet"

# PROVENANCE: DESIGN §4 "Boundary tickets" / R14c — a route whose confidence is
# below this floor is *ambiguous*, one of the two boundary-ticket triggers. Carried
# seam default; not a hot-path tunable (callers pass it explicitly).
# TUNING METRIC: boundary-ticket precision vs the multi-persona refinement cost.
DEFAULT_AMBIGUITY_CONFIDENCE_THRESHOLD = 0.6


class RouterError(Exception):
    """Router misuse or a malformed routing decision, with an actionable message."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- the routing-decision log (R12) ---------------------------------------------


def ensure_routing_log(store: Store) -> None:
    """Create the routing-decision log table if absent (idempotent).

    Owned here, not in a store migration (see the module docstring). The schema
    carries everything routing replay needs: the request text, the candidate set
    at decision time, the choice, the confidence, and the library snapshot.
    """
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS routing_decisions ("
        "  id              INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  family_id       INTEGER NOT NULL,"
        "  request_hash    TEXT NOT NULL,"
        "  request_text    TEXT NOT NULL,"
        "  candidates_json TEXT NOT NULL,"
        "  chosen_agent_id INTEGER NOT NULL,"
        "  confidence      REAL NOT NULL,"
        "  ambiguous       INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),"
        "  snapshot_id     INTEGER NOT NULL,"
        "  created_at      TEXT NOT NULL)"
    )
    store.conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_routing_decisions_family"
        " ON routing_decisions(family_id)"
    )
    store.conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_routing_decisions_chosen"
        " ON routing_decisions(chosen_agent_id)"
    )


# --- candidate + decision shapes -------------------------------------------------


@dataclass(frozen=True)
class RoutingCandidate:
    """One routable specialist: its agent id, name, and routing description."""

    agent_id: int
    name: str
    description: str


@dataclass(frozen=True)
class RoutingDecision:
    """One logged routing decision (R12) — the full audit row plus the choice."""

    decision_id: int | None
    family_id: int
    request_text: str
    request_hash: str
    candidates: tuple[RoutingCandidate, ...]
    chosen_agent_id: int
    confidence: float
    ambiguous: bool
    snapshot_id: int


def request_hash(request_text: str, candidate_names: list[str]) -> str:
    """Stable identity for a routing request: sha256 over (text, sorted names).

    Carries no volatile data (no timestamps / floats) so a re-routed request
    hashes identically — what routing replay keys on.
    """
    canonical = json.dumps(
        {"request": request_text, "candidates": sorted(candidate_names)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- the candidate pool ----------------------------------------------------------


"""Lineage statuses that exclude an agent from routing: a split's parent is
unroutable both while the split is ``split_pending`` (its children handle traffic
during the revertible window) and after it is ``retired`` (a frozen anchor)."""
_UNROUTABLE_LINEAGE = ("split_pending", "retired")


def family_candidates(store: Store, family_id: int) -> list[RoutingCandidate]:
    """The routable specialists in a family: agents not being/already split away.

    A split's parent flips ``lineage_status`` to ``split_pending`` then ``retired``
    (see :mod:`agent_families.reflector.agent_split`); its children become the new
    candidates. Everything else (NULL / ``active``) routes.
    """
    rows = store.conn.execute(
        "SELECT id, name, description, lineage_status FROM agents"
        " WHERE family_id = ? ORDER BY id ASC",
        (family_id,),
    ).fetchall()
    return [
        RoutingCandidate(r["id"], r["name"], r["description"])
        for r in rows
        if r["lineage_status"] not in _UNROUTABLE_LINEAGE
    ]


# --- the routing judge contract --------------------------------------------------


def routing_schema(candidate_names: list[str]) -> dict:
    """Closed routing schema: choose exactly one candidate name + a confidence."""
    return {
        "type": "object",
        "properties": {
            "chosen_agent": {"type": "string", "enum": list(candidate_names)},
            "confidence": {"type": "number"},
        },
        "required": ["chosen_agent", "confidence"],
        "additionalProperties": False,
    }


def build_routing_prompt(
    family_row, candidates: list[RoutingCandidate], request_text: str
) -> str:
    """Deterministic routing prompt over the family's agent descriptions.

    Carries no volatile data so the request hash is record/replay-stable (R23).
    """
    roster = json.dumps(
        [{"name": c.name, "description": c.description} for c in candidates],
        sort_keys=True,
        ensure_ascii=False,
    )
    charter = ""
    if family_row is not None:
        charter = family_row["charter"] if "charter" in family_row.keys() else ""
    return (
        "You are the family router. Given the request below and the roster of"
        " specialist agents (name + description), choose the SINGLE best-fit"
        " specialist to handle this request. If two specialists fit nearly"
        " equally, pick one but lower your confidence — a low confidence is a"
        " signal the request straddles a boundary.\n\n"
        f"## Family charter\n{charter}\n\n"
        f"## Specialists\n{roster}\n\n"
        f"## Request\n{request_text}\n\n"
        "Return structured output only: the chosen specialist's exact name and"
        " your confidence in [0, 1]."
    )


JudgeFn = Callable[..., object]


# --- the route entry point (R12) -------------------------------------------------


def route(
    store: Store,
    *,
    family_id: int,
    request_text: str,
    judge_fn: JudgeFn | None = None,
    judge_model: str = DEFAULT_ROUTER_MODEL,
    judge_max_retries: int = 0,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
    ambiguity_confidence_threshold: float = DEFAULT_AMBIGUITY_CONFIDENCE_THRESHOLD,
    snapshot_id: int | None = None,
    log: bool = True,
) -> RoutingDecision:
    """Route one request to a family specialist and log the decision (R12).

    With a single candidate (the Phase-3a lone generic agent) the route is
    trivial — that agent, confidence 1.0, no judge call — but it still logs and
    increments, so the agent accrues the routing decisions its split gate reads.
    With multiple candidates a judge-seam call over the descriptions picks one;
    a confidence below ``ambiguity_confidence_threshold`` flags the decision
    ambiguous (a §4 boundary-ticket trigger, R14c).
    """
    ensure_routing_log(store)
    candidates = family_candidates(store, family_id)
    if not candidates:
        raise RouterError(
            f"family {family_id} has no routable agents; nothing to route to"
        )
    snap = store.current_snapshot_id() if snapshot_id is None else snapshot_id
    names = [c.name for c in candidates]
    by_name = {c.name: c for c in candidates}

    if len(candidates) == 1:
        chosen = candidates[0]
        confidence = 1.0
        ambiguous = False
    else:
        family_row = store.conn.execute(
            "SELECT * FROM families WHERE id = ?", (family_id,)
        ).fetchone()
        judge = judge_fn if judge_fn is not None else run_judge
        result = judge(
            build_routing_prompt(family_row, candidates, request_text),
            routing_schema(names),
            judge_model,
            max_retries=judge_max_retries,
            mode=judge_mode,
            fixtures_dir=judge_fixtures_dir,
        )
        output = result.output
        chosen_name = output.get("chosen_agent")
        if chosen_name not in by_name:
            raise RouterError(
                f"router chose {chosen_name!r}, not one of the candidate names"
                f" {names} (the schema enum should have prevented this)"
            )
        chosen = by_name[chosen_name]
        confidence = float(output.get("confidence", 0.0))
        ambiguous = confidence < ambiguity_confidence_threshold

    rhash = request_hash(request_text, names)
    decision_id: int | None = None
    if log:
        candidates_json = json.dumps(
            [{"agent_id": c.agent_id, "name": c.name} for c in candidates],
            sort_keys=True,
            ensure_ascii=False,
        )
        with store.transaction():
            cur = store.conn.execute(
                "INSERT INTO routing_decisions (family_id, request_hash,"
                " request_text, candidates_json, chosen_agent_id, confidence,"
                " ambiguous, snapshot_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (family_id, rhash, request_text, candidates_json, chosen.agent_id,
                 confidence, 1 if ambiguous else 0, int(snap), _utcnow()),
            )
            decision_id = cur.lastrowid
            store.conn.execute(
                "UPDATE agents SET routing_decisions = routing_decisions + 1"
                " WHERE id = ?",
                (chosen.agent_id,),
            )

    return RoutingDecision(
        decision_id=decision_id,
        family_id=family_id,
        request_text=request_text,
        request_hash=rhash,
        candidates=tuple(candidates),
        chosen_agent_id=chosen.agent_id,
        confidence=confidence,
        ambiguous=ambiguous,
        snapshot_id=int(snap),
    )


# --- reading the log (the split's replay substrate, R13) -------------------------


def logged_decisions(
    store: Store, *, family_id: int | None = None, chosen_agent_id: int | None = None
) -> list:
    """The logged routing decisions, optionally filtered by family / chosen agent.

    Routing replay (R13) reads the decisions a splitting agent was chosen for and
    re-routes their requests against the proposed children.
    """
    ensure_routing_log(store)
    clauses: list[str] = []
    params: list[object] = []
    if family_id is not None:
        clauses.append("family_id = ?")
        params.append(family_id)
    if chosen_agent_id is not None:
        clauses.append("chosen_agent_id = ?")
        params.append(chosen_agent_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return store.conn.execute(
        f"SELECT * FROM routing_decisions{where} ORDER BY id ASC", params
    ).fetchall()


def routing_decision_count(store: Store, agent_id: int) -> int:
    """The agent's accumulated routing-decision count (the split gate's input)."""
    row = store.conn.execute(
        "SELECT routing_decisions FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise RouterError(f"agent {agent_id} does not exist")
    return row["routing_decisions"]


def route_request_over_candidates(
    request_text: str,
    candidates: list[RoutingCandidate],
    *,
    judge_fn: JudgeFn | None = None,
    judge_model: str = DEFAULT_ROUTER_MODEL,
    judge_max_retries: int = 0,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
) -> int:
    """Route a request over an explicit candidate set, returning the chosen agent id.

    The pure, log-free routing primitive that **routing replay** drives (R13): it
    re-runs the judge seam over a *proposed* candidate set (a split's children +
    the untouched siblings) for a historical request and reports the choice, never
    touching the decision log or any counter.
    """
    if not candidates:
        raise RouterError("cannot route over an empty candidate set")
    if len(candidates) == 1:
        return candidates[0].agent_id
    names = [c.name for c in candidates]
    by_name = {c.name: c for c in candidates}
    judge = judge_fn if judge_fn is not None else run_judge
    result = judge(
        build_routing_prompt(None, candidates, request_text),
        routing_schema(names),
        judge_model,
        max_retries=judge_max_retries,
        mode=judge_mode,
        fixtures_dir=judge_fixtures_dir,
    )
    chosen_name = result.output.get("chosen_agent")
    if chosen_name not in by_name:
        raise RouterError(
            f"replay router chose {chosen_name!r}, not one of {names}"
        )
    return by_name[chosen_name].agent_id
