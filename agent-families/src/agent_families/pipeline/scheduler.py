"""Parallel episodes and batch merging: wall-clock scale within quota (plan-005 U5).

This is the §15 scheduler — the deliberately small change earlier phases were
seamed for. Plans 0-4 enforced the invariants that make parallelism a scheduler
problem rather than a redesign (DESIGN §15): the library is **read-only during
episodes**, every write goes through the **single-writer promotion queue**,
mutations are **snapshot-keyed**, and the target stack is **env-isolatable**. This
module only adds (a) an admission scheduler that runs N episodes at once and (b)
the batch-merge policy that folds their reflections into one library.

The pieces, in the order a training wave uses them:

1. **Parallel episodes** (R15) — :func:`run_episodes` admits up to ``max_concurrent``
   episodes against ONE shared library snapshot, **at most one in-flight episode
   per target** (the frontier ledger / persistent clone / target container are
   per-target serial), each in its own environment namespace (compose project +
   port window, ``grading.target_env.episode_namespace`` — the Phase 1/2 seam).
   Episodes see the library only through a frozen :class:`LibrarySnapshotView`
   (read-only by construction); the scheduler asserts the snapshot did not move
   while they ran. Admission is **quota-aware**: episodes whose estimated cost
   would exceed the remaining budget are deferred to a later wave (2-3 concurrent
   is the expected ceiling on one Max subscription).

2. **Batch merging** (R16, the decided policy) — :func:`merge_batches` takes the
   batches that **independently** passed validation against the shared snapshot
   (Plan 4 U7's gate is the per-batch verdict), sorts them by the **canonical key
   (episode id)**, folds them through registration so the **cosine/judge prefilter
   absorbs cross-batch overlap** (a planted near-duplicate is merge-logged, not
   double-counted), runs **one joint confirmation on the union**, and only then
   promotes — all writes through the single-writer queue. A joint-confirmation
   failure (an interaction the per-batch validations could not see) **blocks the
   promotion** and records the interaction as telemetry; **revert is per-batch**.

3. **Parallel validation** (R17) — :func:`run_validations` runs validation tasks
   (trials, benchmarks, replay re-judging, mutation audits) concurrently; they are
   read-only against library variants, so they parallelize freely and are the
   throughput bottleneck that parallelizes first.

Determinism (the U5 verification): true order-independence is unachievable —
judge-mediated dedup makes the surviving canonical row a function of registration
order. The fix is the **canonical sort**: validated batches sort by episode id
before registration, so a parallel run (batches completing in any order) and a
sequential run over the same shared snapshot produce a **byte-identical post-merge
state**. :meth:`MergeResult.canonical_digest` is the row-id-independent witness of
that state (it hashes content hashes and verdicts, never autoincrement ids).

Offline by construction: episodes, the overlap prefilter, the joint confirmation,
and validation tasks are all injected callables. The suite drives scripted fakes —
zero quota, no ``claude`` on PATH, no docker. The promote/revert/merge-log paths
drive the real store queue.

## Conformance

Test-scenario / invariant (plan-005 U5) -> test (in ``tests/test_scheduler.py``):

- two concurrent episodes never contend on the library (read-only) and serialize
  at the queue: ``test_two_episodes_run_concurrently_library_read_only``
- per-target serialization (at most one in-flight per target):
  ``test_same_target_episodes_serialize``
- port/compose namespaces disjoint: ``test_episode_namespaces_are_disjoint``
- quota-aware admission defers over-budget episodes:
  ``test_quota_aware_admission_defers_episodes``
- two batches merge with the prefilter absorbing a planted near-duplicate:
  ``test_merge_absorbs_planted_near_duplicate``
- joint-confirmation failure blocks promotion and records telemetry:
  ``test_joint_confirmation_failure_blocks_and_records_telemetry``
- per-batch revert after a merged promotion removes exactly one batch's insights:
  ``test_per_batch_revert_removes_exactly_one_batch``
- validation runs execute concurrently: ``test_validations_run_concurrently``
- canonical merge order -> byte-identical state regardless of completion order:
  ``test_canonical_merge_order_is_deterministic``
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent_families import lifecycle
from agent_families.grading.target_env import (
    DEFAULT_COMPOSE_PROJECT,
    DEFAULT_PORT_STRIDE,
    PORT_TABLE,
    EpisodeNamespace,
    assert_namespaces_disjoint,
    episode_namespace,
)
from agent_families.store import Store

logger = logging.getLogger(__name__)

# review_queue kind for the R16 interaction-effect telemetry a blocked
# joint-confirmation writes — the interaction the independent per-batch
# validations could not see, surfaced for the human reflector.
JOINT_CONFIRM_FAIL_KIND = "batch_merge_interaction"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SchedulerError(Exception):
    """A broken scheduler precondition with an actionable message."""


# --- configuration (caller-supplied, carried defaults; PROVENANCE per §17) ------

# PROVENANCE: R15 / DESIGN §15 — "expect 2-3 concurrent before token-bound" on one
# Max subscription. TUNING METRIC: quota-exhaustion rate vs. wall-clock throughput;
# re-measured against the Agent SDK credit envelope (Plans 1-4 Risks).
DEFAULT_MAX_CONCURRENT = 2


@dataclass(frozen=True)
class SchedulerConfig:
    """The parallel scheduler's governing values (caller-routed from thresholds).

    Tunables are NOT read from config here — they ride in this object, per the
    EpisodeConfig / ValidateParams seam precedent; the carried defaults record
    PROVENANCE and the run-assembly wiring routes the live values.
    """

    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    port_stride: int = DEFAULT_PORT_STRIDE
    base_project: str = DEFAULT_COMPOSE_PROJECT
    quota_budget_usd: float | None = None

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise SchedulerError(
                f"max_concurrent must be >= 1, got {self.max_concurrent}"
            )
        if self.port_stride <= 0:
            raise SchedulerError(
                f"port_stride must be positive, got {self.port_stride}"
            )
        if self.quota_budget_usd is not None and self.quota_budget_usd < 0:
            raise SchedulerError(
                f"quota_budget_usd must be >= 0 or None, got {self.quota_budget_usd}"
            )


# --- the read-only library view (R15) -------------------------------------------


@dataclass(frozen=True)
class ActiveInsight:
    """One immutable row of the library snapshot an episode may read (R15)."""

    insight_id: int
    content_hash: str
    precondition: str
    action: str
    expected_outcome: str
    scope_tag: str | None
    batch_id: int | None


class LibrarySnapshotView:
    """A frozen, read-only view of the active library at one snapshot (R15).

    Episodes receive this — never the :class:`Store` — so a parallel wave
    structurally cannot mutate the library mid-flight. The view holds no store
    handle and exposes no write path; the scheduler additionally asserts the
    snapshot id did not move across the concurrent phase (the empirical read-only
    check).
    """

    __slots__ = ("_snapshot_id", "_active")

    def __init__(self, snapshot_id: int, active: Sequence[ActiveInsight]) -> None:
        self._snapshot_id = int(snapshot_id)
        self._active = tuple(active)

    @property
    def snapshot_id(self) -> int:
        return self._snapshot_id

    def active_insights(self) -> tuple[ActiveInsight, ...]:
        return self._active

    def __len__(self) -> int:
        return len(self._active)

    @classmethod
    def capture(cls, store: Store) -> LibrarySnapshotView:
        """Snapshot the current active set into an immutable view."""
        snapshot_id = store.current_snapshot_id()
        rows = store.conn.execute(
            "SELECT id, content_hash, precondition, action, expected_outcome,"
            " scope_tag, batch_id FROM insights WHERE status = 'active'"
            " ORDER BY id ASC"
        ).fetchall()
        active = [
            ActiveInsight(
                insight_id=r["id"],
                content_hash=r["content_hash"],
                precondition=r["precondition"],
                action=r["action"],
                expected_outcome=r["expected_outcome"],
                scope_tag=r["scope_tag"],
                batch_id=r["batch_id"],
            )
            for r in rows
        ]
        return cls(snapshot_id, active)


# --- parallel episodes (R15) ----------------------------------------------------


@dataclass(frozen=True)
class EpisodeSpec:
    """One episode the scheduler may admit: a target plus its admission cost."""

    episode_id: int
    target: str
    est_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if not self.target.strip():
            raise SchedulerError("EpisodeSpec.target must be non-empty")
        if self.est_cost_usd < 0:
            raise SchedulerError(
                f"EpisodeSpec.est_cost_usd must be >= 0, got {self.est_cost_usd}"
            )


@dataclass(frozen=True)
class EpisodeRunContext:
    """What an admitted episode's work function receives.

    ``library`` is the read-only snapshot view (R15); ``namespace`` is this
    episode's disjoint compose-project + port window; ``slot`` is the reusable
    lane index two concurrent episodes never share.
    """

    spec: EpisodeSpec
    slot: int
    namespace: EpisodeNamespace
    library: LibrarySnapshotView


# A work function turns a run context into whatever per-episode product the caller
# wants carried back (Plan 4's Stage B batch handle, in the live wiring). It must
# touch the library only through ``ctx.library`` — never the store (R15).
EpisodeWorkFn = Callable[[EpisodeRunContext], object]


@dataclass(frozen=True)
class ParallelEpisodeResult:
    """The outcome of one parallel wave."""

    products: tuple[object, ...]  # one per admitted spec, in admitted order
    admitted: tuple[EpisodeSpec, ...]
    deferred: tuple[EpisodeSpec, ...]
    peak_concurrency: int
    namespaces: tuple[EpisodeNamespace, ...]
    snapshot_id: int


def _admit_within_budget(
    specs: Sequence[EpisodeSpec], budget: float | None
) -> tuple[list[EpisodeSpec], list[EpisodeSpec]]:
    """Greedy quota-aware admission (R15): admit specs in order while the
    cumulative estimated cost stays within budget; defer the rest. ``budget=None``
    admits everything (cost-unbounded)."""
    if budget is None:
        return list(specs), []
    admitted: list[EpisodeSpec] = []
    deferred: list[EpisodeSpec] = []
    spent = 0.0
    for spec in specs:
        if deferred:
            # Once one spec is deferred, preserve order: everything after waits
            # too (a cheaper later spec must not jump the queue).
            deferred.append(spec)
            continue
        if spent + spec.est_cost_usd <= budget:
            admitted.append(spec)
            spent += spec.est_cost_usd
        else:
            deferred.append(spec)
    return admitted, deferred


def run_episodes(
    store: Store,
    cfg: SchedulerConfig,
    specs: Sequence[EpisodeSpec],
    work_fn: EpisodeWorkFn,
) -> ParallelEpisodeResult:
    """Run a parallel wave of episodes against one shared library snapshot (R15).

    Admission is quota-aware (over-budget specs deferred), concurrency is capped at
    ``cfg.max_concurrent``, and **at most one episode per target runs at a time**
    (per-target serial). Each admitted episode gets a disjoint environment
    namespace and the read-only library view. The library snapshot must not move
    while episodes run (the single-writer invariant) — a violation raises.
    """
    library = LibrarySnapshotView.capture(store)
    admitted, deferred = _admit_within_budget(specs, cfg.quota_budget_usd)

    namespaces = [
        episode_namespace(
            slot,
            base_project=cfg.base_project,
            port_table=PORT_TABLE,
            port_stride=cfg.port_stride,
        )
        for slot in range(cfg.max_concurrent)
    ]
    assert_namespaces_disjoint(namespaces)

    # Per-target serial: a per-target lock acquired before a slot, so a blocked
    # same-target episode does not hold a lane (R15 — per-target serialization).
    target_locks: dict[str, threading.Lock] = {
        spec.target: threading.Lock() for spec in admitted
    }
    # Slot lanes 0..N-1 handed out and returned; two concurrent episodes never
    # share one, so their namespaces stay disjoint by construction.
    slot_pool: list[int] = list(range(cfg.max_concurrent))
    slot_guard = threading.Lock()

    conc_guard = threading.Lock()
    in_flight = 0
    peak = 0
    products: list[object | None] = [None] * len(admitted)

    def _acquire_slot() -> int:
        with slot_guard:
            return slot_pool.pop()

    def _release_slot(slot: int) -> None:
        with slot_guard:
            slot_pool.append(slot)

    def _run(index: int, spec: EpisodeSpec) -> None:
        nonlocal in_flight, peak
        with target_locks[spec.target]:
            slot = _acquire_slot()
            try:
                with conc_guard:
                    in_flight += 1
                    peak = max(peak, in_flight)
                ctx = EpisodeRunContext(
                    spec=spec,
                    slot=slot,
                    namespace=namespaces[slot],
                    library=library,
                )
                products[index] = work_fn(ctx)
            finally:
                with conc_guard:
                    in_flight -= 1
                _release_slot(slot)

    if admitted:
        workers = min(cfg.max_concurrent, len(admitted))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_run, i, spec) for i, spec in enumerate(admitted)
            ]
            for fut in futures:
                fut.result()  # re-raise any work_fn error on the main thread

    # The read-only invariant, verified: episodes saw a frozen view and hold no
    # store handle, so the active-set snapshot must be exactly where we left it.
    after = store.current_snapshot_id()
    if after != library.snapshot_id:
        raise SchedulerError(
            f"library snapshot moved during the parallel wave"
            f" ({library.snapshot_id} -> {after}); episodes must be read-only"
            " against the library (R15) — all writes go through the single-writer"
            " queue AFTER the wave"
        )

    logger.info(
        "parallel wave: %d admitted, %d deferred, peak concurrency %d",
        len(admitted),
        len(deferred),
        peak,
    )
    return ParallelEpisodeResult(
        products=tuple(products),
        admitted=tuple(admitted),
        deferred=tuple(deferred),
        peak_concurrency=peak,
        namespaces=tuple(namespaces),
        snapshot_id=library.snapshot_id,
    )


# --- parallel validation (R17) --------------------------------------------------


@dataclass(frozen=True)
class ParallelValidationResult:
    """Validation tasks run concurrently, results returned in input order."""

    results: tuple[object, ...]
    peak_concurrency: int


def run_validations(
    tasks: Sequence[Callable[[], object]], *, max_workers: int
) -> ParallelValidationResult:
    """Run read-only validation tasks concurrently (R17).

    Validation (trials, benchmarks, replay re-judging, mutation audits) is
    read-only against library variants, so it parallelizes freely and is the
    throughput bottleneck that parallelizes first. Results are returned in input
    order regardless of completion order.
    """
    if max_workers < 1:
        raise SchedulerError(f"max_workers must be >= 1, got {max_workers}")
    if not tasks:
        return ParallelValidationResult(results=(), peak_concurrency=0)

    guard = threading.Lock()
    in_flight = 0
    peak = 0
    results: list[object | None] = [None] * len(tasks)

    def _run(index: int, task: Callable[[], object]) -> None:
        nonlocal in_flight, peak
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            results[index] = task()
        finally:
            with guard:
                in_flight -= 1

    with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as pool:
        futures = [pool.submit(_run, i, t) for i, t in enumerate(tasks)]
        for fut in futures:
            fut.result()
    return ParallelValidationResult(results=tuple(results), peak_concurrency=peak)


# --- batch merging (R16) --------------------------------------------------------


@dataclass(frozen=True)
class MergeCandidate:
    """A batch that **independently** passed validation against the shared
    snapshot (Plan 4 U7 is the per-batch gate) and is entering the merge."""

    episode_id: int
    batch_label: str

    def __post_init__(self) -> None:
        if not self.batch_label.strip():
            raise SchedulerError("MergeCandidate.batch_label must be non-empty")


# The overlap prefilter (R16): given the ordered candidate insights (a list of
# ``(batch_label, insight_row)`` in canonical order), return the duplicate pairs
# to absorb as ``(duplicate_insight_id, canonical_insight_id)``. The canonical id
# is always an earlier (or same) member, so dedup folds later batches into earlier
# ones deterministically. The live binding is the cosine/judge prefilter; the
# suite injects a scripted fake. ``content_overlap_absorber`` is the deterministic
# default (exact structural-hash overlap, no embeddings).
OverlapFn = Callable[[Sequence["CandidateInsight"]], "Sequence[tuple[int, int]]"]


@dataclass(frozen=True)
class CandidateInsight:
    """One quarantined member of a candidate batch, in canonical merge order."""

    batch_label: str
    episode_id: int
    insight_id: int
    content_hash: str


@dataclass(frozen=True)
class JointConfirmation:
    """The one joint confirmation run on the merged union (R16).

    ``passed=False`` blocks the promotion and is recorded as interaction-effect
    telemetry — the interaction the independent per-batch validations could not
    see.
    """

    passed: bool
    detail: str = ""


# The joint confirmation seam (R16): given the surviving union insight ids (post
# overlap absorption), confirm the union does not regress. The live binding runs
# one confirmation episode/benchmark on the union; the suite injects a fake.
JointConfirmFn = Callable[[Sequence[int]], JointConfirmation]


@dataclass(frozen=True)
class AbsorbedOverlap:
    """One absorbed cross-batch duplicate (R16 telemetry)."""

    duplicate_insight_id: int
    canonical_insight_id: int
    duplicate_content_hash: str
    canonical_content_hash: str


@dataclass(frozen=True)
class MergeResult:
    """The outcome of one batch merge."""

    promoted: bool
    candidate_labels: tuple[str, ...]  # canonical (episode-id sorted) order
    absorbed: tuple[AbsorbedOverlap, ...]
    joint_confirmation: JointConfirmation
    promoted_labels: tuple[str, ...]
    promoted_content_hashes: tuple[str, ...]
    reverted_labels: tuple[str, ...]
    absorb_snapshot_id: int | None
    telemetry_id: int | None

    def canonical_digest(self) -> str:
        """A row-id-independent fingerprint of the post-merge state (the U5
        determinism witness). Hashes content hashes and verdicts — never
        autoincrement ids — so a parallel run and a sequential run over the same
        shared snapshot and canonical order produce the SAME digest.
        """
        payload = {
            "promoted": self.promoted,
            "candidate_labels": list(self.candidate_labels),
            "absorbed": sorted(
                [a.duplicate_content_hash, a.canonical_content_hash]
                for a in self.absorbed
            ),
            "joint_passed": self.joint_confirmation.passed,
            "promoted_labels": list(self.promoted_labels),
            "promoted_content_hashes": sorted(self.promoted_content_hashes),
            "reverted_labels": sorted(self.reverted_labels),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _quarantined_members(store: Store, batch_label: str) -> list:
    """The batch's still-quarantined members (``id``/``content_hash`` rows)."""
    row = store.conn.execute(
        "SELECT id FROM batches WHERE label = ?", (batch_label,)
    ).fetchone()
    if row is None:
        raise SchedulerError(f"unknown batch '{batch_label}'")
    return store.conn.execute(
        "SELECT id, content_hash FROM insights"
        " WHERE batch_id = ? AND status = 'quarantined' ORDER BY id ASC",
        (row["id"],),
    ).fetchall()


