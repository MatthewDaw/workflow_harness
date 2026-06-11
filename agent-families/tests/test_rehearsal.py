"""plan-005 U2: rehearsal pass and the one-shot metric.

Fully offline and fake-driven per the plan's approach. The DAG/wave scheduling
is pure and tested directly; the fan-out runs against a small REAL git workspace
(worktrees, merges, and the HEAD-moves-on-adopt contract execute for real — the
one-shot worker session, gate, and verifier are scripted fakes that need no
``node_modules``). The store is real (episode/run rows, the plan document, and
the run-memory injection are exercised, never mocked).

## Conformance

Plan test scenarios (005 U2), mapped 1:1:

- diamond DAG rehearses in two waves with the conflicting-files pair serialized:
  ``test_diamond_dag_rehearses_in_waves_with_conflicting_pair_serialized``
  (with ``test_compute_waves_respects_topological_order`` guarding the DAG
  ordering invariant the serialization rides on)
- all-green rehearsal adopts the fan-out artifact (workspace head moves):
  ``test_all_green_rehearsal_adopts_fanout_artifact_head_moves``
- one failed ticket falls back to converged artifact and writes the
  integration-failure record:
  ``test_one_failed_ticket_falls_back_and_writes_integration_failure_record``
  (and the headline class:
  ``test_passed_alone_broke_together_records_integration_failure_class``)
- one-shot rates computed correctly across fixtures:
  ``test_one_shot_rates_computed_across_fixtures``
- sampling honors the budget share:
  ``test_sampling_honors_budget_share``
- crossover report compares both regimes' measured costs:
  ``test_crossover_report_compares_both_regimes``

Verification clauses:

- metric appears in settlement reports:
  ``test_metric_appears_in_settlement_report``
- fallback path leaves the episode equivalent to a no-rehearsal run:
  ``test_fallback_leaves_episode_equivalent_to_no_rehearsal``

R5/R6/R8 supporting invariants:

- one-shot sessions consume the episode run-memory workflows (R5):
  ``test_oneshot_sessions_consume_run_memory_workflows``
- file-ownership lint promoted to enforcement is the shared definition (R8):
  ``test_file_ownership_conflicts_is_the_shared_definition``
- rebuild-probe promotion decision (R6, clone-rot remedy):
  ``test_should_promote_probe_on_must_tier_parity``
- rebuild-probe mode carries episode-one-shot (R6):
  ``test_rebuild_probe_mode_carries_episode_one_shot``
- the episode rehearsal seam (005 U2 episode.py touch): base-ref capture
  round-trips and the seam never blocks the episode:
  ``test_episode_state_round_trips_increment_base_ref``,
  ``test_run_rehearsal_seam_invokes_with_base_ref``,
  ``test_run_rehearsal_seam_swallows_failure_and_skips_without_base_ref``
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_families.grading.settle import assemble_report
from agent_families.pipeline.planning import (
    file_ownership_conflicts,
    plan_meta_key,
)
from agent_families.pipeline.rehearsal import (
    FAILURE_CLASS_INTEGRATION,
    FAILURE_CLASS_TICKET,
    REBUILD_PROBE_MODE,
    REGIME_CONVERGE_FIRST,
    REGIME_FANOUT_FIRST,
    REHEARSAL_INTEGRATION_FAILURE_KIND,
    CheckResult,
    RegimeCost,
    RehearsalConfig,
    RehearsalError,
    RehearsalSamplingPolicy,
    RehearsalStages,
    RehearsalTicket,
    attach_one_shot_metrics,
    compute_waves,
    crossover_report,
    current_head,
    load_rehearsal_outcome,
    run_rehearsal,
    should_promote_probe,
)
from agent_families.pipeline.workspace import Workspace
from agent_families.store import Store

TARGET = "linkding"
DIGEST = "sha256:0123456789abcdef"


# --- scaffolding -------------------------------------------------------------


def make_store(base: Path) -> Store:
    base.mkdir(parents=True, exist_ok=True)
    store = Store(base / "library.db")
    store.migrate()
    return store


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return proc.stdout.strip()


def make_workspace(root: Path) -> tuple[Workspace, str, str]:
    """A small real git repo: a base commit, then a converged commit on top.

    Returns the workspace, the increment-base ref, and the converged ref (the
    workspace HEAD after convergence).
    """
    root.mkdir(parents=True)
    (root / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "user.email", "t@localhost")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    base_ref = _git(root, "rev-parse", "HEAD")
    (root / "converged.txt").write_text(
        "converged\n", encoding="utf-8", newline="\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "converged")
    converged_ref = _git(root, "rev-parse", "HEAD")
    return Workspace(root=root), base_ref, converged_ref


def setup_increment(
    store: Store,
    tickets: list[dict],
    *,
    increment_index: int = 1,
    epoch: int | None = None,
) -> tuple[int, int]:
    """Create an episode + run and persist a plan document with ``tickets``.

    Each ticket dict needs id/depends_on/files. Returns (episode_id, run_id).
    """
    snapshot = store.current_snapshot_id()
    episode_id = store.create_episode(
        TARGET, DIGEST, snapshot, max_increments=5, cost_ceiling_usd=100.0
    )
    if epoch is not None:
        with store.transaction():
            store.conn.execute(
                "UPDATE episodes SET epoch = ? WHERE id = ?",
                (epoch, episode_id),
            )
    run_id = store.create_run(
        f"spec-{episode_id}",
        snapshot,
        episode_id=episode_id,
        increment_index=increment_index,
    )
    document = {
        "run_id": run_id,
        "spec_ref": f"spec-{episode_id}",
        "requirements": [],
        "tickets": [
            {
                "id": t["id"],
                "title": t["id"],
                "description": "",
                "kind": "feature",
                "covers": [],
                "depends_on": list(t.get("depends_on", ())),
                "files": list(t.get("files", ())),
                "acceptance_criteria": [],
            }
            for t in tickets
        ],
        "assumptions": [],
        "warnings": [],
    }
    import json

    store.set_meta(
        plan_meta_key(run_id), json.dumps(document, sort_keys=True)
    )
    return episode_id, run_id


def make_oneshot(pass_map: dict[str, bool], *, seen: dict | None = None):
    """A scripted one-shot session: writes a per-ticket file into its worktree
    and reports pass/fail from ``pass_map`` (default pass)."""

    def fn(ctx):
        (ctx.workspace.root / f"{ctx.ticket.ticket_id}.ts").write_text(
            f"// {ctx.ticket.ticket_id}\n", encoding="utf-8", newline="\n"
        )
        if seen is not None:
            seen[ctx.ticket.ticket_id] = {
                "section": ctx.injection_section,
                "workflow_ids": ctx.injection_workflow_ids,
            }
        return CheckResult(pass_map.get(ctx.ticket.ticket_id, True))

    return fn


def always(passed: bool):
    return lambda ctx: CheckResult(passed)


def worktree_count(workspace: Workspace) -> int:
    out = _git(workspace.root, "worktree", "list")
    return len([line for line in out.splitlines() if line.strip()])


CONFIG = RehearsalConfig(injection_budget_tokens=1000)


def tkt(tid: str, depends_on=(), files=()) -> RehearsalTicket:
    return RehearsalTicket(tid, tuple(depends_on), tuple(files))


# --- compute_waves (scenario 1 + ordering invariant) -------------------------


def test_diamond_dag_rehearses_in_waves_with_conflicting_pair_serialized():
    # Diamond: A -> {B, C} -> D. Without a file conflict B and C share one wave.
    parallel = [
        tkt("TKT-A", files=("a.ts",)),
        tkt("TKT-B", depends_on=("TKT-A",), files=("b.ts",)),
        tkt("TKT-C", depends_on=("TKT-A",), files=("c.ts",)),
        tkt("TKT-D", depends_on=("TKT-B", "TKT-C"), files=("d.ts",)),
    ]
    assert compute_waves(parallel) == (
        ("TKT-A",),
        ("TKT-B", "TKT-C"),
        ("TKT-D",),
    )

    # Now B and C claim the SAME file: the promoted file-ownership lint serializes
    # them into separate waves so their fan-out artifacts cannot conflict.
    conflicting = [
        tkt("TKT-A", files=("a.ts",)),
        tkt("TKT-B", depends_on=("TKT-A",), files=("shared.ts",)),
        tkt("TKT-C", depends_on=("TKT-A",), files=("shared.ts",)),
        tkt("TKT-D", depends_on=("TKT-B", "TKT-C"), files=("d.ts",)),
    ]
    waves = compute_waves(conflicting)
    assert waves == (("TKT-A",), ("TKT-B",), ("TKT-C",), ("TKT-D",))
    # The conflicting pair never shares a wave.
    b_wave = next(i for i, w in enumerate(waves) if "TKT-B" in w)
    c_wave = next(i for i, w in enumerate(waves) if "TKT-C" in w)
    assert b_wave != c_wave


def test_compute_waves_respects_topological_order():
    waves = compute_waves(
        [
            tkt("TKT-A", files=("a.ts",)),
            tkt("TKT-B", depends_on=("TKT-A",), files=("b.ts",)),
            tkt("TKT-C", depends_on=("TKT-B",), files=("c.ts",)),
        ]
    )
    order = [t for w in waves for t in w]
    # every dependency precedes its dependent in the flattened schedule
    assert order.index("TKT-A") < order.index("TKT-B") < order.index("TKT-C")


def test_compute_waves_rejects_cycle_and_unknown_dep():
    with pytest.raises(RehearsalError, match="cycle"):
        compute_waves(
            [
                tkt("TKT-A", depends_on=("TKT-B",)),
                tkt("TKT-B", depends_on=("TKT-A",)),
            ]
        )
    with pytest.raises(RehearsalError, match="unknown ticket"):
        compute_waves([tkt("TKT-A", depends_on=("TKT-Z",))])


def test_file_ownership_conflicts_is_the_shared_definition():
    # The wave scheduler and the planning lint read the same definition (R8).
    mapping = {"TKT-A": ["x.ts"], "TKT-B": ["x.ts", "y.ts"], "TKT-C": ["z.ts"]}
    conflicts = file_ownership_conflicts(mapping)
    assert set(conflicts) == {"x.ts"}
    assert set(conflicts["x.ts"]) == {"TKT-A", "TKT-B"}


# --- run_rehearsal: adopt (scenario 2) ---------------------------------------


def test_all_green_rehearsal_adopts_fanout_artifact_head_moves(tmp_path):
    store = make_store(tmp_path / "lib")
    ws, base_ref, converged = make_workspace(tmp_path / "ws")
    episode_id, run_id = setup_increment(
        store,
        [
            {"id": "TKT-A", "files": ["a.ts"]},
            {"id": "TKT-B", "depends_on": ["TKT-A"], "files": ["b.ts"]},
        ],
    )
    stages = RehearsalStages(
        oneshot_fn=make_oneshot({"TKT-A": True, "TKT-B": True}),
        gate_fn=always(True),
        verify_fn=always(True),
    )

    outcome = run_rehearsal(
        store,
        episode_id=episode_id,
        run_id=run_id,
        workspace=ws,
        increment_base_ref=base_ref,
        stages=stages,
        config=CONFIG,
        work_dir=tmp_path / "rh",
    )

    assert outcome.adopted is True
    # The workspace HEAD moved off the converged artifact onto the fan-out one.
    new_head = current_head(ws)
    assert new_head != converged
    assert new_head == outcome.adopted_ref
    # The fan-out artifact is what's on disk now (converged-only file gone, the
    # ticket files present).
    assert not (ws.root / "converged.txt").exists()
    assert (ws.root / "TKT-A.ts").exists() and (ws.root / "TKT-B.ts").exists()
    # Metric: a clean increment one-shot.
    assert outcome.metric.increment_one_shot is True
    assert outcome.metric.ticket_one_shot_rate == 1.0
    assert outcome.metric.tickets_passed == 2
    assert outcome.metric.snapshot_id == store.current_snapshot_id()
    assert outcome.integration_failure is None
    # No worktrees leaked.
    assert worktree_count(ws) == 1
    # Persisted.
    doc = load_rehearsal_outcome(store, run_id)
    assert doc["adopted"] is True
    assert doc["metric"]["ticket_one_shot_rate"] == 1.0


# --- run_rehearsal: fallback (scenario 3 + headline class) -------------------


def test_one_failed_ticket_falls_back_and_writes_integration_failure_record(
    tmp_path,
):
    store = make_store(tmp_path / "lib")
    ws, base_ref, converged = make_workspace(tmp_path / "ws")
    episode_id, run_id = setup_increment(
        store,
        [
            {"id": "TKT-A", "files": ["a.ts"]},
            {"id": "TKT-B", "depends_on": ["TKT-A"], "files": ["b.ts"]},
        ],
    )
    stages = RehearsalStages(
        oneshot_fn=make_oneshot({"TKT-A": True, "TKT-B": False}),
        gate_fn=always(True),
        verify_fn=always(True),
    )

    outcome = run_rehearsal(
        store,
        episode_id=episode_id,
        run_id=run_id,
        workspace=ws,
        increment_base_ref=base_ref,
        stages=stages,
        config=CONFIG,
        work_dir=tmp_path / "rh",
    )

    assert outcome.adopted is False
    assert outcome.adopted_ref is None
    # Fallback: the workspace stays exactly on the converged artifact.
    assert current_head(ws) == converged
    assert (ws.root / "converged.txt").exists()
    # The typed integration-failure record is written (R5).
    rec = outcome.integration_failure
    assert rec is not None
    assert rec["failure_kind"] == REHEARSAL_INTEGRATION_FAILURE_KIND
    assert rec["failure_class"] == FAILURE_CLASS_TICKET
    assert rec["failed_tickets"] == ["TKT-B"]
    assert rec["base_ref"] == base_ref
    # Metric: half the tickets one-shot, the increment did not.
    assert outcome.metric.increment_one_shot is False
    assert outcome.metric.ticket_one_shot_rate == 0.5
    assert worktree_count(ws) == 1
    assert load_rehearsal_outcome(store, run_id)["adopted"] is False


def test_passed_alone_broke_together_records_integration_failure_class(tmp_path):
    store = make_store(tmp_path / "lib")
    ws, base_ref, converged = make_workspace(tmp_path / "ws")
    episode_id, run_id = setup_increment(
        store,
        [
            {"id": "TKT-A", "files": ["a.ts"]},
            {"id": "TKT-B", "files": ["b.ts"]},
        ],
    )
    # Every ticket passes its own one-shot, but the integration gate fails: the
    # headline "passed alone, broke together" class (R5).
    stages = RehearsalStages(
        oneshot_fn=make_oneshot({"TKT-A": True, "TKT-B": True}),
        gate_fn=always(False),
        verify_fn=always(True),
    )

    outcome = run_rehearsal(
        store,
        episode_id=episode_id,
        run_id=run_id,
        workspace=ws,
        increment_base_ref=base_ref,
        stages=stages,
        config=CONFIG,
        work_dir=tmp_path / "rh",
    )

    assert outcome.adopted is False
    assert current_head(ws) == converged
    assert outcome.metric.ticket_one_shot_rate == 1.0  # passed alone
    assert outcome.metric.increment_one_shot is False  # broke together
    rec = outcome.integration_failure
    assert rec["failure_class"] == FAILURE_CLASS_INTEGRATION
    assert rec["failed_tickets"] == []  # no ticket failed alone
    assert "gate" in rec["location"]


# --- one-shot rates across fixtures (scenario 4) -----------------------------


@pytest.mark.parametrize(
    "pass_map, gate, verify, exp_rate, exp_increment",
    [
        ({"A": True, "B": True, "C": True}, True, True, 1.0, True),
        ({"A": True, "B": False, "C": True}, True, True, 2 / 3, False),
        ({"A": False, "B": False, "C": False}, True, True, 0.0, False),
        ({"A": True, "B": True, "C": True}, True, False, 1.0, False),
    ],
)
def test_one_shot_rates_computed_across_fixtures(
    tmp_path, pass_map, gate, verify, exp_rate, exp_increment
):
    store = make_store(tmp_path / "lib")
    ws, base_ref, _ = make_workspace(tmp_path / "ws")
    episode_id, run_id = setup_increment(
        store,
        [{"id": "A", "files": ["a.ts"]},
         {"id": "B", "files": ["b.ts"]},
         {"id": "C", "files": ["c.ts"]}],
    )
    stages = RehearsalStages(
        oneshot_fn=make_oneshot(pass_map),
        gate_fn=always(gate),
        verify_fn=always(verify),
    )
    outcome = run_rehearsal(
        store,
        episode_id=episode_id,
        run_id=run_id,
        workspace=ws,
        increment_base_ref=base_ref,
        stages=stages,
        config=CONFIG,
        work_dir=tmp_path / "rh",
    )
    assert outcome.metric.rehearsal_tickets == 3
    assert outcome.metric.ticket_one_shot_rate == pytest.approx(exp_rate)
    assert outcome.metric.increment_one_shot is exp_increment
    # Ordinary episodes cannot measure episode-one-shot.
    assert outcome.metric.episode_one_shot is None


def test_rebuild_probe_mode_carries_episode_one_shot(tmp_path):
    store = make_store(tmp_path / "lib")
    ws, base_ref, _ = make_workspace(tmp_path / "ws")
    episode_id, run_id = setup_increment(store, [{"id": "A", "files": ["a.ts"]}])
    stages = RehearsalStages(
        oneshot_fn=make_oneshot({"A": True}),
        gate_fn=always(True),
        verify_fn=always(True),
    )
    outcome = run_rehearsal(
        store,
        episode_id=episode_id,
        run_id=run_id,
        workspace=ws,
        increment_base_ref=base_ref,
        stages=stages,
        config=CONFIG,
        work_dir=tmp_path / "rh",
        mode=REBUILD_PROBE_MODE,
        episode_one_shot=True,
    )
    assert outcome.metric.mode == REBUILD_PROBE_MODE
    assert outcome.metric.episode_one_shot is True


# --- run-memory consumption (R5) ---------------------------------------------


def test_oneshot_sessions_consume_run_memory_workflows(tmp_path):
    store = make_store(tmp_path / "lib")
    ws, base_ref, _ = make_workspace(tmp_path / "ws")
    episode_id, run_id = setup_increment(store, [{"id": "A", "files": ["a.ts"]}])
    # A live episode workflow the fan-out session must receive (R5).
    wf_id = store.insert_workflow(
        episode_id,
        precondition="when adding a route",
        action="register it in the router",
        expected_outcome="the route resolves",
        run_id=run_id,
    )
    seen: dict = {}
    stages = RehearsalStages(
        oneshot_fn=make_oneshot({"A": True}, seen=seen),
        gate_fn=always(True),
        verify_fn=always(True),
    )
    run_rehearsal(
        store,
        episode_id=episode_id,
        run_id=run_id,
        workspace=ws,
        increment_base_ref=base_ref,
        stages=stages,
        config=CONFIG,
        work_dir=tmp_path / "rh",
    )
    assert wf_id in seen["A"]["workflow_ids"]
    assert "register it in the router" in seen["A"]["section"]


# --- sampling (scenario 5) ---------------------------------------------------


def test_sampling_honors_budget_share():
    policy = RehearsalSamplingPolicy(
        baseline_increments=2, sample_every=3, max_budget_share=0.2
    )
    budget = 100.0

    # Baseline increments rehearse when under the budget cap.
    assert policy.should_rehearse(
        1, rehearsal_spend_usd=0.0, episode_budget_usd=budget
    ).rehearse
    assert policy.should_rehearse(
        2, rehearsal_spend_usd=0.0, episode_budget_usd=budget
    ).rehearse

    # After the baseline, only every-3rd increment rehearses.
    assert policy.should_rehearse(
        3, rehearsal_spend_usd=5.0, episode_budget_usd=budget
    ).rehearse
    skip = policy.should_rehearse(
        4, rehearsal_spend_usd=5.0, episode_budget_usd=budget
    )
    assert not skip.rehearse and skip.reason.startswith("sample")

    # The budget share dominates: once spend hits 20% of the budget, nothing
    # rehearses — not even a cadence hit, not even a baseline increment.
    budget_skip = policy.should_rehearse(
        3, rehearsal_spend_usd=25.0, episode_budget_usd=budget
    )
    assert not budget_skip.rehearse and budget_skip.reason.startswith("budget")
    baseline_over_budget = policy.should_rehearse(
        1, rehearsal_spend_usd=30.0, episode_budget_usd=budget
    )
    assert not baseline_over_budget.rehearse
    assert baseline_over_budget.reason.startswith("budget")


def test_sampling_policy_validates_share():
    with pytest.raises(RehearsalError, match="max_budget_share"):
        RehearsalSamplingPolicy(
            baseline_increments=1, sample_every=1, max_budget_share=1.5
        )


# --- crossover (scenario 6) --------------------------------------------------


def test_crossover_report_compares_both_regimes():
    # Early: converge-first is cheaper, no crossover.
    early = crossover_report(
        RegimeCost(REGIME_CONVERGE_FIRST, 10.0),
        RegimeCost(REGIME_FANOUT_FIRST, 12.0),
        current_regime=REGIME_CONVERGE_FIRST,
        ticket_one_shot_rate=0.4,
    )
    assert early["converge_first_usd"] == 10.0
    assert early["fanout_first_usd"] == 12.0
    assert early["crossover_reached"] is False
    assert early["recommended_regime"] == REGIME_CONVERGE_FIRST
    assert early["flip_recommended"] is False
    assert early["delta_usd"] == pytest.approx(2.0)

    # Late: fan-out-first crosses under converge-first — flip recommended.
    late = crossover_report(
        RegimeCost(REGIME_CONVERGE_FIRST, 10.0),
        RegimeCost(REGIME_FANOUT_FIRST, 8.0),
        current_regime=REGIME_CONVERGE_FIRST,
        ticket_one_shot_rate=0.8,
    )
    assert late["crossover_reached"] is True
    assert late["recommended_regime"] == REGIME_FANOUT_FIRST
    assert late["flip_recommended"] is True
    assert late["delta_usd"] == pytest.approx(-2.0)


# --- probe promotion (R6) ----------------------------------------------------


def test_should_promote_probe_on_must_tier_parity():
    assert should_promote_probe(0.90, 0.85) is True   # probe beats engagement
    assert should_promote_probe(0.85, 0.85) is True   # parity promotes
    assert should_promote_probe(0.80, 0.85) is False  # probe worse: keep clone


# --- verification: metric appears in settlement reports ----------------------


def test_metric_appears_in_settlement_report(tmp_path):
    store = make_store(tmp_path / "lib")
    ws, base_ref, _ = make_workspace(tmp_path / "ws")
    episode_id, run_id = setup_increment(
        store,
        [{"id": "A", "files": ["a.ts"]}, {"id": "B", "files": ["b.ts"]}],
    )
    stages = RehearsalStages(
        oneshot_fn=make_oneshot({"A": True, "B": False}),
        gate_fn=always(True),
        verify_fn=always(True),
    )
    run_rehearsal(
        store,
        episode_id=episode_id,
        run_id=run_id,
        workspace=ws,
        increment_base_ref=base_ref,
        stages=stages,
        config=CONFIG,
        work_dir=tmp_path / "rh",
    )

    report = assemble_report(
        episode_id=episode_id,
        snapshot_id=store.current_snapshot_id(),
        target=TARGET,
        verdicts=[],
        scen_ids={},
    )
    enriched = attach_one_shot_metrics(report, store, episode_id)
    one_shot = enriched["one_shot"]
    assert one_shot["rehearsed_increments"] == 1
    assert one_shot["ticket_one_shot_rate"] == pytest.approx(0.5)
    assert one_shot["increment_one_shot_rate"] == pytest.approx(0.0)
    assert one_shot["increments"][0]["target"] == TARGET
    # The rubric-side report is preserved alongside the new section.
    assert "score" in enriched


def test_attach_one_shot_metrics_when_nothing_rehearsed(tmp_path):
    store = make_store(tmp_path / "lib")
    episode_id, _ = setup_increment(store, [{"id": "A", "files": ["a.ts"]}])
    enriched = attach_one_shot_metrics({"score": {}}, store, episode_id)
    assert enriched["one_shot"]["rehearsed_increments"] == 0
    assert enriched["one_shot"]["increments"] == []


# --- verification: fallback ≡ no-rehearsal run -------------------------------


def test_fallback_leaves_episode_equivalent_to_no_rehearsal(tmp_path):
    store = make_store(tmp_path / "lib")
    ws, base_ref, converged = make_workspace(tmp_path / "ws")
    episode_id, run_id = setup_increment(
        store, [{"id": "A", "files": ["a.ts"]}, {"id": "B", "files": ["b.ts"]}]
    )

    insights_before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights"
    ).fetchone()["n"]
    workflows_before = store.live_workflows(episode_id)

    stages = RehearsalStages(
        oneshot_fn=make_oneshot({"A": False, "B": True}),
        gate_fn=always(True),
        verify_fn=always(True),
    )
    outcome = run_rehearsal(
        store,
        episode_id=episode_id,
        run_id=run_id,
        workspace=ws,
        increment_base_ref=base_ref,
        stages=stages,
        config=CONFIG,
        work_dir=tmp_path / "rh",
    )

    assert outcome.adopted is False
    # The workspace is byte-for-byte where convergence left it.
    assert current_head(ws) == converged
    assert (ws.root / "converged.txt").read_text(encoding="utf-8") == "converged\n"
    # No library writes — rehearsal never registers ideas.
    insights_after = store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights"
    ).fetchone()["n"]
    assert insights_after == insights_before
    assert store.live_workflows(episode_id) == workflows_before
    # No worktrees or branches leaked.
    assert worktree_count(ws) == 1
    branches = _git(ws.root, "branch", "--list")
    assert "af-rh" not in branches


# --- episode.py rehearsal-seam touch (005 U2) --------------------------------


def test_episode_state_round_trips_increment_base_ref():
    from agent_families.pipeline.episode import _EpisodeState

    state = _EpisodeState(
        workspace_root="ws", slice_size=2, increment_base_ref="deadbeef"
    )
    restored = _EpisodeState.from_json(state.to_json())
    assert restored.increment_base_ref == "deadbeef"
    # Backward compatible: a checkpoint written before this field defaults to "".
    import json

    legacy = json.loads(state.to_json())
    del legacy["increment_base_ref"]
    assert (
        _EpisodeState.from_json(json.dumps(legacy)).increment_base_ref == ""
    )


def _episode_ctx_and_stages(tmp_path, rehearsal_fn):
    from agent_families.pipeline.episode import EpisodeContext, EpisodeStages

    store = make_store(tmp_path / "lib")
    ws, _base, _conv = make_workspace(tmp_path / "ws")
    episode_id = store.create_episode(
        TARGET, DIGEST, store.current_snapshot_id(),
        max_increments=1, cost_ceiling_usd=1.0,
    )
    ctx = EpisodeContext(
        store=store,
        episode_id=episode_id,
        target=TARGET,
        digest=DIGEST,
        workspace=ws,
        reset_target=lambda: None,
    )
    noop = lambda *a, **k: None  # noqa: E731
    stages = EpisodeStages(
        request_fn=noop,
        increment_fn=noop,
        uat_fn=noop,
        reset_target_fn=noop,
        settle_fn=noop,
        rehearsal_fn=rehearsal_fn,
    )
    return ctx, stages


def test_run_rehearsal_seam_invokes_with_base_ref(tmp_path):
    from agent_families.pipeline.episode import _run_rehearsal

    calls = []
    ctx, stages = _episode_ctx_and_stages(
        tmp_path, lambda *a: calls.append(a)
    )
    _run_rehearsal(stages, ctx, 3, 42, "basesha")
    assert calls == [(ctx, 3, 42, "basesha")]


def test_run_rehearsal_seam_swallows_failure_and_skips_without_base_ref(
    tmp_path,
):
    from agent_families.pipeline.episode import _run_rehearsal

    calls = []

    def boom(*a):
        calls.append(a)
        raise RuntimeError("rehearsal exploded")

    ctx, stages = _episode_ctx_and_stages(tmp_path, boom)
    # An exploding seam must never propagate — the episode advances unchanged.
    _run_rehearsal(stages, ctx, 1, 7, "base")
    assert len(calls) == 1
    # No base ref captured → the seam is skipped entirely (nothing to fan out).
    _run_rehearsal(stages, ctx, 1, 7, "")
    assert len(calls) == 1
