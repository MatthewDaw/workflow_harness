"""Reflector Stage B: stingy, validated, counterfactual reflection (plan-004 U6).

Stage A hands Stage B a set of attributions, each with a *case file* (the
implicated trace rows + evidence). Stage B turns those into at most a handful of
quarantined library ideas — and is **stingy by construction** (DESIGN §12.3, §15):

1. **Cluster first** (R10): failed items are grouped by
   ``(ticket | FEAT | failure-signature)`` so six scenarios with one root cause
   become one reflection, not six. The failure signature is the Stage A
   attribution ``(role, aspect)`` — same cause, same signature.
2. **One reflection per cluster** (R10): a single fresh-context counterfactual
   judge call per cluster, seeded with the case file and given *explicit no-lesson
   permission*. A cluster yields 0 or 1 ideas — never more.
3. **Typed output validation** (R12): the proposed ``counterfactual_insight`` must
   pass the structural (precondition / action / expected-outcome) schema — free
   prose is rejected; ``implicated_existing_insights`` must reference insights
   **active at the episode snapshot** (in a training episode a same-batch
   quarantined ref is a validator error; in a trial it is legal and feeds the
   batch verdict only); ``scope_tag_proposal`` flows to ``add_idea``'s registration
   judge; an ``attribution_override`` wins over Stage A only when its confidence
   clears the configured gate — below it, the override is *recorded but not
   applied* (override-rate-per-link-type is contract-tightening telemetry, §12.3).
4. **Budget + ranking** (R11): a per-episode idea budget caps the batch; when more
   clusters carry lessons than the budget allows, they rank must-tier first, then
   cluster size, then confidence, and the overflow is dropped to a telemetry row.
   **An empty batch is a legal episode outcome** — settle, no validation cycle,
   one telemetry row.
5. **Batch formation** (R10/R13): surviving lessons register through Phase 0
   ``add_idea`` under one batch label (status=quarantined, batch-tagged), and the
   full provenance chain (episode -> cluster -> scenarios -> insight) rides home in
   :class:`StageBResult`.

Offline by construction: the reflection call goes through the judge seam (a fake
``judge_fn`` in the suite, ``run_judge`` live), and registration goes through an
injected ``register_fn`` (a fake in unit tests, the real ``add_idea`` +
record/replay fixtures in the integration test). Zero quota, no ``claude`` on PATH.

Scope note (smallest faithful adaptation): the U6 *Approach* line also mentions
"run-memory nominations". Run memory (R5/R6) is unit U3's ``pipeline/runmemory.py``,
which does not exist in this wave; U6's *requirements* are R10-R13 only, so the
success-channel nomination path is left to U3 (it submits its survivors through the
same ``add_idea`` batch tag this module establishes).

## Conformance

Test-scenario / invariant (plan-004 U6) -> test (in ``tests/test_stage_b.py``):

- clustering merges six same-cause SCEN failures into one cluster:
  ``test_clustering_merges_same_cause_failures``
- a different cause splits into its own cluster:
  ``test_clustering_splits_distinct_causes``
- no-lesson output produces no insight and a telemetry row:
  ``test_no_lesson_produces_no_insight_and_a_telemetry_row``
- over-budget ranking drops the right clusters (must-tier > size > confidence):
  ``test_over_budget_ranking_drops_the_right_clusters``
- one idea per cluster is enforced (verification):
  ``test_one_idea_per_cluster_enforced``
- validator rejects a same-batch / non-active implicated ref in training mode:
  ``test_validator_rejects_non_active_implicated_ref_in_training``
- a quarantined batch-under-trial ref is legal in trial mode:
  ``test_validator_allows_batch_under_trial_ref_in_trial``
- validator rejects free-prose (non-structural) insights:
  ``test_validator_rejects_free_prose_insight``
- override below the confidence threshold is recorded but not applied:
  ``test_override_below_threshold_recorded_not_applied``
- override at/above the threshold is applied:
  ``test_override_at_threshold_is_applied``
- empty batch settles legally (no validation cycle, telemetry row):
  ``test_empty_batch_settles_legally``
- batch lands quarantined with full provenance:
  ``test_batch_lands_quarantined_with_full_provenance``
- a full fixture episode yields a batch whose every insight passes registration:
  ``test_full_episode_every_insight_passes_registration``

R3 routing (plan-008 U8, R19)
-----------------------------

The R3 ingest gauntlet (:func:`agent_families.pipeline.add_idea`) is the
registration path (plan-008 A-U6 cut-over). Reflector lessons inherit Operation 1
(the admission gate's generalize/altitude-audit) and Operation 2 (key-collision +
NLI verdict) **for free** — stage_b hands the raw counterfactual triple straight to
the gate and performs NO in-reflector generalization (the gate owns it, DESIGN §13).
The ``for lesson in selected`` loop captures every non-registering gate outcome
(``lint_reject`` / ``rewrite_proposed`` / structural reject) as a telemetry row
and CONTINUES with the remaining lessons — one bad lesson never crashes or
aborts the batch. A duplicate becomes a corroboration vote on the incumbent,
recorded through the ONE ``corroborate`` fitness-event counter inside
``add_idea`` (the §11 cross-target-recurrence substrate); stage_b adds no
separate recurrence tally. A deferred-supersede (a ``contradicts`` edge written
at ingest) lands as a normal quarantined registration whose move rides home in
:class:`InsightProvenance`; the incumbent is invalidated only at promotion
(``validate.py`` surfaces the retired-incumbent ids in the ``batch_validations``
record).

Note: ``add_idea_r3`` is now an alias for ``add_idea`` (collapsed in U6 cut-over).
The legacy author-at-ingest path (``_add_idea_legacy``) is deleted. The ``use_r3_gate``
parameter in :func:`make_add_idea_registrar` is a no-op (always routes through R3)
and is retained only for backward-compat with existing test call sites.

Test-scenario / invariant (plan-008 U8, R19) -> test (in ``tests/test_stage_b.py``):

- reflector text reaches ``add_idea`` via the registrar and inherits the R3 gate;
  stage_b does NO separate generalization:
  ``test_reflector_routes_through_r3_gate_no_separate_generalization``
- a gate rejection inside the selected loop is telemetry, not a crash; the loop
  continues: ``test_gate_rejection_does_not_abort_batch``
- a duplicating lesson corroborates through the single fitness-event counter, with
  no separate recurrence tally: ``test_corroborate_is_single_recurrence_counter``
- promoting a reflector batch that retires a contradicted incumbent names it in the
  validation record: ``test_validation_record_names_retired_incumbent``
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Sequence

from agent_families.judge import run_judge
from agent_families.reflector.stage_a import Attribution, StageAResult
from agent_families.store import Store

# --- tunables (caller-supplied, per the U2/U3/U5 precedent) ---------------------
#
# Tunables are NOT read from config here — they ride in :class:`StageBParams`, and
# the run-assembly wiring routes the live values from ``thresholds.toml``
# (idea_budget ~5-8 per R11; override_confidence_threshold per R13). The values
# below are the carried seam defaults so the params object always has something to
# read (the same "carried now so the seam reads it" discipline as ``active_cap``).

# PROVENANCE: R11 / DESIGN §15 — "per-episode idea budget ~5-8"; stinginess is
# where curation value comes from. TUNING METRIC: promoted-idea yield vs batch
# size on the calibration corpus.
DEFAULT_IDEA_BUDGET = 6

# PROVENANCE: R13 — attribution_override wins over Stage A only at high confidence.
# TUNING METRIC: override-rate-per-link-type drift (§12.3 contract-tightening).
DEFAULT_OVERRIDE_CONFIDENCE_THRESHOLD = 0.7

# review_queue.kind values for Stage B's telemetry rows (R11). Persisted the same
# way Stage A persists instrument-health records, so an empty/over-budget episode
# leaves an auditable trail.
NO_LESSON_KIND = "reflection_no_lesson"
OVER_BUDGET_KIND = "reflection_over_budget"
EMPTY_BATCH_KIND = "reflection_empty_batch"
# A lesson the registration gate declined (lint_reject / rewrite_proposed /
# structural reject): captured as telemetry so a single bad lesson never aborts
# the batch (R19). The selected loop continues with the remaining lessons.
GATE_REJECTED_KIND = "reflection_gate_rejected"
# A lesson that duplicated an existing insight: the corroboration vote is recorded
# through add_idea_r3's single `corroborate` fitness-event counter (the §11
# recurrence substrate); this telemetry row only names the incumbent (R19).
CORROBORATE_KIND = "reflection_corroborate"

# Registration codes that mean "no new quarantined insight landed" — the gate
# declined the lesson (R19). The selected loop turns these into telemetry and
# carries on; ``add_idea``'s success codes (registered/corroborated) are handled
# separately.
GATE_REJECTION_CODES = frozenset(
    {"lint_reject", "rewrite_proposed", "structural_invalid", "no_placement",
     "retired_near_duplicate", "rejected"}
)

# Structural fields every counterfactual insight must carry (R12); free prose is
# rejected for missing any of them (mirrors the add_idea structural template).
STRUCTURAL_FIELDS = ("precondition", "action", "expected_outcome")


class StageBError(Exception):
    """A broken Stage B precondition with an actionable message."""


class StageBValidationError(StageBError):
    """A reflection output failed typed validation (R12) — no idea is registered."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- the reflection judge contract ----------------------------------------------

