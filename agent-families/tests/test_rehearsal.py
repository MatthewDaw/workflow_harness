"""Rehearsal pass and one-shot metric tests (plan-005 U2, R5-R8).

Fully offline and fake-driven per the plan's approach: the per-ticket one-shot
session, the integration verify, the workspace adopt, and the workflow injection
are scripted seams; the store is real (rehearsal records, run-memory workflows,
and the episode equivalence are exercised, never mocked). No embedding model,
real judge, or ``claude`` subprocess ever runs — the suite passes with zero quota
and no ``claude`` on PATH.

## Conformance

Each plan-005 U2 test scenario / invariant maps to a behavioral test here:

- diamond DAG rehearses in waves with the conflicting-files pair serialized ->
  ``test_diamond_dag_rehearses_in_waves_with_conflicting_pair_serialized``,
  ``test_nonconflicting_tickets_share_a_wave``,
  ``test_file_conflict_pairs_and_planning_helper_agree``
- all-green rehearsal adopts the fan-out artifact (workspace head moves) ->
  ``test_all_green_rehearsal_adopts_fanout_artifact``
- one failed ticket falls back to the converged artifact and writes the
  integration-failure record ->
  ``test_passed_alone_broke_together_falls_back_and_records``,
  ``test_failed_ticket_one_shot_falls_back``
- one-shot sessions consume the episode's run-memory workflows ->
  ``test_one_shot_sessions_consume_run_memory_ledger``
- one-shot rates computed correctly across fixtures ->
  ``test_one_shot_rates_computed_across_fixtures``,
  ``test_rebuild_probe_measures_episode_one_shot``,
  ``test_one_shot_is_zero_iteration_only``
- sampling honors the budget share -> ``test_sampling_honors_budget_share``
- crossover report compares both regimes' measured costs ->
  ``test_crossover_report_compares_both_regimes``

Verification (plan-005 U2): the metric appears in settlement reports ->
``test_metric_persisted_and_surfaced_for_settlement``; the fallback path leaves
the episode equivalent to a no-rehearsal run ->
``test_fallback_leaves_episode_equivalent_to_no_rehearsal``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_families.grading.registry import FeatureCandidate, mint_feat
from agent_families.pipeline import rehearsal as rh
from agent_families.pipeline.episode import EpisodeConfig, EpisodeStages, run_episode
from agent_families.pipeline.explorer import write_explorer_msg
from agent_families.pipeline.orchestrator import RunResult
from agent_families.pipeline.planning import file_ownership_conflict_pairs
from agent_families.store import Store

TARGET = "linkding"
DIGEST = "sha256:0123456789abcdef"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


def ticket(tid, *, depends_on=(), files=()):
    return rh.RehearsalTicket(
        ticket_id=tid, depends_on=tuple(depends_on), files=tuple(files)
    )


def passing(_t, _injection):
    return rh.RehearsalTicketResult(ticket_id=_t.ticket_id, passed=True)


def integration_pass(_results):
    return rh.IntegrationResult(passed=True)


def integration_fail(_results):
    return rh.IntegrationResult(passed=False, detail="planted interaction defect")


# --- wave scheduling (R8) ----------------------------------------------------


def _wave_of(waves, tid):
    for i, wave in enumerate(waves):
        if tid in wave:
            return i
    raise AssertionError(f"{tid} is in no wave: {waves}")


def test_diamond_dag_rehearses_in_waves_with_conflicting_pair_serialized():
    # Diamond: A -> {B, C} -> D, with B and C claiming a SHARED file. The
    # file-ownership lint, promoted to enforcement (R8), must serialize B and C
    # into different waves; D still follows both.
    tickets = [
        ticket("TKT-A", files=["a.ts"]),
        ticket("TKT-B", depends_on=["TKT-A"], files=["shared.ts"]),
        ticket("TKT-C", depends_on=["TKT-A"], files=["shared.ts"]),
        ticket("TKT-D", depends_on=["TKT-B", "TKT-C"], files=["d.ts"]),
    ]
    waves = rh.compute_waves(tickets)

    # The conflicting pair is serialized: never in the same wave.
    assert _wave_of(waves, "TKT-B") != _wave_of(waves, "TKT-C")
    # Dependencies precede dependents across waves (topological soundness).
    assert _wave_of(waves, "TKT-A") < _wave_of(waves, "TKT-B")
    assert _wave_of(waves, "TKT-A") < _wave_of(waves, "TKT-C")
    assert _wave_of(waves, "TKT-D") > _wave_of(waves, "TKT-B")
    assert _wave_of(waves, "TKT-D") > _wave_of(waves, "TKT-C")
    # Waves are contiguous and gap-free.
    assert [t for wave in waves for t in wave].count("TKT-B") == 1
    flat = [t for wave in waves for t in wave]
    assert sorted(flat) == ["TKT-A", "TKT-B", "TKT-C", "TKT-D"]


def test_two_conflicting_independents_serialize_into_two_waves():
    # The minimal serialization case: two independent tickets that conflict on a
    # file run in two waves (never one), even though the DAG would allow parallel.
    waves = rh.compute_waves(
        [ticket("TKT-B", files=["shared.ts"]), ticket("TKT-C", files=["shared.ts"])]
    )
    assert len(waves) == 2
    assert waves == (("TKT-B",), ("TKT-C",))


def test_nonconflicting_tickets_share_a_wave():
    # Same DAG level, DISJOINT files -> they parallelize in one wave.
    waves = rh.compute_waves(
        [
            ticket("TKT-A", files=["a.ts"]),
            ticket("TKT-B", depends_on=["TKT-A"], files=["b.ts"]),
            ticket("TKT-C", depends_on=["TKT-A"], files=["c.ts"]),
        ]
    )
    assert _wave_of(waves, "TKT-B") == _wave_of(waves, "TKT-C")
    assert waves[0] == ("TKT-A",)
    assert waves[1] == ("TKT-B", "TKT-C")


def test_file_conflict_pairs_and_planning_helper_agree():
    # The rehearsal scheduler and the promoted planning lint compute the same
    # conflict pairs from the same plan (R8 — one mechanism, two call sites).
    plan_tickets = [
        {"id": "TKT-A", "files": ["a.ts", "shared.ts"]},
        {"id": "TKT-B", "files": ["shared.ts"]},
        {"id": "TKT-C", "files": ["c.ts"]},
    ]
    from_plan = file_ownership_conflict_pairs(plan_tickets)
    from_rehearsal = rh.file_conflict_pairs(rh.tickets_from_plan({"tickets": plan_tickets}))
    assert from_plan == from_rehearsal == [("TKT-A", "TKT-B")]


def test_compute_waves_rejects_cycle():
    with pytest.raises(rh.RehearsalError, match="cycle"):
        rh.compute_waves(
            [
                ticket("TKT-A", depends_on=["TKT-B"]),
                ticket("TKT-B", depends_on=["TKT-A"]),
            ]
        )


# --- adopt / fallback (R5) ---------------------------------------------------


def test_all_green_rehearsal_adopts_fanout_artifact(store):
    head = {"moved": False}
    tickets = [ticket("TKT-A"), ticket("TKT-B", depends_on=["TKT-A"])]
    result = rh.run_rehearsal(
        store,
        episode_id=1,
        increment_index=1,
        run_id=7,
        tickets=tickets,
        rehearse_fn=passing,
        integrate_fn=integration_pass,
        adopt_fn=lambda: head.__setitem__("moved", True),
    )
    assert result.adopted is True
    assert head["moved"] is True  # the workspace head moved to the fan-out
    assert result.integration_attempted and result.integration_passed
    assert result.integration_failure is None
    assert result.metric.increment_one_shot is True
    assert result.metric.ticket_one_shot_rate == 1.0


def test_passed_alone_broke_together_falls_back_and_records(store):
    # Every ticket passes its own one-shot, but the merged fan-out fails the
    # single integration verify -> fall back, record passed_alone_broke_together,
    # and DO NOT move the workspace head.
    head = {"moved": False}
    tickets = [ticket("TKT-A"), ticket("TKT-B")]
    result = rh.run_rehearsal(
        store,
        episode_id=2,
        increment_index=1,
        run_id=9,
        tickets=tickets,
        rehearse_fn=passing,
        integrate_fn=integration_fail,
        adopt_fn=lambda: head.__setitem__("moved", True),
    )
    assert result.adopted is False
    assert head["moved"] is False  # fallback never moves the head
    assert result.integration_attempted is True
    assert result.integration_passed is False
    failure = result.integration_failure
    assert failure is not None
    assert failure.kind == rh.INTEGRATION_FAILURE_KIND == "passed_alone_broke_together"
    assert failure.passed_alone == ("TKT-A", "TKT-B")
    assert "planted interaction defect" in failure.observed
    assert result.metric.increment_one_shot is False
    # The typed record is persisted and queryable for the reflector.
    records = rh.rehearsal_records(store, 2)
    assert records[0]["integration_failure"]["kind"] == "passed_alone_broke_together"


def test_failed_ticket_one_shot_falls_back(store):
    # One ticket fails its one-shot -> fall back with NO integration attempt
    # (this is not a passed-alone-broke-together case).
    def one_fails(t, _injection):
        return rh.RehearsalTicketResult(
            ticket_id=t.ticket_id, passed=(t.ticket_id != "TKT-B")
        )

    head = {"moved": False}
    tickets = [ticket("TKT-A"), ticket("TKT-B"), ticket("TKT-C")]
    result = rh.run_rehearsal(
        store,
        episode_id=3,
        increment_index=1,
        run_id=11,
        tickets=tickets,
        rehearse_fn=one_fails,
        integrate_fn=integration_pass,
        adopt_fn=lambda: head.__setitem__("moved", True),
    )
    assert result.adopted is False
    assert result.integration_attempted is False
    assert result.integration_failure is None
    assert head["moved"] is False
    assert result.metric.tickets_passed == 2
    assert result.metric.tickets_total == 3
    assert result.metric.ticket_one_shot_rate == pytest.approx(2 / 3)
    assert result.metric.increment_one_shot is False


def test_one_shot_is_zero_iteration_only(store):
    # The no-Ralph-loop contract is enforced: a seam reporting > 1 iteration is a
    # bug, not a one-shot.
    def two_iterations(t, _injection):
        return rh.RehearsalTicketResult(ticket_id=t.ticket_id, passed=True, iterations=2)

    with pytest.raises(rh.RehearsalError, match="one-shot"):
        rh.run_rehearsal(
            store,
            episode_id=4,
            increment_index=1,
            run_id=13,
            tickets=[ticket("TKT-A")],
            rehearse_fn=two_iterations,
            integrate_fn=integration_pass,
        )


# --- run-memory consumption (R5) ---------------------------------------------


def test_one_shot_sessions_consume_run_memory_ledger(store):
    # The one-shot sessions receive the episode's live run-memory workflows under
    # the standard injection budget — what makes one-shot rates a learning curve.
    episode_id = store.create_episode(TARGET, DIGEST, store.current_snapshot_id())
    store.insert_workflow(
        episode_id,
        precondition="adding a bookmark model field",
        action="write the migration then the form",
        expected_outcome="the field persists and renders",
    )
    seen = {}

    def capture(t, injection):
        seen[t.ticket_id] = injection
        return rh.RehearsalTicketResult(ticket_id=t.ticket_id, passed=True)

    rh.run_rehearsal(
        store,
        episode_id=episode_id,
        increment_index=1,
        run_id=21,
        tickets=[ticket("TKT-A")],
        rehearse_fn=capture,
        integrate_fn=integration_pass,
        compose_injection_fn=lambda: rh.episode_workflow_injection(
            store, episode_id, budget_tokens=500
        ),
    )
    injection = seen["TKT-A"]
    assert "Run memory" in injection
    assert "write the migration then the form" in injection


# --- the one-shot metric (R6) ------------------------------------------------


@pytest.mark.parametrize(
    "outcomes, integ_passed, expected_rate, expected_increment",
    [
        ([True, True, True, True], True, 1.0, True),
        ([True, True, True, False], True, 0.75, False),
        ([False, False], True, 0.0, False),
        ([True, True], False, 1.0, False),  # passed alone, broke together
    ],
)
def test_one_shot_rates_computed_across_fixtures(
    store, outcomes, integ_passed, expected_rate, expected_increment
):
    tickets = [ticket(f"TKT-{i}") for i in range(len(outcomes))]
    verdicts = dict(zip([t.ticket_id for t in tickets], outcomes))

    def rehearse(t, _injection):
        return rh.RehearsalTicketResult(
            ticket_id=t.ticket_id, passed=verdicts[t.ticket_id]
        )

    result = rh.run_rehearsal(
        store,
        episode_id=5,
        increment_index=1,
        run_id=31,
        tickets=tickets,
        rehearse_fn=rehearse,
        integrate_fn=(integration_pass if integ_passed else integration_fail),
        persist=False,
    )
    assert result.metric.ticket_one_shot_rate == pytest.approx(expected_rate)
    assert result.metric.increment_one_shot is expected_increment
    # Episode one-shot is undefined for an ordinary increment rehearsal (R6).
    assert result.metric.episode_one_shot is None


@pytest.mark.parametrize("integ_passed, expected", [(True, True), (False, False)])
def test_rebuild_probe_measures_episode_one_shot(store, integ_passed, expected):
    result = rh.run_rehearsal(
        store,
        episode_id=6,
        increment_index=1,
        run_id=41,
        tickets=[ticket("TKT-A"), ticket("TKT-B")],
        rehearse_fn=passing,
        integrate_fn=(integration_pass if integ_passed else integration_fail),
        mode=rh.MODE_REBUILD_PROBE,
        persist=False,
    )
    # Only a rebuild-probe can measure episode one-shot (fresh workspace, full-app
    # opening prompt, one delivery pass).
    assert result.metric.episode_one_shot is expected
    assert result.metric.mode == "rebuild_probe"


# --- sampling + crossover (R7) -----------------------------------------------


def test_sampling_honors_budget_share():
    policy = rh.SamplingPolicy(baseline_increments=1, max_budget_share=0.2)
    # Baseline period: always rehearse, regardless of share.
    base = rh.should_rehearse(
        policy,
        increment_index=1,
        rehearsal_cost_so_far=0.0,
        projected_rehearsal_cost=50.0,
        episode_budget=100.0,
    )
    assert base.rehearse is True

    # After baseline: rehearse only while the projected share stays within budget.
    within = rh.should_rehearse(
        policy,
        increment_index=2,
        rehearsal_cost_so_far=15.0,
        projected_rehearsal_cost=4.0,
        episode_budget=100.0,
    )
    assert within.rehearse is True
    assert within.projected_share == pytest.approx(0.19)

    over = rh.should_rehearse(
        policy,
        increment_index=2,
        rehearsal_cost_so_far=15.0,
        projected_rehearsal_cost=10.0,
        episode_budget=100.0,
    )
    assert over.rehearse is False
    assert over.projected_share == pytest.approx(0.25)


def test_sampling_policy_validates():
    with pytest.raises(rh.RehearsalError):
        rh.SamplingPolicy(baseline_increments=-1, max_budget_share=0.2)
    with pytest.raises(rh.RehearsalError):
        rh.SamplingPolicy(baseline_increments=1, max_budget_share=0.0)


def test_crossover_report_compares_both_regimes():
    # Fan-out-first cheaper -> the graduation point (crossed over).
    crossed = rh.cost_model_crossover(
        converge_first_cost=12.0, fan_out_first_cost=9.0
    )
    assert crossed.cheaper_regime == rh.REGIME_FAN_OUT_FIRST
    assert crossed.crossed_over is True
    assert crossed.savings == pytest.approx(3.0)
    # Both regimes' measured costs are reported, not just the winner.
    as_dict = crossed.as_dict()
    assert as_dict["converge_first_cost"] == 12.0
    assert as_dict["fan_out_first_cost"] == 9.0

    # Converge-first still cheaper -> not yet crossed over.
    not_yet = rh.cost_model_crossover(
        converge_first_cost=8.0, fan_out_first_cost=11.0
    )
    assert not_yet.cheaper_regime == rh.REGIME_CONVERGE_FIRST
    assert not_yet.crossed_over is False


# --- settlement surfacing (R6 verification) ----------------------------------


def test_metric_persisted_and_surfaced_for_settlement(store):
    # Two increments rehearsed: one adopted, one passed-alone-broke-together.
    rh.run_rehearsal(
        store,
        episode_id=8,
        increment_index=1,
        run_id=51,
        tickets=[ticket("TKT-A"), ticket("TKT-B")],
        rehearse_fn=passing,
        integrate_fn=integration_pass,
    )
    rh.run_rehearsal(
        store,
        episode_id=8,
        increment_index=2,
        run_id=52,
        tickets=[ticket("TKT-A"), ticket("TKT-B")],
        rehearse_fn=passing,
        integrate_fn=integration_fail,
    )
    section = rh.rehearsal_metric_section(store, 8)
    assert section["increments_rehearsed"] == 2
    assert section["increment_one_shot_rate"] == pytest.approx(0.5)
    assert section["mean_ticket_one_shot_rate"] == pytest.approx(1.0)
    assert len(section["per_increment"]) == 2
    # The integration-failure class is surfaced for the reflector.
    failures = section["integration_failures"]
    assert len(failures) == 1
    assert failures[0]["increment_index"] == 2
    assert failures[0]["record"]["kind"] == "passed_alone_broke_together"
    # The section is JSON-serializable (the report assembler embeds it).
    json.dumps(section)


# --- episode equivalence (the fallback verification) -------------------------


def make_store(base: Path) -> Store:
    base.mkdir(parents=True, exist_ok=True)
    s = Store(base / "library.db")
    s.migrate()
    return s


def make_ws(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "index.ts").write_text(
        "export const answer = 42\n", encoding="utf-8", newline="\n"
    )
    for args in (
        ("init", "--initial-branch=main"),
        ("config", "user.name", "t"),
        ("config", "user.email", "t@localhost"),
        ("config", "commit.gpgsign", "false"),
        ("config", "core.autocrlf", "false"),
        ("add", "-A"),
        ("commit", "-m", "init"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        )
    return root


def _git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()


def _mint_feat(store, key):
    return mint_feat(
        store,
        FeatureCandidate(
            key=key,
            area="bookmarks",
            behavior=f"behavior {key}",
            route="/bookmarks",
            confirm_steps=({"action": "goto", "selector": "", "args": {}},),
            scenario_steps=(f"exercise {key}",),
            expected_outcome="it works",
            tier="must",
        ),
        f"evidence/{key}.json",
        target=TARGET,
        digest=DIGEST,
    )


def _single_increment_stages(rehearse_fn=None):
    """One-increment fake episode stages (delivers one done ticket, accepted)."""

    def request_fn(ctx, increment_index, slice_ids):
        msg_id = f"MSG-e{ctx.episode_id}-inc{increment_index:03d}-open"
        write_explorer_msg(ctx.store, msg_id, "build it", slice_ids)
        return SimpleNamespace(msg_id=msg_id, text="build it")

    def increment_fn(inc):
        store = inc.store
        run_id = store.create_run(
            f"episode:{inc.episode_id}",
            store.current_snapshot_id(),
            episode_id=inc.episode_id,
            increment_index=inc.increment_index,
        )
        rid = f"REQ-r{run_id}-000"
        tid = f"TKT-r{run_id}-000"
        with store.transaction():
            store.conn.execute(
                "INSERT INTO trace_req (id, source_msg_id) VALUES (?, ?)",
                (rid, inc.request_msg_id),
            )
            store.conn.execute(
                "INSERT INTO trace_tkt (id, status) VALUES (?, 'pending')", (tid,)
            )
            store.conn.execute(
                "INSERT INTO trace_tkt_covers (tkt_id, req_id) VALUES (?, ?)",
                (tid, rid),
            )
            store.set_ticket_status(tid, "done", run_id)
            document = {
                "run_id": run_id,
                "spec_ref": f"run-{run_id}",
                "requirements": [
                    {"id": rid, "text": "req", "source_msg": inc.request_msg_id}
                ],
                "tickets": [
                    {
                        "id": tid,
                        "title": "ticket",
                        "kind": "feature",
                        "covers": [rid],
                    }
                ],
                "assumptions": [],
                "warnings": [],
            }
            store.set_meta(
                f"plan:run:{run_id}",
                json.dumps(document, sort_keys=True, ensure_ascii=False),
            )
        store.set_run_status(run_id, "success")
        return RunResult(
            run_id=run_id, status="success", ticket_statuses={tid: "done"}
        )

    def uat_fn(ctx, increment_index, run_id, briefing):
        return SimpleNamespace(verdict="accepted", feedback_msg_ids=())

    def settle_fn(ctx):
        ctx.reset_target()

    return EpisodeStages(
        request_fn=request_fn,
        increment_fn=increment_fn,
        uat_fn=uat_fn,
        reset_target_fn=lambda: None,
        settle_fn=settle_fn,
        rehearse_fn=rehearse_fn,
    )


def _run_one_increment_episode(tmp_path, name, rehearse_fn=None):
    store = make_store(tmp_path / f"{name}-lib")
    _mint_feat(store, "alpha")
    ws = make_ws(tmp_path / f"{name}-ws")
    cfg = EpisodeConfig(
        target=TARGET,
        digest=DIGEST,
        workspace_dir=ws,
        max_increments=1,
        cost_ceiling_usd=100.0,
        slice_size=1,
    )
    result = run_episode(store, cfg, _single_increment_stages(rehearse_fn))
    return store, ws, result


def test_fallback_leaves_episode_equivalent_to_no_rehearsal(tmp_path):
    # Baseline: the same one-increment episode with NO rehearsal pass.
    _, _, baseline = _run_one_increment_episode(tmp_path, "baseline")

    # The fallback rehearsal: a ticket fails its one-shot (so the pass falls back),
    # AND the workspace head is asserted unchanged across the rehearsal pass — the
    # episode must be left exactly as a no-rehearsal run would leave it.
    adopt_calls = []

    def fallback_rehearse(ctx, increment_index, run_id):
        head_before = _git_head(Path(ctx.workspace.root))
        result = rh.run_rehearsal(
            ctx.store,
            episode_id=ctx.episode_id,
            increment_index=increment_index,
            run_id=run_id,
            tickets=[rh.RehearsalTicket("TKT-x")],
            rehearse_fn=lambda t, inj: rh.RehearsalTicketResult(
                ticket_id=t.ticket_id, passed=False
            ),
            integrate_fn=integration_pass,
            adopt_fn=lambda: adopt_calls.append(1),
        )
        assert _git_head(Path(ctx.workspace.root)) == head_before
        return result

    _, ws, rehearsed = _run_one_increment_episode(
        tmp_path, "rehearsed", fallback_rehearse
    )

    # Fallback never adopts (never moves the workspace head).
    assert adopt_calls == []
    # The episode outcome is identical to the no-rehearsal run.
    assert rehearsed.status == baseline.status == "budget_spent"
    assert rehearsed.increments_completed == baseline.increments_completed == 1
    assert len(rehearsed.run_ids) == len(baseline.run_ids) == 1


def test_rehearsal_pass_error_never_breaks_the_episode(tmp_path):
    # A raised rehearsal pass is signal for the reflector, never an episode
    # failure: the episode settles exactly as it would without rehearsal.
    def boom(ctx, increment_index, run_id):
        raise RuntimeError("rehearsal blew up")

    _, _, result = _run_one_increment_episode(tmp_path, "boom", boom)
    assert result.status == "budget_spent"
    assert result.increments_completed == 1
