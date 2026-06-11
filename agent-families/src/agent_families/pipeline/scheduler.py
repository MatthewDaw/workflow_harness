"""Parallel episodes and batch merging (plan-005 U5, R15-R17).

Wall-clock scale within quota reality. The §15 invariants were enforced from
Phase 0 precisely so this plan adds only a SCHEDULER and the compose namespacing
— no redesign:

- **R15 (parallel episodes)** — :class:`EpisodeScheduler` admits up to N
  episodes concurrently with quota-aware admission, holding **at most one
  in-flight episode per target** (the frontier ledger, mention-coverage audit,
  persistent clone, and target container state are all per-target serial). Each
  episode runs against the library snapshot captured at fan-out time and the
  library stays **read-only for the whole fan-out** — the scheduler re-reads the
  snapshot id afterwards and refuses to return if it moved (a write escaped the
  single-writer discipline). Per-episode environment isolation is the
  :func:`~agent_families.grading.target_env.episode_namespace` seam (compose
  project + disjoint host ports) the scheduler hands each episode.

- **R16 (batch merging)** — :func:`merge_batches` implements the decided policy:
  validate each batch independently against the shared snapshot; sort the
  survivors by a **canonical key (episode id)**; merge them **through
  registration** (the add_idea cosine prefilter / judge / ratchet absorb
  cross-batch overlap — a near-duplicate planted by a second episode is merged,
  not double-counted); run **one joint confirmation on the union** before
  promotion; promote on pass, leave quarantined on fail (the joint-confirmation
  failure log is the interaction-effect telemetry); **per-batch revert**.

- **R17 (parallel validation)** — validation is read-only against library
  variants, so :func:`parallel_map` runs the independent per-batch validations
  (and any other read-only validation fan-out) concurrently; the validation gate
  parallelises first because it is the throughput bottleneck.

Determinism (the verification): registration is judge-mediated dedup, so the
surviving canonical row is a function of registration ORDER. True
order-independence is therefore unachievable — the fix is the **canonical sort**
(by episode id) applied before registration. A parallel run and a sequential run
that feed the same validated batches through the same canonical order leave
**identical merge state** (:func:`merge_state_digest`, which projects away
timestamps and raw ids so the comparison is on the surviving content).

Offline by construction: the episode body, the registration step, and the joint
confirmation are all injected callables, so the scheduler and merge logic run
the full decision table with zero quota and no ``claude`` on PATH. The real
bindings (a Phase 2 episode, ``pipeline.add_idea``, a benchmark confirmation
episode) land in the U7 run assembly.

## Conformance

Test-scenario / invariant (plan-005 U5) -> test (in ``tests/test_scheduler.py``):

- two concurrent episodes never contend on the library (read-only verified) and
  serialize at the queue: ``test_concurrent_episodes_are_read_only_on_library``
- at most one in-flight episode per target (per-target serial):
  ``test_same_target_episodes_serialize``
- quota-aware admission throttles concurrency:
  ``test_quota_aware_admission_throttles``
- port/compose namespaces disjoint: ``test_episode_namespaces_are_disjoint``
- two batches merge with the prefilter absorbing a planted near-duplicate:
  ``test_merge_absorbs_planted_near_duplicate``
- joint-confirmation failure blocks promotion and records the telemetry:
  ``test_joint_confirmation_failure_blocks_promotion``
- per-batch revert after a merged promotion removes exactly one batch's
  insights: ``test_per_batch_revert_removes_one_batch``
- a batch that fails independent validation never reaches registration:
  ``test_independent_validation_drops_a_batch``
- validation runs execute concurrently: ``test_parallel_validation_runs_concurrently``
- canonical-order determinism (parallel == sequential post-merge state):
  ``test_canonical_order_determinism``
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from agent_families import lifecycle
from agent_families.grading.target_env import EpisodeNamespace, episode_namespace
from agent_families.pipeline import AddIdeaResult
from agent_families.reflector.validate import (
    BenchmarkOutcome,
    ReplayResult,
    ValidateParams,
    active_batch_insight_ids,
    decide_validation,
    is_bootstrap,
)
from agent_families.store import Store

logger = logging.getLogger(__name__)

# The meta key under which joint-confirmation outcomes accumulate. The
# interaction-effect telemetry (R16) is the failure RATE over this log.
JOINT_CONFIRM_LOG_KEY = "scheduler:joint_confirm_log"


class SchedulerError(Exception):
    """A broken scheduler/merge precondition or invariant, with a reason."""


# --- episode scheduler (R15) ----------------------------------------------------


@dataclass(frozen=True)
class SchedulerParams:
    """The scheduler's governing values, caller-supplied (no hidden config).

    ``max_concurrent`` is the hard concurrency ceiling. PROVENANCE: plan-005
    Risks — "2–3 concurrent episodes is the expected ceiling on one Max
    subscription"; the quota-aware admission callback tightens it dynamically.
    """

    max_concurrent: int = 2

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise SchedulerError(
                f"max_concurrent must be >= 1, got {self.max_concurrent}"
            )


@dataclass(frozen=True)
class EpisodeSpec:
    """One episode to schedule: its id, its target, and an opaque payload.

    ``episode_id`` is the canonical key (also the namespace seed); ``target`` is
    the per-target serialization key (at most one in-flight per target).
    """

    episode_id: int
    target: str
    payload: object = None

    @property
    def namespace(self) -> EpisodeNamespace:
        """The episode's disjoint stack identity (compose project + ports)."""
        return episode_namespace(self.episode_id)