# The single counterfactual schema (R10/R12). Nullable fields use a type list so
# the hand-rolled judge validator accepts an explicit ``null`` (no-lesson, no
# scope proposal, no override). ``confidence`` governs both ranking (R11) and the
# override gate (R13).
REFLECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "has_lesson": {"type": "boolean"},
        "counterfactual_insight": {
            "type": ["object", "null"],
            "properties": {
                "precondition": {"type": "string"},
                "action": {"type": "string"},
                "expected_outcome": {"type": "string"},
            },
            "required": ["precondition", "action", "expected_outcome"],
            "additionalProperties": False,
        },
        "implicated_existing_insights": {
            "type": "array",
            "items": {"type": "integer"},
        },
        "scope_tag_proposal": {"type": ["string", "null"]},
        "attribution_override": {
            "type": ["object", "null"],
            "properties": {
                "role": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["role", "confidence"],
            "additionalProperties": False,
        },
        "confidence": {"type": "number"},
    },
    "required": ["has_lesson", "confidence"],
    "additionalProperties": False,
}

JudgeFn = Callable[..., object]

# A registrar turns one validated idea into a :class:`RegistrationOutcome` — the
# new insight id plus the registration ``code``/move so the selected loop can tell
# a fresh quarantined insight from a gate rejection or a corroboration vote (R19).
# The default binds ``add_idea`` / ``add_idea_r3`` (see
# :func:`make_add_idea_registrar`); unit tests inject a fake so they need no
# embedder / vec / gate fixtures.
RegisterFn = Callable[["RegisteredIdea"], "RegistrationOutcome"]


