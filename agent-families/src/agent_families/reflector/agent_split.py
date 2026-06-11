"""Agent split: the family taxonomy self-reorganizes (plan-005 U4, R13, DESIGN §6).

Plan 4 U8 shipped the agent-split **gate as a stub** (``maybe_agent_split`` always
defers, because no Phase-3a writer increments ``agents.routing_decisions``). Plan 5's
router (:mod:`agent_families.library.router`) finally feeds that counter, so this
module builds the real §6 machinery the stub stood in for. It is invoked from the
post-promotion maintenance pass's lineage step; ``maintenance.py`` is untouched
(its stub still honestly defers — this is the activated path the run-assembly calls
once routing data exists).

The §6 mechanics, end to end:

1. **Decision-count gate** — refuse candidacy until ``agents.routing_decisions``
   reaches ``min_routing_decisions`` (the small-N noise guard; clustering and
   replay on a handful of decisions is noise).
2. **Base-prompt residue check** — a non-empty ``base_prompt_specialty`` is
   *residue*: specialist knowledge living in the base prompt instead of skills.
   Splitting would silently strip it from both children, so residue **blocks** the
   split with a conversion warning (convert it to skills first, DESIGN §6).
3. **Clustering over skill descriptions** — k-means (k=2) over the agent's skill
   *description* embeddings (the routing substrate, not insight bodies); accept on
   silhouette ≥ ``silhouette_threshold`` with each child ≥ ``min_cluster_size``.
4. **Compressibility gate** — each child needs a contrastive description that
   compresses to ≤ ``compressibility_max_words`` words (DESIGN §6 "≤25-word
   compressibility gate"); a child that cannot be summarized that tightly is not a
   real specialty. Contrastive sibling descriptions are generated **jointly** (a
   namer seam — ``run_judge`` live, a scripted fake offline).
5. **Transactional lineage** — execute creates two children (``parent_id`` set),
   re-points each skill's ownership to its child, and flips the parent's
   ``lineage_status`` to ``split_pending``. It is **revertible** until the split
   passes both gates: **routing replay ≥ ``replay_agreement_threshold``** against
   the logged decisions, and **one benchmark run**. Only then does ``finalize``
   flip the parent to ``retired`` (a frozen lineage anchor, never deleted); any
   failure ``revert``s to the pre-split state byte-for-byte (routing identical).

Offline by construction: clustering/silhouette is the same deterministic arithmetic
as the skill split (imported from ``maintenance``); the namer, the routing replay,
and the benchmark gate are injected seams (scripted fakes in the suite, live
bindings in run-assembly). Zero quota, no ``claude`` on PATH.

## Conformance (plan-005 U4 split test scenarios -> tests in tests/test_agent_split.py)

- split candidacy refuses below the decision-count gate:
  ``test_split_candidacy_refuses_below_decision_gate``
- two planted skill clusters split with ≥90% replay agreement:
  ``test_planted_clusters_split_with_replay_agreement``
- replay below threshold rejects and preserves the parent:
  ``test_replay_below_threshold_rejects_and_preserves_parent``
- a reverted split restores routing identically:
  ``test_reverted_split_restores_routing_identically``
- base-prompt residue blocks the split with the conversion warning:
  ``test_base_prompt_residue_blocks_split``
- children inherit correct skill partitions including quarantined members:
  ``test_children_inherit_skill_partitions_including_quarantined``
- one fixture split survives the full transaction incl. a benchmark-pass gate:
  ``test_split_survives_full_transaction_with_benchmark_gate``
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable

from agent_families.library import router
from agent_families.reflector.maintenance import _kmeans2, silhouette_two
from agent_families.reflector.stage_a import INSTRUMENT_HEALTH_KIND
from agent_families.store import Store

# --- tunables (caller-supplied, carried defaults; PROVENANCE per DESIGN §17) -----

# PROVENANCE: §6 R2 / R21 — "gates on a min_routing_decisions threshold only Plan 5's
# volume can produce"; clustering/replay on small N is noise. TUNING METRIC: first-split
# revert rate vs decision volume (the first split should be expected to revert once).
DEFAULT_MIN_ROUTING_DECISIONS = 50

# PROVENANCE: §6 agent split "best silhouette > ~0.35". TUNING METRIC: post-split
# routing accuracy vs the agent's skill-description spread.
DEFAULT_SILHOUETTE_THRESHOLD = 0.35

# PROVENANCE: §6 "minimum cluster sizes" — a one-skill child is not a specialty.
# TUNING METRIC: child-agent retire-by-rebalance rate.
DEFAULT_MIN_CLUSTER_SIZE = 2

# PROVENANCE: §6 "≤25-word compressibility gate" — a specialty you cannot summarize
# in a sentence is not a specialty. TUNING METRIC: routing-description hit rate.
DEFAULT_COMPRESSIBILITY_MAX_WORDS = 25

# PROVENANCE: R13 / §6 "routing replay ≥90% agreement". TUNING METRIC: post-split
# misroute rate on held-back decisions.
DEFAULT_REPLAY_AGREEMENT_THRESHOLD = 0.90

# lineage_status values written on the parent (the Plan 4 U1 seam column).
LINEAGE_SPLIT_PENDING = "split_pending"
LINEAGE_RETIRED = "retired"

_WORD_RE = re.compile(r"\S+")


class AgentSplitError(Exception):
    """A broken agent-split precondition with an actionable message."""


@dataclass(frozen=True)
class AgentSplitParams:
    """Caller-supplied agent-split tunables (routed from thresholds.toml live)."""

    min_routing_decisions: int = DEFAULT_MIN_ROUTING_DECISIONS
    silhouette_threshold: float = DEFAULT_SILHOUETTE_THRESHOLD
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE
    compressibility_max_words: int = DEFAULT_COMPRESSIBILITY_MAX_WORDS
    replay_agreement_threshold: float = DEFAULT_REPLAY_AGREEMENT_THRESHOLD

    def __post_init__(self) -> None:
        if self.min_routing_decisions < 0:
            raise AgentSplitError(
                f"min_routing_decisions must be >= 0, got {self.min_routing_decisions}"
            )
        if not (-1.0 <= self.silhouette_threshold <= 1.0):
            raise AgentSplitError(
                f"silhouette_threshold must be in [-1, 1], got"
                f" {self.silhouette_threshold}"
            )
        if self.min_cluster_size < 1:
            raise AgentSplitError(
                f"min_cluster_size must be >= 1, got {self.min_cluster_size}"
            )
        if self.compressibility_max_words < 1:
            raise AgentSplitError(
                "compressibility_max_words must be >= 1, got"
                f" {self.compressibility_max_words}"
            )
        if not (0.0 <= self.replay_agreement_threshold <= 1.0):
            raise AgentSplitError(
                "replay_agreement_threshold must be in [0, 1], got"
                f" {self.replay_agreement_threshold}"
            )


# --- base-prompt residue check (R13, DESIGN §6) ---------------------------------


def check_base_prompt_residue(agent_row) -> str | None:
    """Return a conversion warning if the agent carries base-prompt residue, else None.

    Residue = a non-empty ``base_prompt_specialty``: specialist knowledge encoded in
    the base prompt instead of skills. A split partitions *skills*; residue would be
    silently lost from both children, so the presence of any residue blocks the split
    until it is converted to skills (DESIGN §6).
    """
    specialty = (agent_row["base_prompt_specialty"] or "").strip()
    if not specialty:
        return None
    return (
        "base-prompt residue blocks the split: this agent's base_prompt_specialty"
        f" carries specialist knowledge ({specialty!r:.60}) that a skill-partition"
        " split would strip from both children. Convert it to skills before"
        " splitting (DESIGN §6 base-prompt residue check)."
    )


# --- clustering over skill descriptions (R13, DESIGN §6) ------------------------


def _agent_skill_ids(store: Store, agent_id: int) -> list[int]:
    rows = store.conn.execute(
        "SELECT id FROM skills WHERE agent_id = ? ORDER BY id ASC", (agent_id,)
    ).fetchall()
    return [r["id"] for r in rows]


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


# A namer seam: given the parent agent row + the two skill-id clusters, return
# ``((name0, desc0), (name1, desc1))`` — jointly-generated contrastive sibling
# descriptions (DESIGN §6). Live binding routes ``run_judge``; the suite injects a
# deterministic fake.
NamerFn = Callable[..., tuple[tuple[str, str], tuple[str, str]]]


def _default_namer(
    parent_row, cluster0: Sequence[int], cluster1: Sequence[int]
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Deterministic offline child names/descriptions (the live binding overrides
    with a judge seam). Kept short so the compressibility gate passes by default."""
    base = parent_row["name"]
    return (
        (f"{base}-1", f"{base} specialist (cluster 1)"),
        (f"{base}-2", f"{base} specialist (cluster 2)"),
    )