def content_overlap_absorber(
    candidates: Sequence[CandidateInsight],
) -> list[tuple[int, int]]:
    """Deterministic default overlap prefilter: absorb exact structural-hash
    duplicates (R16). Folds every later occurrence of a content hash into the
    first (canonical) one. The cosine/judge near-duplicate prefilter is the live
    binding (the suite injects it as a fake); this exact-hash absorber needs no
    embeddings and is the offline floor.
    """
    first_by_hash: dict[str, int] = {}
    pairs: list[tuple[int, int]] = []
    for cand in candidates:
        canonical = first_by_hash.get(cand.content_hash)
        if canonical is None:
            first_by_hash[cand.content_hash] = cand.insight_id
        else:
            pairs.append((cand.insight_id, canonical))
    return pairs


def merge_batches(
    store: Store,
    candidates: Sequence[MergeCandidate],
    *,
    shared_snapshot_id: int,
    joint_confirm_fn: JointConfirmFn,
    overlap_fn: OverlapFn = content_overlap_absorber,
) -> MergeResult:
    """Merge independently-validated batches under the decided policy (R16).

    Order of operations (the policy, exactly):

    1. **Canonical sort** the candidates by episode id — the determinism fix
       (judge-mediated dedup makes the surviving row order-dependent, so the order
       is pinned, not assumed irrelevant).
    2. **Absorb cross-batch overlap** through registration: the prefilter flags
       near/exact duplicates across batches; each is retired and merge-logged in
       one single-writer queue operation, so the union carries no double count.
    3. **One joint confirmation** on the surviving union.
    4. **Promote on pass, block on fail**: a pass promotes every candidate batch
       (canonical order, single-writer queue); a fail blocks the promotion, leaves
       the union quarantined, and records the interaction as telemetry. Revert is
       per-batch (:func:`revert_merged_batch`).
    """
    if not candidates:
        raise SchedulerError("merge_batches needs at least one candidate batch")
    ordered = sorted(candidates, key=lambda c: c.episode_id)
    candidate_labels = tuple(c.batch_label for c in ordered)

    # Gather every candidate batch's quarantined members in canonical order.
    union: list[CandidateInsight] = []
    for cand in ordered:
        for member in _quarantined_members(store, cand.batch_label):
            union.append(
                CandidateInsight(
                    batch_label=cand.batch_label,
                    episode_id=cand.episode_id,
                    insight_id=member["id"],
                    content_hash=member["content_hash"],
                )
            )

    # 2. Absorb overlap: retire + merge-log each duplicate under one snapshot.
    raw_pairs = list(overlap_fn(union))
    by_id = {c.insight_id: c for c in union}
    absorbed: list[AbsorbedOverlap] = []
    absorb_snapshot_id: int | None = None
    if raw_pairs:
        with store.queue_operation(
            "merge_batches_absorb", ",".join(candidate_labels)
        ) as snapshot_id:
            absorb_snapshot_id = snapshot_id
            for dup_id, canonical_id in raw_pairs:
                dup = by_id.get(dup_id)
                if dup is None:
                    raise SchedulerError(
                        f"overlap prefilter named insight {dup_id} which is not a"
                        " quarantined member of any candidate batch"
                    )
                canonical_row = store.get_insight(canonical_id)
                if canonical_row is None:
                    raise SchedulerError(
                        f"overlap prefilter named canonical insight {canonical_id}"
                        " which does not exist"
                    )
                store.set_status(dup_id, "retired", snapshot_id)
                store.insert_merge_log(
                    content_hash=dup.content_hash,
                    structural_fields_json=json.dumps(
                        {"merged_into": canonical_id}, sort_keys=True
                    ),
                    duplicate_of=canonical_id,
                    batch_id=store.get_insight(dup_id)["batch_id"],
                )
                absorbed.append(
                    AbsorbedOverlap(
                        duplicate_insight_id=dup_id,
                        canonical_insight_id=canonical_id,
                        duplicate_content_hash=dup.content_hash,
                        canonical_content_hash=canonical_row["content_hash"],
                    )
                )

    # 3. The joint confirmation runs on the surviving union (post-absorption).
    absorbed_ids = {a.duplicate_insight_id for a in absorbed}
    survivors = [c.insight_id for c in union if c.insight_id not in absorbed_ids]
    confirmation = joint_confirm_fn(survivors)

    # 4. Promote on pass, block + record on fail.
    if confirmation.passed:
        promoted_labels: list[str] = []
        promoted_hashes: list[str] = []
        for cand in ordered:
            members = _quarantined_members(store, cand.batch_label)
            if not members:
                # Fully absorbed — nothing left to promote (no error).
                continue
            lifecycle.promote_batch(store, cand.batch_label)
            promoted_labels.append(cand.batch_label)
            promoted_hashes.extend(m["content_hash"] for m in members)
        logger.info(
            "batch merge promoted %d batch(es) on joint confirmation (%d absorbed)",
            len(promoted_labels),
            len(absorbed),
        )
        return MergeResult(
            promoted=True,
            candidate_labels=candidate_labels,
            absorbed=tuple(absorbed),
            joint_confirmation=confirmation,
            promoted_labels=tuple(promoted_labels),
            promoted_content_hashes=tuple(promoted_hashes),
            reverted_labels=(),
            absorb_snapshot_id=absorb_snapshot_id,
            telemetry_id=None,
        )

    # Joint-confirmation failure: block the promotion, record the interaction.
    telemetry_id = _write_joint_fail_telemetry(
        store, ordered, survivors, confirmation
    )
    logger.warning(
        "batch merge blocked: joint confirmation failed on the union (%s);"
        " interaction recorded (review_queue %d)",
        confirmation.detail or "no detail",
        telemetry_id,
    )
    return MergeResult(
        promoted=False,
        candidate_labels=candidate_labels,
        absorbed=tuple(absorbed),
        joint_confirmation=confirmation,
        promoted_labels=(),
        promoted_content_hashes=(),
        reverted_labels=(),
        absorb_snapshot_id=absorb_snapshot_id,
        telemetry_id=telemetry_id,
    )