# --- clustering (R10) -----------------------------------------------------------


@dataclass(frozen=True)
class Cluster:
    """A group of failed scenarios sharing ``(ticket | FEAT | failure-signature)``.

    ``signature`` is the Stage A attribution ``(role, aspect)`` — the failure's
    causal fingerprint. ``must_tier`` is true iff ANY member scenario is must-tier
    (drives R11 ranking). ``attributions`` are id-ordered for determinism.
    """

    cluster_id: str
    feat_id: str
    tkt_key: str
    signature: tuple[str, str]
    must_tier: bool
    attributions: tuple[Attribution, ...]

    @property
    def size(self) -> int:
        return len(self.attributions)

    @property
    def scen_ids(self) -> tuple[str, ...]:
        return tuple(a.scen_id for a in self.attributions)

    @property
    def primary(self) -> Attribution:
        """The lexicographically-first member is the cluster's representative."""
        return self.attributions[0]


def _signature_hash(role: str, aspect: str) -> str:
    return hashlib.sha256(f"{role}|{aspect}".encode("utf-8")).hexdigest()[:12]


def _cluster_key(attr: Attribution) -> tuple[str, str, str, str]:
    feat_id = attr.case_file.feat_id
    tkt_key = ",".join(sorted(attr.case_file.tkt_ids))
    return (feat_id, tkt_key, attr.primary.role, attr.primary.aspect)


def _scen_is_must(store: Store, scen_id: str) -> bool:
    row = store.conn.execute(
        "SELECT tier FROM trace_scen WHERE id = ?", (scen_id,)
    ).fetchone()
    return bool(row is not None and row["tier"] == "must")