# --- candidacy (R13) ------------------------------------------------------------


@dataclass(frozen=True)
class SplitCandidacy:
    """The agent-split candidacy decision (R13) — eligible or a refusal reason."""

    agent_id: int
    eligible: bool
    reason: str
    routing_decisions: int
    cluster0_skill_ids: tuple[int, ...]
    cluster1_skill_ids: tuple[int, ...]
    silhouette: float
    child_names: tuple[str, str] | None
    child_descriptions: tuple[str, str] | None
    residue_warning: str | None


def _routing_decisions(store: Store, agent_id: int) -> int:
    row = store.conn.execute(
        "SELECT routing_decisions FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise AgentSplitError(f"agent {agent_id} does not exist")
    return row["routing_decisions"]


def evaluate_split_candidacy(
    store: Store,
    agent_id: int,
    *,
    skill_vectors: dict[int, list[float]],
    params: AgentSplitParams = AgentSplitParams(),
    namer_fn: NamerFn | None = None,
) -> SplitCandidacy:
    """Evaluate whether ``agent_id`` is a §6 split candidate (R13).

    ``skill_vectors`` maps each of the agent's skill ids to its description
    embedding (the run-assembly embeds skill descriptions; the suite injects them).
    Checks run in order — decision gate, residue, cluster/silhouette/sizes,
    compressibility — and the first failure is the refusal reason.
    """
    parent = store.conn.execute(
        "SELECT id, name, base_prompt_specialty FROM agents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    if parent is None:
        raise AgentSplitError(f"agent {agent_id} does not exist")

    decisions = _routing_decisions(store, agent_id)

    def refuse(reason: str, *, residue_warning: str | None = None) -> SplitCandidacy:
        return SplitCandidacy(
            agent_id=agent_id,
            eligible=False,
            reason=reason,
            routing_decisions=decisions,
            cluster0_skill_ids=(),
            cluster1_skill_ids=(),
            silhouette=-1.0,
            child_names=None,
            child_descriptions=None,
            residue_warning=residue_warning,
        )

    # 1. Decision-count gate.
    if decisions < params.min_routing_decisions:
        return refuse(
            f"agent split deferred: routing_decisions={decisions} <"
            f" min_routing_decisions={params.min_routing_decisions} (small-N noise"
            " guard, §6 R2)"
        )

    # 2. Base-prompt residue check.
    residue = check_base_prompt_residue(parent)
    if residue is not None:
        return refuse(residue, residue_warning=residue)

    # 3. Cluster the skill descriptions.
    skill_ids = _agent_skill_ids(store, agent_id)
    if len(skill_ids) < 2 * params.min_cluster_size:
        return refuse(
            f"agent has {len(skill_ids)} skill(s); need >="
            f" {2 * params.min_cluster_size} to form two clusters of"
            f" min_cluster_size={params.min_cluster_size}"
        )
    missing = [sid for sid in skill_ids if sid not in skill_vectors]
    if missing:
        raise AgentSplitError(
            f"skills {missing} have no description vector; cannot cluster"
            " (run-assembly embeds every skill description before candidacy)"
        )
    cluster0, cluster1 = _kmeans2(skill_ids, skill_vectors)
    sil = silhouette_two(cluster0, cluster1, skill_vectors)
    if len(cluster0) < params.min_cluster_size or len(cluster1) < params.min_cluster_size:
        return refuse(
            f"k-means produced clusters of sizes {len(cluster0)}/{len(cluster1)};"
            f" each must be >= min_cluster_size={params.min_cluster_size}"
        )
    if sil < params.silhouette_threshold:
        return refuse(
            f"skill-description silhouette {sil:.3f} <"
            f" silhouette_threshold={params.silhouette_threshold}: the skills do not"
            " separate into two specialties"
        )

    # 4. Compressibility gate on the jointly-generated contrastive descriptions.
    namer = namer_fn if namer_fn is not None else _default_namer
    (name0, desc0), (name1, desc1) = namer(parent, cluster0, cluster1)
    for desc in (desc0, desc1):
        if _word_count(desc) > params.compressibility_max_words:
            return refuse(
                f"a child description is {_word_count(desc)} words >"
                f" compressibility_max_words={params.compressibility_max_words}:"
                " the cluster is not a compressible specialty (§6 ≤25-word gate)"
            )

    return SplitCandidacy(
        agent_id=agent_id,
        eligible=True,
        reason="eligible: gate cleared, residue clean, clusters separate and"
        " compressible",
        routing_decisions=decisions,
        cluster0_skill_ids=tuple(cluster0),
        cluster1_skill_ids=tuple(cluster1),
        silhouette=sil,
        child_names=(name0, name1),
        child_descriptions=(desc0, desc1),
        residue_warning=None,
    )


# --- transactional lineage (R13) ------------------------------------------------


@dataclass(frozen=True)
class AgentSplit:
    """One executed agent split's provenance — revertible until finalized (R13)."""

    parent_agent_id: int
    family_id: int
    child_agent_ids: tuple[int, int]
    child_skill_ids: tuple[tuple[int, ...], tuple[int, ...]]
    silhouette: float
    finalized: bool = False


def execute_split(
    store: Store, candidacy: SplitCandidacy
) -> AgentSplit:
    """Create the two children, re-point skill ownership, mark the parent pending.

    A plain transaction (no snapshot minted — agent/skill-ownership changes do not
    touch insight ``status_transitions``), so :func:`revert_split` is an exact
    inverse. The parent flips to ``split_pending`` and is excluded from routing
    while pending; it is **not** retired until :func:`finalize_split`.
    """
    if not candidacy.eligible:
        raise AgentSplitError(
            f"cannot execute an ineligible split for agent {candidacy.agent_id}:"
            f" {candidacy.reason}"
        )
    parent = store.conn.execute(
        "SELECT id, family_id, lineage_status FROM agents WHERE id = ?",
        (candidacy.agent_id,),
    ).fetchone()
    if parent is None:
        raise AgentSplitError(f"agent {candidacy.agent_id} does not exist")
    if parent["lineage_status"] in (LINEAGE_SPLIT_PENDING, LINEAGE_RETIRED):
        raise AgentSplitError(
            f"agent {candidacy.agent_id} is already {parent['lineage_status']};"
            " cannot split it again"
        )
    family_id = parent["family_id"]
    (name0, name1) = candidacy.child_names  # type: ignore[misc]
    (desc0, desc1) = candidacy.child_descriptions  # type: ignore[misc]

    with store.transaction():
        child0 = store.create_agent(
            family_id, name0, description=desc0, parent_id=candidacy.agent_id
        )
        child1 = store.create_agent(
            family_id, name1, description=desc1, parent_id=candidacy.agent_id
        )
        for sid in candidacy.cluster0_skill_ids:
            store.conn.execute(
                "UPDATE skills SET agent_id = ? WHERE id = ?", (child0, sid)
            )
        for sid in candidacy.cluster1_skill_ids:
            store.conn.execute(
                "UPDATE skills SET agent_id = ? WHERE id = ?", (child1, sid)
            )
        store.conn.execute(
            "UPDATE agents SET lineage_status = ? WHERE id = ?",
            (LINEAGE_SPLIT_PENDING, candidacy.agent_id),
        )

    return AgentSplit(
        parent_agent_id=candidacy.agent_id,
        family_id=family_id,
        child_agent_ids=(child0, child1),
        child_skill_ids=(candidacy.cluster0_skill_ids, candidacy.cluster1_skill_ids),
        silhouette=candidacy.silhouette,
    )


def revert_split(store: Store, split: AgentSplit) -> None:
    """Undo a pending split: skills back to the parent, children gone, parent active.

    The exact inverse of :func:`execute_split` — leaves routing identical to the
    pre-split state. Refuses once the split is finalized (a retired parent is a
    frozen anchor).
    """
    parent = store.conn.execute(
        "SELECT lineage_status FROM agents WHERE id = ?", (split.parent_agent_id,)
    ).fetchone()
    if parent is None:
        raise AgentSplitError(f"agent {split.parent_agent_id} does not exist")
    if parent["lineage_status"] == LINEAGE_RETIRED:
        raise AgentSplitError(
            f"agent {split.parent_agent_id} is retired by a finalized split;"
            " a finalized split is not revertible (R13)"
        )
    with store.transaction():
        for child_id in split.child_agent_ids:
            store.conn.execute(
                "UPDATE skills SET agent_id = ? WHERE agent_id = ?",
                (split.parent_agent_id, child_id),
            )
            store.conn.execute("DELETE FROM agents WHERE id = ?", (child_id,))
        store.conn.execute(
            "UPDATE agents SET lineage_status = NULL WHERE id = ?",
            (split.parent_agent_id,),
        )


def finalize_split(store: Store, split: AgentSplit) -> AgentSplit:
    """Commit a split that passed replay + benchmark: retire the parent anchor.

    The parent flips to ``retired`` (excluded from routing forever, never deleted —
    a frozen lineage anchor); the children become permanent. After this the split
    is no longer revertible.
    """
    with store.transaction():
        store.conn.execute(
            "UPDATE agents SET lineage_status = ? WHERE id = ?",
            (LINEAGE_RETIRED, split.parent_agent_id),
        )
    return AgentSplit(
        parent_agent_id=split.parent_agent_id,
        family_id=split.family_id,
        child_agent_ids=split.child_agent_ids,
        child_skill_ids=split.child_skill_ids,
        silhouette=split.silhouette,
        finalized=True,
    )


# --- routing replay (R13) -------------------------------------------------------

RouteRequestFn = Callable[..., int]


def replay_agreement(
    store: Store,
    split: AgentSplit,
    *,
    judge_fn=None,
    route_request_fn: RouteRequestFn | None = None,
    judge_model: str = router.DEFAULT_ROUTER_MODEL,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
) -> float:
    """Fraction of the parent's logged decisions that re-route into its children (R13).

    Re-routes each historical request the parent was chosen for over the **new**
    candidate set (the children + untouched siblings; the pending parent is already
    excluded from :func:`router.family_candidates`). Agreement = the share that land
    on one of this split's children — a faithful split keeps the parent's traffic in
    its lineage; a bad split leaks it to siblings. Returns 0.0 when the parent has no
    logged decisions (the candidacy gate prevents that in practice).
    """
    decisions = router.logged_decisions(
        store, chosen_agent_id=split.parent_agent_id
    )
    if not decisions:
        return 0.0
    candidates = router.family_candidates(store, split.family_id)
    children = set(split.child_agent_ids)
    routed = route_request_fn or (
        lambda request_text: router.route_request_over_candidates(
            request_text,
            candidates,
            judge_fn=judge_fn,
            judge_model=judge_model,
            judge_mode=judge_mode,
            judge_fixtures_dir=judge_fixtures_dir,
        )
    )
    hits = 0
    for row in decisions:
        chosen = routed(row["request_text"])
        if chosen in children:
            hits += 1
    return hits / len(decisions)


# --- the full split transaction (R13 verification) ------------------------------

BenchmarkFn = Callable[[AgentSplit], bool]


@dataclass(frozen=True)
class SplitOutcome:
    """The result of a full split attempt (candidacy -> execute -> gates)."""

    candidacy: SplitCandidacy
    split: AgentSplit | None
    committed: bool
    replay_score: float | None
    benchmark_passed: bool | None
    reason: str


def perform_agent_split(
    store: Store,
    agent_id: int,
    *,
    skill_vectors: dict[int, list[float]],
    params: AgentSplitParams = AgentSplitParams(),
    namer_fn: NamerFn | None = None,
    judge_fn=None,
    route_request_fn: RouteRequestFn | None = None,
    benchmark_fn: BenchmarkFn,
    judge_model: str = router.DEFAULT_ROUTER_MODEL,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
) -> SplitOutcome:
    """Run the whole §6 split transaction with both gates (R13 verification).

    candidacy -> execute (pending) -> routing-replay gate -> benchmark gate ->
    finalize, reverting to the exact pre-split state on either gate's failure. The
    split is committed (parent retired, children permanent) only when replay
    agreement clears the threshold **and** one benchmark run passes.
    """
    candidacy = evaluate_split_candidacy(
        store, agent_id, skill_vectors=skill_vectors, params=params, namer_fn=namer_fn
    )
    if not candidacy.eligible:
        return SplitOutcome(
            candidacy=candidacy,
            split=None,
            committed=False,
            replay_score=None,
            benchmark_passed=None,
            reason=candidacy.reason,
        )

    split = execute_split(store, candidacy)
    replay = replay_agreement(
        store,
        split,
        judge_fn=judge_fn,
        route_request_fn=route_request_fn,
        judge_model=judge_model,
        judge_mode=judge_mode,
        judge_fixtures_dir=judge_fixtures_dir,
    )
    if replay < params.replay_agreement_threshold:
        revert_split(store, split)
        return SplitOutcome(
            candidacy=candidacy,
            split=None,
            committed=False,
            replay_score=replay,
            benchmark_passed=None,
            reason=(
                f"routing replay {replay:.3f} <"
                f" {params.replay_agreement_threshold}: split reverted, parent"
                " preserved (R13)"
            ),
        )

    benchmark_passed = bool(benchmark_fn(split))
    if not benchmark_passed:
        revert_split(store, split)
        return SplitOutcome(
            candidacy=candidacy,
            split=None,
            committed=False,
            replay_score=replay,
            benchmark_passed=False,
            reason="benchmark run failed: split reverted, parent preserved (R13)",
        )

    finalized = finalize_split(store, split)
    return SplitOutcome(
        candidacy=candidacy,
        split=finalized,
        committed=True,
        replay_score=replay,
        benchmark_passed=True,
        reason="split committed: replay + benchmark gates passed, parent retired",
    )


# --- R14: the explorer-family decision point (documented, NOT automatic) --------
#
# DESIGN §6 / R14: agent families form through the *normal* taxonomy once their
# library carries idea-shaped volume. The worker family gets that volume from
# episodes; the explorer and grader families do NOT have a library family in Phase
# 3a — their attributions sink to Plan 4's instrument-health records
# (``review_queue.kind = 'instrument_health'``) instead of becoming insights. R14
# is therefore a **documented decision point, not an automatic**: a human reads the
# evidence below and decides whether explorer/grader lessons now recur often enough
# to seed those families through the same taxonomy this module splits. Until then,
# the sink remains. :func:`explorer_family_decision_evidence` is exactly that
# evidence query — it never seeds a family on its own.


@dataclass(frozen=True)
class ExplorerFamilyEvidence:
    """The R14 evidence: instrument-health volume, broken down by role/aspect.

    ``idea_shaped_by_role`` counts the recurring explorer/grader prompt/answer
    lessons; ``recommend_seed`` is advisory only (the seed is a human decision).
    """

    total_records: int
    by_role: dict[str, int]
    by_role_aspect: dict[str, int]  # "role|aspect" -> count (the recurrence signal)
    recommend_seed: bool
    reason: str


def explorer_family_decision_evidence(
    store: Store, *, min_idea_shaped_volume: int = 20
) -> ExplorerFamilyEvidence:
    """The R14 evidence query: how much idea-shaped explorer/grader volume exists.

    Reads Plan 4's instrument-health sink (``review_queue.kind =
    'instrument_health'``), grouping by the primary attribution's role and
    (role, aspect) — a high, *recurring* (role, aspect) count is the "explorer
    prompt/answer lessons recurring" signal R14 names. ``recommend_seed`` flips
    once any single (role, aspect) recurs at least ``min_idea_shaped_volume``
    times, but it is **advisory** — seeding a family is a documented human decision,
    never executed here.
    """
    rows = store.conn.execute(
        "SELECT payload_json FROM review_queue WHERE kind = ?",
        (INSTRUMENT_HEALTH_KIND,),
    ).fetchall()
    by_role: dict[str, int] = {}
    by_role_aspect: dict[str, int] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        primary = payload.get("primary", {}) if isinstance(payload, dict) else {}
        role = str(primary.get("role", "unknown"))
        aspect = str(primary.get("aspect", "unknown"))
        by_role[role] = by_role.get(role, 0) + 1
        key = f"{role}|{aspect}"
        by_role_aspect[key] = by_role_aspect.get(key, 0) + 1

    peak = max(by_role_aspect.values(), default=0)
    recommend = peak >= min_idea_shaped_volume
    reason = (
        f"a (role, aspect) lesson recurs {peak} >= {min_idea_shaped_volume} times:"
        " explorer/grader volume looks idea-shaped — a human may seed the family"
        " through the normal taxonomy (R14 documented decision)"
        if recommend
        else (
            f"peak (role, aspect) recurrence {peak} < {min_idea_shaped_volume}: the"
            " instrument-health sink remains (R14 — no family seeded automatically)"
        )
    )
    return ExplorerFamilyEvidence(
        total_records=len(rows),
        by_role=by_role,
        by_role_aspect=by_role_aspect,
        recommend_seed=recommend,
        reason=reason,
    )