@dataclass(frozen=True)
class EpisodeRun:
    """One scheduled episode's result: the spec, its outcome, and its namespace."""

    spec: EpisodeSpec
    outcome: object
    namespace: EpisodeNamespace


@dataclass(frozen=True)
class SchedulerResult:
    """A fan-out's outcome plus the invariants it observed."""

    runs: tuple[EpisodeRun, ...]
    library_snapshot_id: int
    peak_concurrency: int
    max_concurrent_per_target: int

    @property
    def outcomes(self) -> tuple[object, ...]:
        return tuple(r.outcome for r in self.runs)


# The episode body: given a spec (carrying its namespace), produce an outcome.
EpisodeRunner = Callable[[EpisodeSpec], object]
# Quota-aware admission: given the current in-flight count, may one more start?
QuotaFn = Callable[[int], bool]


class EpisodeScheduler:
    """Admits and runs episodes concurrently under the §15 invariants (R15)."""

    def __init__(
        self, params: SchedulerParams, *, quota_fn: QuotaFn | None = None
    ) -> None:
        self.params = params
        self.quota_fn = quota_fn

    def run(
        self, store: Store, specs: Sequence[EpisodeSpec], runner: EpisodeRunner
    ) -> SchedulerResult:
        """Run every episode, honouring concurrency, per-target serial, and the
        read-only-library invariant.

        Episodes are submitted in id order but admitted only while a slot is
        free, the quota callback (if any) allows it, and no other episode on the
        same target is in flight. The library snapshot is captured before
        fan-out and re-checked after: a moved snapshot means a write escaped the
        single-writer queue mid-fan-out, which is an invariant violation.
        """
        specs = list(specs)
        for spec in specs:
            spec.namespace  # validate every namespace up front (raises on bad id)

        frozen_snapshot = store.current_snapshot_id()
        results: list[EpisodeRun | None] = [None] * len(specs)

        lock = threading.Lock()
        in_flight_targets: dict[str, int] = {}
        concurrency = 0
        peak = 0
        peak_per_target = 0

        def body(idx: int, spec: EpisodeSpec) -> object:
            nonlocal concurrency, peak, peak_per_target
            with lock:
                concurrency += 1
                peak = max(peak, concurrency)
                count = in_flight_targets.get(spec.target, 0) + 1
                in_flight_targets[spec.target] = count
                peak_per_target = max(peak_per_target, count)
            try:
                return runner(spec)
            finally:
                with lock:
                    concurrency -= 1
                    remaining = in_flight_targets.get(spec.target, 1) - 1
                    if remaining <= 0:
                        in_flight_targets.pop(spec.target, None)
                    else:
                        in_flight_targets[spec.target] = remaining

        pending = list(range(len(specs)))
        futures: dict[object, int] = {}
        with ThreadPoolExecutor(max_workers=self.params.max_concurrent) as ex:
            while pending or futures:
                progressed = False
                i = 0
                while i < len(pending):
                    idx = pending[i]
                    spec = specs[idx]
                    with lock:
                        cur = len(futures)
                        quota_ok = self.quota_fn(cur) if self.quota_fn else True
                        target_busy = spec.target in {
                            specs[j].target for j in futures.values()
                        }
                        can_admit = (
                            cur < self.params.max_concurrent
                            and quota_ok
                            and not target_busy
                        )
                    if can_admit:
                        fut = ex.submit(body, idx, spec)
                        futures[fut] = idx
                        pending.pop(i)
                        progressed = True
                    else:
                        i += 1
                if futures:
                    done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                    for fut in done:
                        idx = futures.pop(fut)
                        spec = specs[idx]
                        results[idx] = EpisodeRun(
                            spec=spec,
                            outcome=fut.result(),
                            namespace=spec.namespace,
                        )
                elif pending and not progressed:
                    raise SchedulerError(
                        "quota-aware admission cannot make progress: nothing is"
                        " in flight yet no pending episode may start (the quota"
                        " callback is denying admission at zero concurrency)"
                    )

        after = store.current_snapshot_id()
        if after != frozen_snapshot:
            raise SchedulerError(
                f"library was mutated during the episode fan-out (snapshot"
                f" {frozen_snapshot} -> {after}); episodes must hold the library"
                " read-only and route every write through the single-writer"
                " promotion queue (R15)"
            )

        runs = tuple(r for r in results if r is not None)
        logger.info(
            "scheduled %d episode(s): peak concurrency %d, peak per-target %d,"
            " library snapshot %d held",
            len(runs),
            peak,
            peak_per_target,
            frozen_snapshot,
        )
        return SchedulerResult(
            runs=runs,
            library_snapshot_id=frozen_snapshot,
            peak_concurrency=peak,
            max_concurrent_per_target=peak_per_target,
        )