def cluster_failures(
    store: Store, attributions: Sequence[Attribution]
) -> list[Cluster]:
    """Group attributions into clusters by ``(ticket | FEAT | failure-signature)``.

    Deterministic: clusters are returned in ``cluster_id`` order and each cluster's
    members are ``scen_id``-ordered. Instrument-health attributions never reach here
    (Stage A sank them); a caller passing them in is grouped like any other, but the
    intended input is :pyattr:`StageAResult.idea_candidates`.
    """
    buckets: dict[tuple[str, str, str, str], list[Attribution]] = {}
    for attr in attributions:
        buckets.setdefault(_cluster_key(attr), []).append(attr)

    clusters: list[Cluster] = []
    for key, members in buckets.items():
        feat_id, tkt_key, role, aspect = key
        members_sorted = tuple(sorted(members, key=lambda a: a.scen_id))
        cluster_id = f"{feat_id}|{tkt_key}|{_signature_hash(role, aspect)}"
        must_tier = any(_scen_is_must(store, a.scen_id) for a in members_sorted)
        clusters.append(
            Cluster(
                cluster_id=cluster_id,
                feat_id=feat_id,
                tkt_key=tkt_key,
                signature=(role, aspect),
                must_tier=must_tier,
                attributions=members_sorted,
            )
        )
    clusters.sort(key=lambda c: c.cluster_id)
    return clusters


# --- reflection (R10) -----------------------------------------------------------


@dataclass(frozen=True)
class Reflection:
    """One cluster's parsed counterfactual reflection output."""

    cluster: Cluster
    has_lesson: bool
    insight: dict | None
    implicated_existing_insights: tuple[int, ...]
    scope_tag_proposal: str | None
    attribution_override: dict | None
    confidence: float


