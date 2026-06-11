"""Agent split: the taxonomy self-reorganizes one level up (plan-005 U4, R13/R14).

Plan 4 U8 split *skills*; this splits *agents* — the design's most novel mechanism
(DESIGN §6), finally fed by the routing-decision log Plan 5's router produces. The
§6 mechanics, end to end:

- **Candidacy** gates on ``min_routing_decisions`` (the small-N guard — clustering
  on a handful of routing decisions is noise) AND on having enough skills to form
  two minimum-size clusters.
- **Silhouette over skill descriptions** — k-means (k=2) over the agent's skills'
  *description* embeddings; accept only when the partition's silhouette clears
  ``silhouette_threshold`` (~0.35, §6 agent-split) and **both** clusters meet
  ``min_cluster_skills``.
- **Joint contrastive descriptions** — the two children's names/descriptions/base
  prompts are generated *jointly* (a judge seam) so they contrast; each description
  must pass the **<=25-word compressibility gate** (§6) or the split is blocked.
- **Base-prompt residue check** — before commit, a child base prompt that still
  carries the parent's generic residue (verbatim parent prompt or an unfilled
  marker) blocks the split with a conversion warning (§6 "base-prompt residue
  check before commit").
- **Routing replay** — the candidate split is re-scored against the **logged**
  routing decisions: each decision's request is re-routed to the nearest child
  centroid and compared to the child that owns the skill that actually served it;
  agreement must clear ``replay_agreement_threshold`` (~0.90, §6) or the split is
  rejected and the parent preserved.
- **Transactional lineage, revertible** — children are minted with ``parent_id``
  set and ``lineage_status='split_pending'``; the parent's skills are re-pointed to
  the children and the parent is marked ``retired_by_split``. The split stays
  **revertible until routing replay + one benchmark run pass**; a revert restores
  routing identically (skills re-pointed back, pending children removed). Confirm
  flips the children live; the parent is never deleted (a frozen lineage anchor).

Plus **R14 — the explorer-family decision point**: a *documented* decision (not an
automatic). :func:`explorer_family_decision` runs the evidence query (recurring
explorer-shaped lessons) and returns seed-or-sink with its reason; with no
idea-shaped volume the sink remains (the Phase 3a reality).

Offline by construction: embeddings come through a caller-supplied seam
(``EmbeddingService.embed_query`` live; a fake in the suite), clustering/silhouette
is pure deterministic arithmetic (reused from the skill-split machinery), and the
contrastive-describer call goes through the judge seam. Zero quota, no ``claude`` on
PATH. Tunables ride in :class:`AgentSplitParams` (carried defaults record
PROVENANCE in code; NOT added to ``thresholds.toml`` — its loader is closed).

## Conformance

§6 / R13 test-scenario -> test (in ``tests/test_agent_split.py``):

- split candidacy refuses below the decision-count gate ->
  ``test_split_candidacy_gated_by_decision_count``
- a two-planted-cluster agent splits with >=90% replay agreement ->
  ``test_two_cluster_agent_splits_with_replay_agreement``
- replay below threshold rejects and preserves the parent ->
  ``test_low_replay_agreement_rejects_and_preserves_parent``
- a reverted split restores routing identically -> ``test_reverted_split_restores_routing``
- residue in a base prompt blocks the split with the conversion warning ->
  ``test_base_prompt_residue_blocks_split``
- children inherit correct skill partitions including borderline members ->
  ``test_children_inherit_skill_partitions``
- a fixture split survives the full transaction incl. a benchmark-pass gate ->
  ``test_split_confirmed_after_benchmark_pass``
- the <=25-word compressibility gate blocks a bloated description ->
  ``test_compressibility_gate_blocks_bloated_description``
- low silhouette / undersized clusters reject -> ``test_low_silhouette_rejects``,
  ``test_undersized_cluster_rejects``
- R14 explorer-family decision is documented seed-or-sink ->
  ``test_explorer_family_decision_documents_sink`` / ``..._seeds_on_volume``
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from agent_families.library.router import (
    ROUTING_LOG_TABLE,
    ensure_routing_log,
    family_candidates,
)
from agent_families.reflector.maintenance import (
    _centroid,
    _euclid,
    _kmeans2,
    silhouette_two,
)
from agent_families.store import Store

LINEAGE_SPLIT_PENDING = "split_pending"
LINEAGE_RETIRED_BY_SPLIT = "retired_by_split"

# Seams. ``embed_fn`` embeds one text -> vector (skill descriptions and, where the
# routing log lacks a stored vector, request text). ``describer_fn`` returns the two
# children's contrastive specs jointly. Live bindings route EmbeddingService /
# run_judge; the suite injects deterministic fakes.
EmbedFn = Callable[[str], list[float]]
# describer_fn(parent_row, cluster0_skill_names, cluster1_skill_names) ->
#   ((name0, description0, base_prompt0), (name1, description1, base_prompt1))
DescriberFn = Callable[..., tuple[tuple[str, str, str], tuple[str, str, str]]]
# benchmark_fn() -> True iff one post-split benchmark run passes (the confirm gate).
BenchmarkFn = Callable[[], bool]


class AgentSplitError(Exception):
    """Agent-split misuse or invariant breach with an actionable message."""


# --- tunables (caller-supplied, carried defaults; PROVENANCE per DESIGN §17) ----


@dataclass(frozen=True)
class AgentSplitParams:
    """Caller-supplied agent-split tunables (run-assembly routes live values;
    never read from ``thresholds.toml`` here — its loader is closed)."""

    # PROVENANCE: §6 / R21 — agent split "gates on a min_routing_decisions
    # threshold only Plan 5's volume can produce" (~low hundreds). TUNING METRIC:
    # first-split revert rate vs decision volume.
    min_routing_decisions: int = 50
    # PROVENANCE: §6 agent split "best silhouette > ~0.35". TUNING METRIC: split-
    # acceptance precision on hand-labeled separable agents.
    silhouette_threshold: float = 0.35
    # PROVENANCE: §6 "minimum cluster sizes" — a child smaller than this is not a
    # specialist worth minting. TUNING METRIC: post-split routing accuracy.
    min_cluster_skills: int = 2
    # PROVENANCE: §6 "<=25-word compressibility gate" — a child description that
    # will not compress to a routable line is a sign the cluster is not coherent.
    # TUNING METRIC: routing-selection accuracy vs description length.
    max_description_words: int = 25
    # PROVENANCE: §6 "routing replay >=90% agreement". TUNING METRIC: replay-
    # agreement vs post-split regression rate.
    replay_agreement_threshold: float = 0.90

    def __post_init__(self) -> None:
        if self.min_routing_decisions < 0:
            raise AgentSplitError(
                f"min_routing_decisions must be >= 0, got {self.min_routing_decisions}"
            )
        if not (-1.0 <= self.silhouette_threshold <= 1.0):
            raise AgentSplitError(
                f"silhouette_threshold must be in [-1, 1], got {self.silhouette_threshold}"
            )
        if self.min_cluster_skills < 1:
            raise AgentSplitError(
                f"min_cluster_skills must be >= 1, got {self.min_cluster_skills}"
            )
        if self.max_description_words < 1:
            raise AgentSplitError(
                f"max_description_words must be >= 1, got {self.max_description_words}"
            )
        if not (0.0 <= self.replay_agreement_threshold <= 1.0):
            raise AgentSplitError(
                "replay_agreement_threshold must be in [0.0, 1.0], got"
                f" {self.replay_agreement_threshold}"
            )


# --- skills + their description vectors ----------------------------------------


@dataclass(frozen=True)
class _SkillRow:
    skill_id: int
    name: str
    description: str


def _agent_skills(store: Store, agent_id: int) -> list[_SkillRow]:
    rows = store.conn.execute(
        "SELECT id, name, description FROM skills WHERE agent_id = ? ORDER BY id ASC",
        (agent_id,),
    ).fetchall()
    return [_SkillRow(r["id"], r["name"], r["description"]) for r in rows]


def _skill_vectors(skills: Sequence[_SkillRow], embed_fn: EmbedFn) -> dict[int, list[float]]:
    return {s.skill_id: list(embed_fn(s.description)) for s in skills}


def _word_count(text: str) -> int:
    return len(text.split())


# --- candidacy (§6 gate) -------------------------------------------------------


def agent_routing_decisions(store: Store, agent_id: int) -> int:
    """The agent's logged routing-decision count (the §6 split gate's volume)."""
    row = store.conn.execute(
        "SELECT routing_decisions FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise AgentSplitError(f"agent {agent_id} does not exist")
    return row["routing_decisions"]


def agent_split_candidates(
    store: Store, family_id: int, params: AgentSplitParams
) -> list[int]:
    """Agents in ``family_id`` that pass the candidacy gate (§6): enough routing
    decisions AND enough skills to form two minimum-size clusters. Retired-by-split
    parents (frozen anchors) are never candidates."""
    rows = store.conn.execute(
        "SELECT id FROM agents WHERE family_id = ? AND"
        " (lineage_status IS NULL OR lineage_status != ?) ORDER BY id ASC",
        (family_id, LINEAGE_RETIRED_BY_SPLIT),
    ).fetchall()
    out: list[int] = []
    need = 2 * params.min_cluster_skills
    for r in rows:
        if agent_routing_decisions(store, r["id"]) < params.min_routing_decisions:
            continue
        if len(_agent_skills(store, r["id"])) < need:
            continue
        out.append(r["id"])
    return out


# --- planning: cluster + describe + the gates (§6) -----------------------------


@dataclass(frozen=True)
class AgentSplitPlan:
    """A planned (not yet executed) agent split (§6)."""

    agent_id: int
    accepted: bool
    reason: str
    silhouette: float = -1.0
    cluster0_skill_ids: tuple[int, ...] = ()
    cluster1_skill_ids: tuple[int, ...] = ()
    # ((name, description, base_prompt), (name, description, base_prompt))
    child_specs: tuple[tuple[str, str, str], tuple[str, str, str]] | None = None


def _default_describer(
    parent_row, names0: Sequence[str], names1: Sequence[str]
) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    """Deterministic offline contrastive describer — the live binding overrides
    with a judge seam (§6 joint bottom-up regeneration). Kept terse so the
    <=25-word compressibility gate is satisfied by construction, and specialized
    (parent base prompt NOT copied) so the residue check passes."""
    base = parent_row["name"]
    return (
        (
            f"{base}-a",
            f"Specialist for {', '.join(names0[:3])}.",
            f"You specialize in {names0[0] if names0 else base} concerns.",
        ),
        (
            f"{base}-b",
            f"Specialist for {', '.join(names1[:3])}.",
            f"You specialize in {names1[0] if names1 else base} concerns.",
        ),
    )


def _has_base_prompt_residue(parent_base: str, child_base: str) -> bool:
    """True iff the child base prompt still carries the parent's generic residue:
    the parent's base prompt verbatim, or an unfilled conversion marker (§6)."""
    child = child_base.strip()
    if not child:
        return True
    if parent_base.strip() and parent_base.strip() in child:
        return True
    markers = ("TODO", "FIXME", "<residue>", "{{", "PLACEHOLDER")
    return any(marker in child for marker in markers)