# --- parallel read-only fan-out (R17) -------------------------------------------


def parallel_map(
    fn: Callable[[object], object],
    items: Sequence[object],
    *,
    max_workers: int = 4,
) -> list[object]:
    """Run ``fn`` over ``items`` concurrently, returning results in input order.

    Validation runs are read-only against library variants (R17), so they
    parallelise freely; this is the primitive the independent per-batch
    validation and any other read-only validation fan-out share.
    """
    items = list(items)
    if not items:
        return []
    workers = max(1, min(max_workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


# --- batch merging (R16) --------------------------------------------------------


@dataclass(frozen=True)
class CandidateIdea:
    """One reflection a batch proposes for registration (structural template)."""

    precondition: str
    action: str
    expected_outcome: str


@dataclass(frozen=True)
class EpisodeBatch:
    """A parallel episode's candidate batch, with its independent-validation
    inputs (the benchmark measured against the shared snapshot, R16)."""

    episode_id: int
    label: str
    ideas: tuple[CandidateIdea, ...]
    benchmark: BenchmarkOutcome
    n_replay_pairs: int
    replay: ReplayResult | None = None


@dataclass(frozen=True)
class JointConfirm:
    """The one joint confirmation run on the union before promotion (R16)."""

    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RegistrationRecord:
    """One idea's registration outcome (``code`` from :class:`AddIdeaResult`).

    ``merged`` / ``exact_duplicate`` / ``previously_merged`` mean the cosine
    prefilter / judge absorbed the idea (cross-batch overlap); ``registered``
    means a fresh quarantined insight landed.
    """

    label: str
    idea_index: int
    code: str
    insight_id: int


@dataclass(frozen=True)
class MergeResult:
    """The batch merge's outcome and the telemetry that explains it."""

    canonical_order: tuple[int, ...]
    validated_labels: tuple[str, ...]
    dropped_labels: tuple[str, ...]
    registrations: tuple[RegistrationRecord, ...]
    joint_confirm: JointConfirm
    promoted: bool
    promoted_labels: tuple[str, ...]
    active_insight_ids: tuple[int, ...]


# Registration seam: register one idea into a batch label, returning the
# add_idea result (the U7 binding wraps ``pipeline.add_idea`` with its
# store/vec/embedder/config). The cosine prefilter lives inside it.
RegisterFn = Callable[[CandidateIdea, str], AddIdeaResult]
# Joint-confirmation seam: confirm the union of validated batch labels.
JointConfirmFn = Callable[[tuple[str, ...]], JointConfirm]


def _independent_decision(batch: EpisodeBatch, params: ValidateParams):
    bootstrap = is_bootstrap(batch.benchmark.n_points, batch.n_replay_pairs, params)
    return decide_validation(
        batch.benchmark, bootstrap=bootstrap, params=params, replay=batch.replay
    )


def _record_joint_confirm(
    store: Store, snapshot_id: int, labels: tuple[str, ...], jc: JointConfirm
) -> None:
    """Append the joint-confirmation outcome to the telemetry log (R16)."""
    raw = store.get_meta(JOINT_CONFIRM_LOG_KEY)
    log = json.loads(raw) if raw else []
    log.append(
        {
            "snapshot_id": int(snapshot_id),
            "labels": list(labels),
            "passed": bool(jc.passed),
            "detail": jc.detail,
        }
    )
    store.set_meta(
        JOINT_CONFIRM_LOG_KEY,
        json.dumps(log, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    )


def _batch_id(store: Store, label: str) -> int:
    row = store.conn.execute(
        "SELECT id FROM batches WHERE label = ?", (label,)
    ).fetchone()
    if row is None:
        raise SchedulerError(f"unknown batch '{label}'")
    return row["id"]


def joint_confirm_failure_rate(store: Store) -> float:
    """The interaction-effect telemetry: joint-confirmation failures / merges."""
    raw = store.get_meta(JOINT_CONFIRM_LOG_KEY)
    log = json.loads(raw) if raw else []
    if not log:
        return 0.0
    fails = sum(1 for e in log if not e["passed"])
    return fails / len(log)


def merge_batches(
    batches: Sequence[EpisodeBatch],
    *,
    store: Store,
    shared_snapshot_id: int,
    register: RegisterFn,
    joint_confirm: JointConfirmFn,
    params: ValidateParams = ValidateParams(),
    validate_workers: int = 4,
) -> MergeResult:
    """Validate, merge, joint-confirm, and promote a set of parallel-episode
    batches against one shared snapshot (R16).

    Order of operations is load-bearing: independent validation (read-only,
    parallelised per R17) decides which batches survive; the survivors are
    sorted by **episode id** (the canonical key) so registration order is
    deterministic; registration absorbs cross-batch overlap; the joint
    confirmation gates the union's promotion. A failed joint confirmation leaves
    every batch quarantined (default deny) and is logged as interaction-effect
    telemetry. Promotion and revert flow through the single-writer queue.
    """
    batches = list(batches)
    if not batches:
        raise SchedulerError("merge_batches needs at least one batch")
    labels = [b.label for b in batches]
    if len(set(labels)) != len(labels):
        raise SchedulerError(f"batch labels must be unique, got {labels}")

    # Independent validation against the shared snapshot, parallelised (R17).
    decisions = parallel_map(
        lambda b: _independent_decision(b, params),
        batches,
        max_workers=validate_workers,
    )
    decided = list(zip(batches, decisions))

    # Canonical merge order: sort the survivors by episode id (the determinism
    # fix — registration's surviving row is a function of this order).
    validated = sorted(
        (b for b, d in decided if d.promotes), key=lambda b: b.episode_id
    )
    dropped = tuple(
        b.label for b, d in sorted(decided, key=lambda bd: bd[0].episode_id)
        if not d.promotes
    )
    canonical_order = tuple(b.episode_id for b in validated)

    # Merge-all through registration (the cosine prefilter absorbs overlap).
    registrations: list[RegistrationRecord] = []
    for b in validated:
        for i, idea in enumerate(b.ideas):
            res = register(idea, b.label)
            registrations.append(
                RegistrationRecord(
                    label=b.label,
                    idea_index=i,
                    code=res.code,
                    insight_id=res.insight_id,
                )
            )

    validated_labels = tuple(b.label for b in validated)

    # One joint confirmation run on the union before promotion (R16).
    jc = joint_confirm(validated_labels)
    _record_joint_confirm(store, shared_snapshot_id, validated_labels, jc)

    promoted_labels: list[str] = []
    if jc.passed:
        for b in validated:
            try:
                lifecycle.promote_batch(store, b.label)
            except lifecycle.LifecycleError:
                # Every idea was absorbed by the prefilter — nothing quarantined
                # to promote. Not a failure: the batch contributed only merges.
                logger.info(
                    "batch '%s' had no quarantined insights to promote (fully"
                    " absorbed by registration)",
                    b.label,
                )
                continue
            promoted_labels.append(b.label)
            store.insert_batch_validation(
                _batch_id(store, b.label),
                snapshot_id=shared_snapshot_id,
                verdict="promote",
                detail=f"joint-confirm passed on union {validated_labels}",
            )
    else:
        # Default deny: the union stays quarantined; record the open verdict so
        # the block is auditable, plus the interaction-effect telemetry above.
        for b in validated:
            store.insert_batch_validation(
                _batch_id(store, b.label),
                snapshot_id=shared_snapshot_id,
                detail=(
                    f"joint-confirm FAILED on union {validated_labels}; promotion"
                    f" blocked (interaction effect): {jc.detail}"
                ),
            )

    active = _active_ids_for_labels(store, promoted_labels)
    logger.info(
        "merge: %d validated, %d dropped, joint-confirm %s, %d promoted",
        len(validated_labels),
        len(dropped),
        "pass" if jc.passed else "FAIL",
        len(promoted_labels),
    )
    return MergeResult(
        canonical_order=canonical_order,
        validated_labels=validated_labels,
        dropped_labels=dropped,
        registrations=tuple(registrations),
        joint_confirm=jc,
        promoted=jc.passed and bool(promoted_labels),
        promoted_labels=tuple(promoted_labels),
        active_insight_ids=active,
    )


def revert_one_batch(store: Store, batch_label: str) -> tuple[int, ...]:
    """Per-batch revert (R16): retire exactly one merged batch's insights.

    Because cross-batch overlap was absorbed as merge-log rows (not duplicate
    insights), reverting one batch touches only that batch's own insights — the
    others stay active. Returns the reverted insight ids.
    """
    result = lifecycle.revert_batch(store, batch_label)
    return result.insight_ids


def _active_ids_for_labels(store: Store, labels: Sequence[str]) -> tuple[int, ...]:
    ids: list[int] = []
    for label in labels:
        ids.extend(active_batch_insight_ids(store, label))
    return tuple(sorted(ids))


# --- determinism digest (the verification) --------------------------------------


def merge_state_digest(store: Store) -> str:
    """A canonical projection of the post-merge library state (the verification).

    Projects away timestamps and raw ids — keying everything by content hash —
    so a parallel run and a sequential run that registered the same validated
    batches in the same canonical order produce an IDENTICAL digest. The
    surviving canonical row depends on registration order; the canonical sort is
    what makes the two runs agree (true order-independence is unachievable under
    judge-mediated dedup, per the plan).
    """
    id_to_hash = {
        row["id"]: row["content_hash"]
        for row in store.conn.execute(
            "SELECT id, content_hash FROM insights"
        ).fetchall()
    }

    def batch_label(batch_id: int | None) -> str | None:
        if batch_id is None:
            return None
        row = store.conn.execute(
            "SELECT label FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        return row["label"] if row else None

    insights = sorted(
        (
            {
                "content_hash": r["content_hash"],
                "precondition": r["precondition"],
                "action": r["action"],
                "expected_outcome": r["expected_outcome"],
                "scope_tag": r["scope_tag"],
                "status": r["status"],
                "batch": batch_label(r["batch_id"]),
                "supersedes": id_to_hash.get(r["supersedes"]),
            }
            for r in store.conn.execute("SELECT * FROM insights").fetchall()
        ),
        key=lambda d: d["content_hash"],
    )
    merges = sorted(
        (
            {
                "content_hash": r["content_hash"],
                "duplicate_of": id_to_hash.get(r["duplicate_of"]),
                "batch": batch_label(r["batch_id"]),
            }
            for r in store.conn.execute("SELECT * FROM merge_log").fetchall()
        ),
        key=lambda d: (d["content_hash"], d["duplicate_of"] or ""),
    )
    skills = []
    for s in store.conn.execute(
        "SELECT id, agent_id, name FROM skills"
    ).fetchall():
        members = [
            id_to_hash[m["insight_id"]]
            for m in store.conn.execute(
                "SELECT insight_id FROM skill_members WHERE skill_id = ?"
                " ORDER BY position ASC",
                (s["id"],),
            ).fetchall()
        ]
        skills.append(
            {"agent_id": s["agent_id"], "name": s["name"], "members": members}
        )
    skills.sort(key=lambda d: (d["agent_id"], d["name"]))

    return json.dumps(
        {"insights": insights, "merge_log": merges, "skills": skills},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