def _write_joint_fail_telemetry(
    store: Store,
    ordered: Sequence[MergeCandidate],
    survivors: Sequence[int],
    confirmation: JointConfirmation,
) -> int:
    """Record a blocked joint confirmation as interaction-effect telemetry (R16).

    The joint-confirmation failure rate IS the interaction-effect telemetry — the
    signal that two independently-valid batches interact badly on the union.
    """
    payload = json.dumps(
        {
            "candidate_batches": [c.batch_label for c in ordered],
            "candidate_episode_ids": [c.episode_id for c in ordered],
            "union_insight_ids": list(survivors),
            "detail": confirmation.detail,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    with store.transaction():
        cur = store.conn.execute(
            "INSERT INTO review_queue (episode_id, kind, payload_json, status,"
            " created_at) VALUES (NULL, ?, ?, 'open', ?)",
            (JOINT_CONFIRM_FAIL_KIND, payload, _utcnow()),
        )
    return cur.lastrowid


def revert_merged_batch(store: Store, batch_label: str) -> lifecycle.LifecycleResult:
    """Revert exactly one batch of a merged promotion (R16, per-batch revert).

    Removes only ``batch_label``'s insights (the single-writer queue mints one
    snapshot); sibling batches promoted in the same merge are untouched — the
    per-batch keying that made the merge revertible.
    """
    return lifecycle.revert_batch(store, batch_label)