def plan_agent_split(
    store: Store,
    agent_id: int,
    *,
    embed_fn: EmbedFn,
    params: AgentSplitParams = AgentSplitParams(),
    describer_fn: DescriberFn | None = None,
) -> AgentSplitPlan:
    """Plan a split: cluster the agent's skills by description, gate, and describe.

    Pure + seam-driven — no DB writes. Returns ``accepted=False`` with a reason on
    any gate failure (silhouette, cluster size, compressibility, residue)."""
    parent = store.conn.execute(
        "SELECT id, name, base_prompt_specialty FROM agents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    if parent is None:
        raise AgentSplitError(f"agent {agent_id} does not exist")

    skills = _agent_skills(store, agent_id)
    need = 2 * params.min_cluster_skills
    if len(skills) < need:
        return AgentSplitPlan(
            agent_id, False,
            f"too few skills to split: {len(skills)} < 2*min_cluster_skills={need}",
        )

    vectors = _skill_vectors(skills, embed_fn)
    skill_ids = [s.skill_id for s in skills]
    cluster0, cluster1 = _kmeans2(skill_ids, vectors)
    sil = silhouette_two(cluster0, cluster1, vectors)

    if sil < params.silhouette_threshold:
        return AgentSplitPlan(
            agent_id, False,
            f"silhouette {sil:.3f} < threshold {params.silhouette_threshold}"
            " — no separable specialty structure",
            silhouette=sil,
        )
    if len(cluster0) < params.min_cluster_skills or len(cluster1) < params.min_cluster_skills:
        return AgentSplitPlan(
            agent_id, False,
            f"a child cluster is undersized ({len(cluster0)}, {len(cluster1)} <"
            f" min_cluster_skills={params.min_cluster_skills})",
            silhouette=sil,
            cluster0_skill_ids=tuple(cluster0),
            cluster1_skill_ids=tuple(cluster1),
        )

    by_id = {s.skill_id: s for s in skills}
    names0 = [by_id[i].name for i in cluster0]
    names1 = [by_id[i].name for i in cluster1]
    describer = describer_fn if describer_fn is not None else _default_describer
    spec0, spec1 = describer(parent, names0, names1)

    for spec in (spec0, spec1):
        name, description, base_prompt = spec
        if _word_count(description) > params.max_description_words:
            return AgentSplitPlan(
                agent_id, False,
                f"child description for '{name}' is {_word_count(description)} words"
                f" > the {params.max_description_words}-word compressibility gate (§6)",
                silhouette=sil,
                cluster0_skill_ids=tuple(cluster0),
                cluster1_skill_ids=tuple(cluster1),
            )
        if _has_base_prompt_residue(parent["base_prompt_specialty"], base_prompt):
            return AgentSplitPlan(
                agent_id, False,
                f"base-prompt residue in child '{name}' blocks the split"
                " (conversion warning): the child base prompt still carries the"
                " parent's generic residue and must be specialized before commit (§6)",
                silhouette=sil,
                cluster0_skill_ids=tuple(cluster0),
                cluster1_skill_ids=tuple(cluster1),
            )

    return AgentSplitPlan(
        agent_id, True, "split planned",
        silhouette=sil,
        cluster0_skill_ids=tuple(cluster0),
        cluster1_skill_ids=tuple(cluster1),
        child_specs=(spec0, spec1),
    )


# --- routing replay (§6: >=90% agreement against the logged decisions) ---------


@dataclass(frozen=True)
class ReplayResult:
    """Routing replay against the logged decisions (§6)."""

    evaluated: int
    agreed: int

    @property
    def agreement(self) -> float:
        return self.agreed / self.evaluated if self.evaluated else 0.0


def routing_replay_agreement(
    store: Store,
    plan: AgentSplitPlan,
    *,
    embed_fn: EmbedFn | None = None,
) -> ReplayResult:
    """Re-score the planned split against the logged routing decisions (§6).

    For each logged decision routed to the parent, the request is re-routed to the
    nearest child centroid (in skill-description space) and compared to the child
    that owns the skill that actually served it (the recorded ``top_skill_id``).
    Only decisions carrying both a request vector and a top skill are evaluable.
    """
    ensure_routing_log(store)
    if plan.child_specs is None:
        raise AgentSplitError("cannot replay an unaccepted plan (no clusters)")

    # Child centroids in description space — re-embed each child cluster's skills.
    rows = store.conn.execute(
        "SELECT id, description FROM skills WHERE id IN ({})".format(
            ", ".join("?" for _ in (plan.cluster0_skill_ids + plan.cluster1_skill_ids))
        ),
        plan.cluster0_skill_ids + plan.cluster1_skill_ids,
    ).fetchall()
    if embed_fn is None:
        raise AgentSplitError("routing replay requires an embed_fn seam")
    desc_by_id = {r["id"]: r["description"] for r in rows}
    vecs = {sid: list(embed_fn(desc_by_id[sid])) for sid in desc_by_id}
    centroid0 = _centroid([vecs[i] for i in plan.cluster0_skill_ids])
    centroid1 = _centroid([vecs[i] for i in plan.cluster1_skill_ids])
    cluster0 = set(plan.cluster0_skill_ids)
    cluster1 = set(plan.cluster1_skill_ids)

    decisions = store.conn.execute(
        f"SELECT request, request_vector_json, top_skill_id FROM {ROUTING_LOG_TABLE}"
        " WHERE chosen_agent_id = ? ORDER BY id ASC",
        (plan.agent_id,),
    ).fetchall()

    evaluated = 0
    agreed = 0
    for d in decisions:
        top = d["top_skill_id"]
        if top is None or (top not in cluster0 and top not in cluster1):
            continue
        if d["request_vector_json"] is not None:
            rvec = json.loads(d["request_vector_json"])
        elif embed_fn is not None:
            rvec = list(embed_fn(d["request"]))
        else:
            continue
        evaluated += 1
        # Replayed routing: the child whose centroid the request is nearest.
        replay_child = 0 if _euclid(rvec, centroid0) <= _euclid(rvec, centroid1) else 1
        # Reference: the child that owns the skill that actually served the request.
        reference_child = 0 if top in cluster0 else 1
        if replay_child == reference_child:
            agreed += 1
    return ReplayResult(evaluated=evaluated, agreed=agreed)


# --- execution: the revertible lineage transaction (§6) ------------------------


@dataclass
class AgentSplitResult:
    """An executed (pending) agent split — the handle revert/confirm operate on."""

    parent_agent_id: int
    child_agent_ids: tuple[int, int]
    cluster0_skill_ids: tuple[int, ...]
    cluster1_skill_ids: tuple[int, ...]
    silhouette: float
    replay_agreement: float = -1.0
    status: str = LINEAGE_SPLIT_PENDING  # split_pending | confirmed | reverted
    child_specs: tuple[tuple[str, str, str], tuple[str, str, str]] | None = field(
        default=None
    )


def execute_agent_split(
    store: Store, plan: AgentSplitPlan, *, params: AgentSplitParams = AgentSplitParams()
) -> AgentSplitResult:
    """Mint the two pending children and re-point the parent's skills (§6).

    Transactional and revertible: children carry ``parent_id`` +
    ``lineage_status='split_pending'``; the parent is marked ``retired_by_split``
    (a frozen lineage anchor, never deleted). Revertible until replay + benchmark
    confirm.
    """
    if not plan.accepted or plan.child_specs is None:
        raise AgentSplitError(
            f"cannot execute an unaccepted plan for agent {plan.agent_id}:"
            f" {plan.reason}"
        )
    parent = store.conn.execute(
        "SELECT id, family_id, name FROM agents WHERE id = ?", (plan.agent_id,)
    ).fetchone()
    if parent is None:
        raise AgentSplitError(f"agent {plan.agent_id} does not exist")
    (name0, desc0, base0), (name1, desc1, base1) = plan.child_specs

    with store.transaction():
        child0 = store.create_agent(
            parent["family_id"], name0, description=desc0,
            base_prompt_specialty=base0, parent_id=plan.agent_id,
        )
        child1 = store.create_agent(
            parent["family_id"], name1, description=desc1,
            base_prompt_specialty=base1, parent_id=plan.agent_id,
        )
        store.conn.execute(
            "UPDATE agents SET lineage_status = ? WHERE id IN (?, ?)",
            (LINEAGE_SPLIT_PENDING, child0, child1),
        )
        for sid in plan.cluster0_skill_ids:
            store.conn.execute(
                "UPDATE skills SET agent_id = ? WHERE id = ?", (child0, sid)
            )
        for sid in plan.cluster1_skill_ids:
            store.conn.execute(
                "UPDATE skills SET agent_id = ? WHERE id = ?", (child1, sid)
            )
        store.conn.execute(
            "UPDATE agents SET lineage_status = ? WHERE id = ?",
            (LINEAGE_RETIRED_BY_SPLIT, plan.agent_id),
        )
    return AgentSplitResult(
        parent_agent_id=plan.agent_id,
        child_agent_ids=(child0, child1),
        cluster0_skill_ids=plan.cluster0_skill_ids,
        cluster1_skill_ids=plan.cluster1_skill_ids,
        silhouette=plan.silhouette,
        child_specs=plan.child_specs,
    )


def revert_agent_split(store: Store, result: AgentSplitResult) -> None:
    """Undo a pending split — restores routing identically (§6).

    Skills are re-pointed back to the parent, the pending children are removed, and
    the parent's ``retired_by_split`` mark is cleared. Only a still-pending split is
    revertible (a confirmed split is permanent)."""
    if result.status != LINEAGE_SPLIT_PENDING:
        raise AgentSplitError(
            f"cannot revert a {result.status} split (only split_pending is revertible)"
        )
    child0, child1 = result.child_agent_ids
    with store.transaction():
        store.conn.execute(
            "UPDATE skills SET agent_id = ? WHERE agent_id IN (?, ?)",
            (result.parent_agent_id, child0, child1),
        )
        store.conn.execute(
            "DELETE FROM agents WHERE id IN (?, ?)", (child0, child1)
        )
        store.conn.execute(
            "UPDATE agents SET lineage_status = NULL WHERE id = ?",
            (result.parent_agent_id,),
        )
    result.status = "reverted"


def confirm_agent_split(
    store: Store, result: AgentSplitResult, *, benchmark_passed: bool
) -> None:
    """Commit a pending split after routing replay + one benchmark run pass (§6).

    Flips the children live (``lineage_status`` cleared); the parent stays
    ``retired_by_split`` (frozen anchor). A failed benchmark gate is a hard error —
    the caller reverts."""
    if result.status != LINEAGE_SPLIT_PENDING:
        raise AgentSplitError(
            f"cannot confirm a {result.status} split (only split_pending confirms)"
        )
    if not benchmark_passed:
        raise AgentSplitError(
            "benchmark-pass gate failed: the split must be reverted, not confirmed (§6)"
        )
    child0, child1 = result.child_agent_ids
    with store.transaction():
        store.conn.execute(
            "UPDATE agents SET lineage_status = NULL WHERE id IN (?, ?)",
            (child0, child1),
        )
    result.status = "confirmed"


# --- the full revertible transaction (§6 end-to-end) ---------------------------


@dataclass(frozen=True)
class AgentSplitOutcome:
    """The end-to-end split attempt's verdict (§6)."""

    agent_id: int
    executed: bool
    confirmed: bool
    reverted: bool
    reason: str
    plan: AgentSplitPlan
    replay: ReplayResult | None = None
    result: AgentSplitResult | None = None


def run_agent_split(
    store: Store,
    agent_id: int,
    *,
    embed_fn: EmbedFn,
    benchmark_fn: BenchmarkFn,
    params: AgentSplitParams = AgentSplitParams(),
    describer_fn: DescriberFn | None = None,
) -> AgentSplitOutcome:
    """Plan -> execute (pending) -> replay-gate -> benchmark-gate -> confirm/revert.

    The full §6 transaction: a split that clears clustering, the description gates,
    routing replay (>=90% agreement), and one benchmark run is confirmed; any gate
    failure reverts (or never executes), leaving routing identical to before."""
    plan = plan_agent_split(
        store, agent_id, embed_fn=embed_fn, params=params, describer_fn=describer_fn
    )
    if not plan.accepted:
        return AgentSplitOutcome(
            agent_id, executed=False, confirmed=False, reverted=False,
            reason=plan.reason, plan=plan,
        )

    result = execute_agent_split(store, plan, params=params)
    replay = routing_replay_agreement(store, plan, embed_fn=embed_fn)
    result.replay_agreement = replay.agreement
    if replay.agreement < params.replay_agreement_threshold:
        revert_agent_split(store, result)
        return AgentSplitOutcome(
            agent_id, executed=True, confirmed=False, reverted=True,
            reason=(
                f"routing replay {replay.agreement:.2%} < threshold"
                f" {params.replay_agreement_threshold:.0%} ({replay.agreed}/"
                f"{replay.evaluated}) — split reverted, parent preserved"
            ),
            plan=plan, replay=replay, result=result,
        )

    if not benchmark_fn():
        revert_agent_split(store, result)
        return AgentSplitOutcome(
            agent_id, executed=True, confirmed=False, reverted=True,
            reason="benchmark-pass gate failed — split reverted, parent preserved",
            plan=plan, replay=replay, result=result,
        )

    confirm_agent_split(store, result, benchmark_passed=True)
    return AgentSplitOutcome(
        agent_id, executed=True, confirmed=True, reverted=False,
        reason="split confirmed (clustering + replay + benchmark all passed)",
        plan=plan, replay=replay, result=result,
    )


# --- R14: the explorer-family decision point (documented, not automatic) -------


@dataclass(frozen=True)
class ExplorerFamilyDecision:
    """The documented seed-or-sink decision for the explorer (and grader) family
    (R14). Not an automatic — a decision point with its evidence query."""

    seed: bool
    recurring_lesson_count: int
    threshold: int
    reason: str


def explorer_family_decision(
    recurring_explorer_lesson_count: int, *, threshold: int = 5
) -> ExplorerFamilyDecision:
    """Decide whether to seed an explorer family from the normal taxonomy (R14).

    The evidence query is the count of recurring explorer-shaped lessons (explorer
    prompt/answer lessons recurring in Plan 4's instrument-health records). With
    idea-shaped volume at or above the threshold, seed; otherwise the sink remains
    (the Phase 3a reality — a documented decision, not an automatic)."""
    seed = recurring_explorer_lesson_count >= threshold
    reason = (
        f"seed explorer family: {recurring_explorer_lesson_count} recurring"
        f" explorer-shaped lessons >= threshold {threshold}"
        if seed
        else (
            f"sink remains: {recurring_explorer_lesson_count} recurring explorer-"
            f"shaped lessons < threshold {threshold} (no idea-shaped volume yet)"
        )
    )
    return ExplorerFamilyDecision(
        seed=seed,
        recurring_lesson_count=recurring_explorer_lesson_count,
        threshold=threshold,
        reason=reason,
    )