def build_reflection_prompt(store: Store, cluster: Cluster) -> str:
    """Deterministic counterfactual prompt seeded with the cluster's case files.

    Carries no volatile data (no timestamps, no absolute paths, no cosines) so the
    request hash is stable for record/replay (R23). The prompt asks for ONE lesson
    or an explicit no-lesson — the stinginess contract is in the instruction, the
    budget enforces it structurally.
    """
    role, aspect = cluster.signature
    case_files = [a.case_file.to_dict() for a in cluster.attributions]
    body = json.dumps(
        {
            "feat_id": cluster.feat_id,
            "implicated_tickets": cluster.tkt_key,
            "failure_signature": {"role": role, "aspect": aspect},
            "must_tier": cluster.must_tier,
            "scenarios": list(cluster.scen_ids),
            "case_files": case_files,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return (
        "You are the reflector's Stage B counterfactual analyst. A cluster of"
        " scenarios failed for one shared root cause, attributed by the"
        f" deterministic Stage A to the {role!r} role ({aspect!r}). You have the"
        " case file (implicated trace rows + evidence) below and read-only"
        " trace-query tools.\n\n"
        "Reflect counterfactually: what *general*, reusable insight, had a"
        " specialist held it BEFORE this episode, would most plausibly have"
        " prevented this failure? Propose AT MOST ONE insight, in the"
        " precondition / action / expected-outcome template. You have explicit"
        " permission to find NO lesson — if the failure is idiosyncratic, a"
        " one-off, or already covered, return has_lesson=false. Stinginess is the"
        " goal: a smaller, sharper library beats a larger noisy one.\n\n"
        "List any EXISTING active insights this failure implicates"
        " (implicated_existing_insights, by id) — these are causal-blame links,"
        " not co-occurrence. If you believe Stage A mis-attributed the cause, set"
        " attribution_override with your replacement role and your confidence.\n\n"
        f"## Case file\n{body}\n\n"
        "Return structured output only, conforming to the schema."
    )


def reflect_cluster(
    store: Store,
    cluster: Cluster,
    *,
    judge_fn: JudgeFn | None = None,
    judge_model: str = "sonnet",
    judge_max_retries: int = 0,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
) -> Reflection:
    """Run ONE counterfactual reflection call for a cluster (R10)."""
    judge = judge_fn if judge_fn is not None else run_judge
    result = judge(
        build_reflection_prompt(store, cluster),
        REFLECTION_SCHEMA,
        judge_model,
        max_retries=judge_max_retries,
        mode=judge_mode,
        fixtures_dir=judge_fixtures_dir,
    )
    output = result.output
    has_lesson = bool(output.get("has_lesson"))
    insight = output.get("counterfactual_insight")
    if not has_lesson:
        insight = None
    override = output.get("attribution_override")
    return Reflection(
        cluster=cluster,
        has_lesson=has_lesson,
        insight=insight,
        implicated_existing_insights=tuple(
            output.get("implicated_existing_insights") or []
        ),
        scope_tag_proposal=output.get("scope_tag_proposal"),
        attribution_override=override,
        confidence=float(output.get("confidence", 0.0)),
    )


# --- typed output validation (R12) ----------------------------------------------


def _validate_structural_insight(insight: object) -> dict:
    if not isinstance(insight, dict):
        raise StageBValidationError(
            "a counterfactual lesson must be a structured"
            " precondition/action/expected-outcome object, not free prose (R12)"
        )
    for name in STRUCTURAL_FIELDS:
        value = insight.get(name)
        if not isinstance(value, str) or not value.strip():
            raise StageBValidationError(
                f"counterfactual insight field '{name}' must be a non-empty"
                " string — free prose is rejected (R12)"
            )
    return {name: insight[name] for name in STRUCTURAL_FIELDS}


def _batch_under_trial_members(store: Store, batch_id: int | None) -> set[int]:
    if batch_id is None:
        return set()
    return {
        r["id"]
        for r in store.conn.execute(
            "SELECT id FROM insights WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    }


def validate_reflection(
    store: Store,
    reflection: Reflection,
    *,
    episode_id: int,
    mode: str = "training",
    batch_under_trial_id: int | None = None,
    override_confidence_threshold: float = DEFAULT_OVERRIDE_CONFIDENCE_THRESHOLD,
) -> "ValidatedLesson":
    """Validate one lesson-bearing reflection (R12); raise on any violation.

    - ``counterfactual_insight`` must pass the structural schema (free prose out);
    - every ``implicated_existing_insights`` ref must be **active at the episode
      snapshot** in a training episode; in a trial, a quarantined member of the
      batch under trial is additionally legal (it feeds the batch verdict only);
    - ``attribution_override`` is *applied* (wins over Stage A) only when its
      confidence clears ``override_confidence_threshold``; below it, it is recorded
      but not applied.
    """
    if not reflection.has_lesson:
        raise StageBValidationError(
            "validate_reflection called on a no-lesson reflection; only"
            " lesson-bearing reflections are validated"
        )
    fields = _validate_structural_insight(reflection.insight)

    episode = store.get_episode(episode_id)
    if episode is None:
        raise StageBError(f"episode {episode_id} does not exist")
    snapshot_id = episode["snapshot_id"]
    trial_ok = (
        _batch_under_trial_members(store, batch_under_trial_id)
        if mode == "trial"
        else set()
    )
    for insight_id in reflection.implicated_existing_insights:
        exists = store.conn.execute(
            "SELECT 1 FROM insights WHERE id = ?", (insight_id,)
        ).fetchone()
        if exists is None:
            raise StageBValidationError(
                f"implicated insight {insight_id} does not exist (R12)"
            )
        status = store.status_at(insight_id, snapshot_id)
        if status == "active":
            continue
        if insight_id in trial_ok:
            # A quarantined batch-under-trial ref is legal in a trial (R12).
            continue
        raise StageBValidationError(
            f"implicated insight {insight_id} is not active at the episode"
            f" snapshot {snapshot_id} (status={status!r}); in a training episode a"
            " same-batch quarantined ref is a validator error (R12)"
        )

    override = reflection.attribution_override
    override_applied = False
    effective_role = reflection.cluster.signature[0]
    if override is not None:
        override_applied = (
            float(override.get("confidence", 0.0)) >= override_confidence_threshold
        )
        if override_applied:
            effective_role = override.get("role", effective_role)

    return ValidatedLesson(
        reflection=reflection,
        fields=fields,
        scope_tag=reflection.scope_tag_proposal,
        override=override,
        override_applied=override_applied,
        effective_role=effective_role,
    )


@dataclass(frozen=True)
class ValidatedLesson:
    """A reflection that passed typed validation and is ready to register."""

    reflection: Reflection
    fields: dict
    scope_tag: str | None
    override: dict | None
    override_applied: bool
    effective_role: str

    @property
    def cluster(self) -> Cluster:
        return self.reflection.cluster

    @property
    def confidence(self) -> float:
        return self.reflection.confidence


# --- budget + ranking (R11) -----------------------------------------------------


def rank_and_select(
    lessons: Sequence[ValidatedLesson], budget: int
) -> tuple[list[ValidatedLesson], list[ValidatedLesson]]:
    """Keep at most ``budget`` lessons; return ``(selected, dropped)`` (R11).

    Ranking key (highest first): must-tier, then cluster size, then confidence.
    Ties beyond that break on ``cluster_id`` for determinism.
    """
    if budget < 0:
        raise StageBError(f"idea budget must be >= 0, got {budget}")
    ordered = sorted(
        lessons,
        key=lambda l: (
            0 if l.cluster.must_tier else 1,
            -l.cluster.size,
            -l.confidence,
            l.cluster.cluster_id,
        ),
    )
    return list(ordered[:budget]), list(ordered[budget:])


# --- batch formation (R10/R13) --------------------------------------------------


@dataclass(frozen=True)
class RegistrationOutcome:
    """What a registrar reports back for one lesson (R19).

    ``code`` is ``add_idea``'s outcome code — ``registered`` / ``corroborated``
    (a duplicate became a vote, no new insight) — or a gate-rejection code in
    :data:`GATE_REJECTION_CODES` when the admission gate declined the lesson.
    ``insight_id`` is the new (or, for a corroboration, the incumbent's) id, or
    ``None`` on rejection; ``outcome`` carries the move summary
    (corroborate/refine/contradicts/unrelated) for a registered R3 insight.
    """

    insight_id: int | None
    code: str
    outcome: str | None = None
    detail: str = ""

    @property
    def is_gate_rejection(self) -> bool:
        return self.code in GATE_REJECTION_CODES


@dataclass(frozen=True)
class RegisteredIdea:
    """The payload handed to a registrar: the idea text + its provenance."""

    precondition: str
    action: str
    expected_outcome: str
    scope_tag: str | None
    batch_label: str
    cluster_id: str
    feat_id: str
    scen_ids: tuple[str, ...]
    effective_role: str
    confidence: float


@dataclass(frozen=True)
class InsightProvenance:
    """A registered insight's full provenance chain (R10) — episode -> cluster ->
    scenarios -> insight, plus the override/blame telemetry (R13)."""

    insight_id: int
    cluster_id: str
    feat_id: str
    scen_ids: tuple[str, ...]
    effective_role: str
    override_applied: bool
    implicated_existing_insights: tuple[int, ...]
    scope_tag: str | None
    confidence: float
    # The R3 registration move (corroborate/refine/contradicts/unrelated summary)
    # when the lesson routed through the gauntlet; None on the legacy path (R19).
    judge_outcome: str | None = None


@dataclass(frozen=True)
class StageBResult:
    """The episode's Stage B outcome: the quarantined batch + the stinginess trail."""

    episode_id: int
    batch_label: str | None
    registered: tuple[InsightProvenance, ...]
    no_lesson_cluster_ids: tuple[str, ...]
    dropped_cluster_ids: tuple[str, ...]
    telemetry_ids: tuple[int, ...]
    # R19 routing telemetry: incumbents a duplicating lesson corroborated (the vote
    # rides add_idea_r3's single fitness-event counter), and clusters the
    # registration gate declined without aborting the batch.
    corroborated_incumbent_ids: tuple[int, ...] = ()
    gate_rejected_cluster_ids: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.registered


def _write_telemetry(
    store: Store, episode_id: int, kind: str, payload: dict
) -> int:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    with store.transaction():
        cur = store.conn.execute(
            "INSERT INTO review_queue (episode_id, kind, payload_json, status,"
            " created_at) VALUES (?, ?, ?, 'open', ?)",
            (episode_id, kind, body, _utcnow()),
        )
    return cur.lastrowid


# add_idea / add_idea_r3 exception class name -> the RegistrationOutcome code the
# selected loop turns into a telemetry row (R19). Anything not here (e.g. a
# JudgeError / fixture-missing) is a real fault and propagates, never swallowed.
_REJECTION_CODE_BY_EXC = {
    "LintRejected": "lint_reject",
    "RewriteProposed": "rewrite_proposed",
    "StructuralValidationError": "structural_invalid",
    "NoPlacement": "no_placement",
    "RetiredNearDuplicate": "retired_near_duplicate",
}


def make_add_idea_registrar(
    store: Store,
    vec,
    embedder,
    config,
    *,
    use_r3_gate: bool = True,  # kept for back-compat; always routes through R3 gate
    provenance: str = "reflector",
    corroborate_mode: str = "training",
    accept_rewrite: bool = False,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
    nli_model: str | None = None,
    nli_mode: str | None = None,
    nli_fixtures_dir=None,
) -> RegisterFn:
    """Bind ``add_idea`` as a Stage B registrar (R19).

    The lesson routes through the R3 ingest gauntlet (``add_idea``) — inheriting
    Operation 1 (the admission gate's generalize/altitude-audit) and Operation 2
    (key-collision + NLI verdict), so stage_b hands the gate the raw counterfactual
    triple and does NO generalization of its own (DESIGN §13). The ``use_r3_gate``
    parameter is accepted for backward-compat but is a no-op — the legacy
    author-at-ingest path is deleted (plan-008 A-U6 cut-over).

    The registrar returns a :class:`RegistrationOutcome`: a gate rejection
    (lint_reject / rewrite_proposed / structural) is caught and reported as a
    non-registering code rather than raised, so :func:`run_stage_b` can record
    it as telemetry and continue the batch (R19). A real fault (e.g. a missing
    judge/NLI fixture) is NOT caught and propagates. Imported lazily so unit tests
    that inject a fake registrar never pull in the embedding stack.
    """
    from agent_families.pipeline import (
        RegistrationRejected,
        StructuralValidationError,
        add_idea,
    )

    def _register(idea: RegisteredIdea) -> RegistrationOutcome:
        try:
            result = add_idea(
                store,
                vec,
                embedder,
                config,
                precondition=idea.precondition,
                action=idea.action,
                expected_outcome=idea.expected_outcome,
                batch_label=idea.batch_label,
                scope_tag=idea.scope_tag,
                accept_rewrite=accept_rewrite,
                provenance=provenance,
                corroborate_mode=corroborate_mode,
                judge_mode=judge_mode,
                judge_fixtures_dir=judge_fixtures_dir,
                nli_model=nli_model,
                nli_mode=nli_mode,
                nli_fixtures_dir=nli_fixtures_dir,
            )
        except (RegistrationRejected, StructuralValidationError) as exc:
            return RegistrationOutcome(
                insight_id=None,
                code=_REJECTION_CODE_BY_EXC.get(type(exc).__name__, "rejected"),
                outcome=None,
                detail=str(exc),
            )
        return RegistrationOutcome(
            insight_id=result.insight_id,
            code=result.code,
            outcome=result.judge_outcome,
            detail=result.message,
        )

    return _register


def batch_label_for(episode_id: int) -> str:
    """The deterministic batch label for an episode's reflection batch (R10)."""
    return f"reflect-ep{episode_id}"


def run_stage_b(
    store: Store,
    stage_a_result: StageAResult,
    *,
    register_fn: RegisterFn,
    episode_id: int | None = None,
    mode: str = "training",
    batch_under_trial_id: int | None = None,
    idea_budget: int = DEFAULT_IDEA_BUDGET,
    override_confidence_threshold: float = DEFAULT_OVERRIDE_CONFIDENCE_THRESHOLD,
    judge_fn: JudgeFn | None = None,
    judge_model: str = "sonnet",
    judge_max_retries: int = 0,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
) -> StageBResult:
    """Cluster -> reflect -> validate -> rank -> register, returning the batch.

    Consumes Stage A's idea candidates (instrument-health attributions are already
    sunk). Every surviving lesson registers through ``register_fn`` under one batch
    label; an empty batch (no failures, or every cluster no-lesson) settles legally
    with a telemetry row and registers nothing (R11).
    """
    episode_id = episode_id if episode_id is not None else stage_a_result.episode_id
    candidates = stage_a_result.idea_candidates
    clusters = cluster_failures(store, candidates)

    no_lesson_ids: list[str] = []
    telemetry_ids: list[int] = []
    lessons: list[ValidatedLesson] = []
    for cluster in clusters:
        reflection = reflect_cluster(
            store,
            cluster,
            judge_fn=judge_fn,
            judge_model=judge_model,
            judge_max_retries=judge_max_retries,
            judge_mode=judge_mode,
            judge_fixtures_dir=judge_fixtures_dir,
        )
        if not reflection.has_lesson:
            no_lesson_ids.append(cluster.cluster_id)
            telemetry_ids.append(
                _write_telemetry(
                    store,
                    episode_id,
                    NO_LESSON_KIND,
                    {
                        "cluster_id": cluster.cluster_id,
                        "feat_id": cluster.feat_id,
                        "scenarios": list(cluster.scen_ids),
                    },
                )
            )
            continue
        lessons.append(
            validate_reflection(
                store,
                reflection,
                episode_id=episode_id,
                mode=mode,
                batch_under_trial_id=batch_under_trial_id,
                override_confidence_threshold=override_confidence_threshold,
            )
        )

    selected, dropped = rank_and_select(lessons, idea_budget)
    for lesson in dropped:
        telemetry_ids.append(
            _write_telemetry(
                store,
                episode_id,
                OVER_BUDGET_KIND,
                {
                    "cluster_id": lesson.cluster.cluster_id,
                    "feat_id": lesson.cluster.feat_id,
                    "must_tier": lesson.cluster.must_tier,
                    "size": lesson.cluster.size,
                    "confidence": lesson.confidence,
                },
            )
        )

    if not selected:
        telemetry_ids.append(
            _write_telemetry(
                store,
                episode_id,
                EMPTY_BATCH_KIND,
                {"clusters": [c.cluster_id for c in clusters]},
            )
        )
        return StageBResult(
            episode_id=episode_id,
            batch_label=None,
            registered=(),
            no_lesson_cluster_ids=tuple(no_lesson_ids),
            dropped_cluster_ids=tuple(l.cluster.cluster_id for l in dropped),
            telemetry_ids=tuple(telemetry_ids),
        )

    label = batch_label_for(episode_id)
    registered: list[InsightProvenance] = []
    corroborated_ids: list[int] = []
    gate_rejected_ids: list[str] = []
    for lesson in selected:
        idea = RegisteredIdea(
            precondition=lesson.fields["precondition"],
            action=lesson.fields["action"],
            expected_outcome=lesson.fields["expected_outcome"],
            scope_tag=lesson.scope_tag,
            batch_label=label,
            cluster_id=lesson.cluster.cluster_id,
            feat_id=lesson.cluster.feat_id,
            scen_ids=lesson.cluster.scen_ids,
            effective_role=lesson.effective_role,
            confidence=lesson.confidence,
        )
        outcome = register_fn(idea)

        if outcome.is_gate_rejection:
            # R19: the registration gate declined this lesson (lint_reject /
            # rewrite_proposed / structural). Capture it as telemetry and carry on
            # — one bad lesson never aborts the batch.
            gate_rejected_ids.append(lesson.cluster.cluster_id)
            telemetry_ids.append(
                _write_telemetry(
                    store,
                    episode_id,
                    GATE_REJECTED_KIND,
                    {
                        "cluster_id": lesson.cluster.cluster_id,
                        "feat_id": lesson.cluster.feat_id,
                        "code": outcome.code,
                        "detail": outcome.detail,
                    },
                )
            )
            continue

        if outcome.code == "corroborated":
            # R19: the lesson duplicated an existing insight — a corroboration vote,
            # not a new registration. The vote already rode add_idea_r3's single
            # `corroborate` fitness-event counter (the §11 recurrence substrate);
            # stage_b adds NO separate tally, only this naming telemetry row.
            if outcome.insight_id is not None:
                corroborated_ids.append(outcome.insight_id)
            telemetry_ids.append(
                _write_telemetry(
                    store,
                    episode_id,
                    CORROBORATE_KIND,
                    {
                        "cluster_id": lesson.cluster.cluster_id,
                        "feat_id": lesson.cluster.feat_id,
                        "incumbent_insight_id": outcome.insight_id,
                    },
                )
            )
            continue

        # Registered (incl. a deferred-supersede that wrote a `contradicts` edge):
        # a fresh quarantined insight landed; its move rides home in the provenance.
        registered.append(
            InsightProvenance(
                insight_id=outcome.insight_id,
                cluster_id=lesson.cluster.cluster_id,
                feat_id=lesson.cluster.feat_id,
                scen_ids=lesson.cluster.scen_ids,
                effective_role=lesson.effective_role,
                override_applied=lesson.override_applied,
                implicated_existing_insights=(
                    lesson.reflection.implicated_existing_insights
                ),
                scope_tag=lesson.scope_tag,
                confidence=lesson.confidence,
                judge_outcome=outcome.outcome,
            )
        )

    return StageBResult(
        episode_id=episode_id,
        batch_label=label,
        registered=tuple(registered),
        no_lesson_cluster_ids=tuple(no_lesson_ids),
        dropped_cluster_ids=tuple(l.cluster.cluster_id for l in dropped),
        telemetry_ids=tuple(telemetry_ids),
        corroborated_incumbent_ids=tuple(corroborated_ids),
        gate_rejected_cluster_ids=tuple(gate_rejected_ids),
    )
