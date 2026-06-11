"""plan-005 U5: Parallel episodes and batch merging (R15-R17).

Fully offline (the default suite): episodes, the overlap prefilter, the joint
confirmation, and validation tasks are all injected callables driven by scripted
fakes — zero quota, no ``claude`` on PATH, no docker. The promote/revert/merge-log
and absorb paths drive the real store queue. Concurrency claims are proven with a
``threading.Barrier`` (a slot cap below the arrival count would deadlock, so a
clean pass is the witness that the episodes truly overlapped).

## Conformance

Test-scenario / invariant (plan-005 U5) -> test:

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

import threading

import pytest

from agent_families.grading import target_env as te
from agent_families.pipeline import scheduler as sch
from agent_families.store import Store


# --- fixtures + builders --------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


def _new_store(tmp_path, name):
    s = Store(tmp_path / name)
    s.migrate()
    return s


def _quarantined_batch(store: Store, label: str, hashes) -> list[int]:
    """Register quarantined insights tagged to one batch (Stage B's output)."""
    batch_id = store.ensure_batch(label)
    ids = []
    for i, h in enumerate(hashes):
        iid = store.insert_insight(
            precondition=f"when {label} case {i}",
            action=f"do {label} action {i}",
            expected_outcome=f"expect {label} outcome {i}",
            content_hash=h,
            status="quarantined",
            batch_id=batch_id,
        )
        ids.append(iid)
    return ids


def _status(store: Store, iid: int) -> str:
    return store.get_insight(iid)["status"]


PASS = sch.JointConfirmation(passed=True)


# --- R15: per-episode environment namespacing -----------------------------------


def test_episode_namespaces_are_disjoint():
    """Each parallel slot gets a distinct compose project and a disjoint port
    window (R15) — verified, not assumed."""
    namespaces = [
        te.episode_namespace(slot, port_stride=100) for slot in range(3)
    ]
    # Slot 0 is byte-identical to the static single-stack harness.
    assert namespaces[0].project_name == te.DEFAULT_COMPOSE_PROJECT
    assert namespaces[0].ports["linkding"] == te.PORT_TABLE["linkding"]
    # Distinct project names.
    assert len({ns.project_name for ns in namespaces}) == 3
    # Every host port across every slot is unique.
    all_ports = [p for ns in namespaces for p in ns.ports.values()]
    assert len(all_ports) == len(set(all_ports))
    # The explicit disjointness check passes for the concurrent set.
    te.assert_namespaces_disjoint(namespaces)
    # ports are offset by slot * stride.
    assert namespaces[1].ports["linkding"] == te.PORT_TABLE["linkding"] + 100
    assert namespaces[1].compose_args == ("-p", f"{te.DEFAULT_COMPOSE_PROJECT}-ep1")


def test_assert_namespaces_disjoint_catches_a_collision():
    """Two slots forced onto one project/port raise (the invariant fails loud)."""
    a = te.episode_namespace(1, port_stride=100)
    clash = te.EpisodeNamespace(slot=2, project_name=a.project_name, ports=a.ports)
    with pytest.raises(te.NamespaceCollisionError):
        te.assert_namespaces_disjoint([a, clash])


def test_namespace_rejects_colliding_base_ports():
    """A port table that maps two services onto one base port is a hard error
    (R15) — every slot inherits the collision, so it fails at construction."""
    with pytest.raises(te.NamespaceCollisionError):
        te.episode_namespace(0, port_table={"a": 5000, "b": 5000})


# --- R15: parallel episodes -----------------------------------------------------


def test_two_episodes_run_concurrently_library_read_only(store):
    """Two different-target episodes run at once, see a read-only library view,
    and the library snapshot does not move during the wave (R15)."""
    cfg = sch.SchedulerConfig(max_concurrent=2)
    specs = [
        sch.EpisodeSpec(episode_id=1, target="linkding"),
        sch.EpisodeSpec(episode_id=2, target="kanboard"),
    ]
    barrier = threading.Barrier(2, timeout=5)
    seen_slots: list[int] = []
    guard = threading.Lock()

    def work(ctx: sch.EpisodeRunContext):
        # Read-only access works; there is no write path on the view.
        assert isinstance(ctx.library, sch.LibrarySnapshotView)
        _ = ctx.library.active_insights()
        assert not hasattr(ctx.library, "set_status")
        with guard:
            seen_slots.append(ctx.slot)
        # Both must arrive for the barrier to release: proves true concurrency.
        barrier.wait()
        return ("done", ctx.spec.episode_id, ctx.namespace.project_name)

    result = sch.run_episodes(store, cfg, specs, work)

    assert result.peak_concurrency == 2
    assert len(result.products) == 2
    # Concurrent episodes held distinct slots -> disjoint namespaces.
    assert sorted(seen_slots) == [0, 1]
    # The two episodes' compose projects differ.
    projects = {p[2] for p in result.products}
    assert len(projects) == 2
    # Read-only verified: the snapshot is exactly where it started (no writes).
    assert result.snapshot_id == store.current_snapshot_id()
    assert result.deferred == ()


def test_same_target_episodes_serialize(store):
    """At most one in-flight episode per target: same-target episodes never
    overlap, even with spare concurrency (R15 per-target serial)."""
    cfg = sch.SchedulerConfig(max_concurrent=2)
    specs = [
        sch.EpisodeSpec(episode_id=1, target="linkding"),
        sch.EpisodeSpec(episode_id=2, target="linkding"),  # same target
    ]
    events: list[tuple[str, int]] = []
    guard = threading.Lock()

    def work(ctx: sch.EpisodeRunContext):
        with guard:
            events.append(("start", ctx.spec.episode_id))
        # brief work window; if the two overlapped, starts would interleave
        for _ in range(1000):
            pass
        with guard:
            events.append(("end", ctx.spec.episode_id))
        return ctx.spec.episode_id

    result = sch.run_episodes(store, cfg, specs, work)

    # Never two in-flight at once on the same target.
    assert result.peak_concurrency == 1
    # The event log is two clean non-overlapping start/end pairs.
    assert events[0][0] == "start" and events[1][0] == "end"
    assert events[1][1] == events[0][1]
    assert events[2][0] == "start" and events[3][0] == "end"
    assert events[3][1] == events[2][1]


def test_quota_aware_admission_defers_episodes(store):
    """Episodes whose cumulative estimated cost exceeds the budget are deferred
    to a later wave (R15 quota-aware admission)."""
    cfg = sch.SchedulerConfig(max_concurrent=3, quota_budget_usd=2.0)
    specs = [
        sch.EpisodeSpec(episode_id=1, target="a", est_cost_usd=1.0),
        sch.EpisodeSpec(episode_id=2, target="b", est_cost_usd=1.0),
        sch.EpisodeSpec(episode_id=3, target="c", est_cost_usd=1.0),  # over budget
    ]
    ran: list[int] = []
    guard = threading.Lock()

    def work(ctx: sch.EpisodeRunContext):
        with guard:
            ran.append(ctx.spec.episode_id)
        return ctx.spec.episode_id

    result = sch.run_episodes(store, cfg, specs, work)

    assert [s.episode_id for s in result.admitted] == [1, 2]
    assert [s.episode_id for s in result.deferred] == [3]
    assert sorted(ran) == [1, 2]


def test_unbounded_budget_admits_all(store):
    cfg = sch.SchedulerConfig(max_concurrent=2, quota_budget_usd=None)
    specs = [sch.EpisodeSpec(episode_id=i, target=f"t{i}") for i in range(4)]
    result = sch.run_episodes(store, cfg, specs, lambda ctx: ctx.spec.episode_id)
    assert len(result.admitted) == 4
    assert result.deferred == ()


# --- R17: parallel validation ---------------------------------------------------


def test_validations_run_concurrently():
    """Read-only validation tasks parallelize freely (R17)."""
    barrier = threading.Barrier(3, timeout=5)

    def make_task(i):
        def task():
            barrier.wait()  # all three must arrive -> proves concurrency
            return i * i
        return task

    result = sch.run_validations([make_task(i) for i in range(3)], max_workers=3)
    assert result.peak_concurrency == 3
    assert result.results == (0, 1, 4)  # input order preserved


def test_validations_empty_is_a_noop():
    result = sch.run_validations([], max_workers=4)
    assert result.results == ()
    assert result.peak_concurrency == 0


# --- R16: batch merging ---------------------------------------------------------


def test_content_overlap_absorber_folds_exact_hashes():
    """The deterministic default absorber folds later occurrences of a hash into
    the first (canonical) one (R16)."""
    cands = [
        sch.CandidateInsight("A", 1, 10, "h1"),
        sch.CandidateInsight("A", 1, 11, "h2"),
        sch.CandidateInsight("B", 2, 20, "h1"),  # dup of insight 10
    ]
    pairs = sch.content_overlap_absorber(cands)
    assert pairs == [(20, 10)]


def test_merge_absorbs_planted_near_duplicate(store):
    """Two validated batches merge; the prefilter absorbs a planted near-duplicate
    (distinct content, judged equivalent) so the union carries no double count,
    and the survivors promote on a passing joint confirmation (R16)."""
    a_ids = _quarantined_batch(store, "reflect-ep1", ["a-h1"])
    b_ids = _quarantined_batch(store, "reflect-ep2", ["b-h1"])  # near-dup of a-h1
    snap = store.current_snapshot_id()

    # The cosine/judge prefilter (faked): batch B's insight is a near-duplicate of
    # batch A's, even though their content hashes differ.
    def overlap(union):
        a_canonical = next(c.insight_id for c in union if c.batch_label == "reflect-ep1")
        b_dup = next(c.insight_id for c in union if c.batch_label == "reflect-ep2")
        return [(b_dup, a_canonical)]

    confirms: list[list[int]] = []

    def joint(survivors):
        confirms.append(list(survivors))
        return sch.JointConfirmation(passed=True)

    candidates = [
        sch.MergeCandidate(episode_id=1, batch_label="reflect-ep1"),
        sch.MergeCandidate(episode_id=2, batch_label="reflect-ep2"),
    ]
    result = sch.merge_batches(
        store,
        candidates,
        shared_snapshot_id=snap,
        joint_confirm_fn=joint,
        overlap_fn=overlap,
    )

    assert result.promoted is True
    assert len(result.absorbed) == 1
    absorbed = result.absorbed[0]
    assert absorbed.duplicate_insight_id == b_ids[0]
    assert absorbed.canonical_insight_id == a_ids[0]
    # The duplicate is retired + merge-logged; only the canonical survives active.
    assert _status(store, b_ids[0]) == "retired"
    assert _status(store, a_ids[0]) == "active"
    assert store.find_merge_log_by_hash("b-h1") is not None
    # The joint confirmation saw the deduped union (one survivor, not two).
    assert confirms == [[a_ids[0]]]
    # Batch A promoted; batch B was fully absorbed so it promoted nothing.
    assert result.promoted_labels == ("reflect-ep1",)


def test_joint_confirmation_failure_blocks_and_records_telemetry(store):
    """A joint-confirmation failure (planted interaction) blocks the promotion,
    leaves the union quarantined, and records the interaction telemetry (R16)."""
    a_ids = _quarantined_batch(store, "reflect-ep1", ["a-h1"])
    b_ids = _quarantined_batch(store, "reflect-ep2", ["b-h1"])
    snap = store.current_snapshot_id()

    def joint(survivors):
        return sch.JointConfirmation(passed=False, detail="planted interaction")

    candidates = [
        sch.MergeCandidate(episode_id=1, batch_label="reflect-ep1"),
        sch.MergeCandidate(episode_id=2, batch_label="reflect-ep2"),
    ]
    result = sch.merge_batches(
        store, candidates, shared_snapshot_id=snap, joint_confirm_fn=joint
    )

    assert result.promoted is False
    assert result.promoted_labels == ()
    # The union stays quarantined — promotion blocked (default deny).
    assert _status(store, a_ids[0]) == "quarantined"
    assert _status(store, b_ids[0]) == "quarantined"
    # The interaction is recorded as telemetry.
    assert result.telemetry_id is not None
    row = store.conn.execute(
        "SELECT kind, payload_json FROM review_queue WHERE id = ?",
        (result.telemetry_id,),
    ).fetchone()
    assert row["kind"] == sch.JOINT_CONFIRM_FAIL_KIND
    assert "planted interaction" in row["payload_json"]


def test_per_batch_revert_removes_exactly_one_batch(store):
    """After a merged promotion, reverting one batch removes exactly that batch's
    insights and leaves the sibling batch active (R16 per-batch revert)."""
    a_ids = _quarantined_batch(store, "reflect-ep1", ["a-h1", "a-h2"])
    b_ids = _quarantined_batch(store, "reflect-ep2", ["b-h1"])
    snap = store.current_snapshot_id()

    candidates = [
        sch.MergeCandidate(episode_id=1, batch_label="reflect-ep1"),
        sch.MergeCandidate(episode_id=2, batch_label="reflect-ep2"),
    ]
    result = sch.merge_batches(
        store, candidates, shared_snapshot_id=snap, joint_confirm_fn=lambda s: PASS
    )
    assert result.promoted is True
    assert set(result.promoted_labels) == {"reflect-ep1", "reflect-ep2"}
    assert all(_status(store, i) == "active" for i in a_ids + b_ids)

    # Revert exactly batch B.
    sch.revert_merged_batch(store, "reflect-ep2")
    assert all(_status(store, i) == "active" for i in a_ids)  # untouched
    assert all(_status(store, i) == "retired" for i in b_ids)  # gone


def test_merge_requires_a_candidate(store):
    with pytest.raises(sch.SchedulerError):
        sch.merge_batches(
            store, [], shared_snapshot_id=0, joint_confirm_fn=lambda s: PASS
        )


# --- the U5 verification: canonical merge order is deterministic ----------------


def _build_two_batches(store: Store):
    a = _quarantined_batch(store, "reflect-ep1", ["a-h1", "a-h2"])
    b = _quarantined_batch(store, "reflect-ep2", ["b-h1"])
    return a, b


def _active_state(store: Store):
    """A row-id-independent serialization of the active set (content hashes)."""
    rows = store.conn.execute(
        "SELECT content_hash, status FROM insights ORDER BY content_hash"
    ).fetchall()
    return tuple((r["content_hash"], r["status"]) for r in rows)


def test_canonical_merge_order_is_deterministic(tmp_path):
    """Validated batches sort by the canonical key (episode id) before
    registration, so a parallel run (batches completing in any order) and a
    sequential run over the same shared snapshot produce a byte-identical
    post-merge state (the U5 verification)."""
    # An overlap prefilter that absorbs B's near-duplicate into A's canonical.
    def overlap(union):
        a_canonical = next(c.insight_id for c in union if c.batch_label == "reflect-ep1")
        b_dup = next(
            (c.insight_id for c in union
             if c.batch_label == "reflect-ep2" and c.content_hash == "b-h1"),
            None,
        )
        return [(b_dup, a_canonical)] if b_dup is not None else []

    store1 = _new_store(tmp_path, "s1.db")
    store2 = _new_store(tmp_path, "s2.db")
    try:
        _build_two_batches(store1)
        _build_two_batches(store2)
        snap1 = store1.current_snapshot_id()
        snap2 = store2.current_snapshot_id()

        cand_a = sch.MergeCandidate(episode_id=1, batch_label="reflect-ep1")
        cand_b = sch.MergeCandidate(episode_id=2, batch_label="reflect-ep2")

        # Store 1: candidates in completion order [A, B].
        r1 = sch.merge_batches(
            store1, [cand_a, cand_b], shared_snapshot_id=snap1,
            joint_confirm_fn=lambda s: PASS, overlap_fn=overlap,
        )
        # Store 2: the SAME batches completing in the reverse order [B, A].
        r2 = sch.merge_batches(
            store2, [cand_b, cand_a], shared_snapshot_id=snap2,
            joint_confirm_fn=lambda s: PASS, overlap_fn=overlap,
        )

        # Canonical sort makes both process [ep1, ep2]: identical digests...
        assert r1.canonical_digest() == r2.canonical_digest()
        assert r1.candidate_labels == r2.candidate_labels == (
            "reflect-ep1",
            "reflect-ep2",
        )
        # ...and a byte-identical post-merge active set.
        assert _active_state(store1) == _active_state(store2)
    finally:
        store1.close()
        store2.close()
