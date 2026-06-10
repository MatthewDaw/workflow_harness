"""plan-003 U6: episode orchestration — the delivery loop over Phase 1 runs.

Fully offline and fake-driven per the plan's approach: stages are scripted
fakes; the engagement workspace is a small real git repo (the create-or-load
contract runs for real); the store is real (FK checks, plan documents, and
the MSG/REQ accumulation are exercised, never mocked).

## Conformance

Plan test scenarios (003 U6), mapped 1:1:

- two-increment episode accumulates MSG/REQ across increments (increment 2's
  planner sees increment 1's transcript):
  ``test_two_increment_episode_accumulates_msg_req_across_increments``
- UAT rejection injects bug tickets prepended to increment 2 and they count
  against its budget:
  ``test_uat_rejection_injects_bug_tickets_prepended_and_budget_counted``
- escalated ticket carries into next plan and does not appear in UAT
  feedback: ``test_escalated_ticket_carries_into_next_plan_and_skips_uat``
- budget cap at increment boundary settles with ``budget_spent``:
  ``test_budget_cap_at_increment_boundary_settles_budget_spent``
- cost ceiling trips from aggregated run costs:
  ``test_cost_ceiling_trips_from_aggregated_run_costs``
- quota mid-increment suspends and resumes to completion:
  ``test_quota_mid_increment_suspends_and_resumes_to_completion`` (plus the
  explorer-session arm,
  ``test_quota_in_explorer_session_suspends_and_resumes``)
- ``plan_failed`` halts with human-escalation record:
  ``test_plan_failed_halts_with_human_escalation_record``
- settlement-handoff resets target before rubric:
  ``test_settlement_handoff_resets_target_before_rubric``

Verification clause (episode lifecycle property — any fake sequence
preserves: library frozen, IDs globally unique, workspace persists across
increments, every run carries episode FKs):
``test_episode_lifecycle_property_invariants``

Unit amendments (003 R1 plumbing this unit owns):

- workspace dual-mode + episode FKs on ``Orchestrator.run``:
  ``test_orchestrator_run_carries_episode_fks``,
  ``test_orchestrator_run_standalone_instantiates_per_run``,
  ``test_orchestrator_run_rejects_half_episode_fks``,
  ``test_orchestrator_run_requires_workspace_or_dest``
- engagement workspace create-or-load:
  ``test_ensure_engagement_workspace_creates_then_loads``,
  ``test_load_workspace_rejects_missing_and_non_repo``
- digest mismatch at episode setup is a hard error (R9):
  ``test_digest_mismatch_at_episode_setup_is_hard_error``
- frontier exhausted at the request boundary settles ``frontier_exhausted``:
  ``test_frontier_exhausted_settles_terminal``
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_families.grading.frontier import frontier_row, select_slice
from agent_families.grading.registry import FeatureCandidate, mint_feat
from agent_families.pipeline.episode import (
    EpisodeConfig,
    EpisodeError,
    EpisodeStages,
    episode_checkpoint_key,
    episode_cost,
    render_uat_briefing,
    resume_episode,
    run_episode,
)
from agent_families.pipeline.explorer import write_explorer_msg
from agent_families.pipeline.orchestrator import (
    GateResult,
    Orchestrator,
    OrchestratorError,
    RunResult,
    TicketSpec,
    VerifierResult,
)
from agent_families.pipeline.planning import plan_meta_key, plan_report
from agent_families.pipeline.sessions import SessionQuotaExhausted
from agent_families.pipeline.workspace import (
    WorkspaceError,
    ensure_engagement_workspace,
    load_workspace,
)
from agent_families.store import Store

TARGET = "linkding"
DIGEST = "sha256:0123456789abcdef"


# --- scaffolding -----------------------------------------------------------------


def make_store(base: Path) -> Store:
    base.mkdir(parents=True, exist_ok=True)
    store = Store(base / "library.db")
    store.migrate()
    return store


def make_ws(root: Path) -> Path:
    """A small real git repo standing in for the engagement workspace."""
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


def make_template(root: Path) -> Path:
    """A fake primed template: package.json + installed node_modules."""
    root.mkdir(parents=True)
    (root / "package.json").write_text("{}\n", encoding="utf-8", newline="\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / ".keep").write_text("", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.ts").write_text(
        "export {}\n", encoding="utf-8", newline="\n"
    )
    return root


def make_candidate(key: str) -> FeatureCandidate:
    return FeatureCandidate(
        key=key,
        area="bookmarks",
        behavior=f"behavior {key}",
        route="/bookmarks",
        confirm_steps=({"action": "goto", "selector": "", "args": {}},),
        scenario_steps=(f"exercise {key}",),
        expected_outcome="it works",
        tier="must",
    )


def mint_feats(store: Store, keys: list[str]) -> list[str]:
    return [
        mint_feat(
            store,
            make_candidate(key),
            f"evidence/{key}.json",
            target=TARGET,
            digest=DIGEST,
        )
        for key in keys
    ]


def make_cfg(ws_root: Path, **overrides) -> EpisodeConfig:
    values = dict(
        target=TARGET,
        digest=DIGEST,
        workspace_dir=ws_root,
        max_increments=5,
        cost_ceiling_usd=100.0,
        slice_size=2,
    )
    values.update(overrides)
    return EpisodeConfig(**values)


def persist_increment_plan(store, run_id, requirements, tickets) -> None:
    """Persist a fake-planner plan document with REAL trace rows, the way
    planning._persist_plan would (REQ/TKT/covers + the meta document)."""
    with store.transaction():
        for req in requirements:
            store.conn.execute(
                "INSERT INTO trace_req (id, source_msg_id) VALUES (?, ?)",
                (req["id"], req["source_msg"]),
            )
        for ticket in tickets:
            store.conn.execute(
                "INSERT INTO trace_tkt (id, status) VALUES (?, 'pending')",
                (ticket["id"],),
            )
            for req_id in ticket["covers"]:
                store.conn.execute(
                    "INSERT INTO trace_tkt_covers (tkt_id, req_id)"
                    " VALUES (?, ?)",
                    (ticket["id"], req_id),
                )
        for ticket in tickets:
            if ticket["status"] != "pending":
                store.set_ticket_status(ticket["id"], ticket["status"], run_id)
        document = {
            "run_id": run_id,
            "spec_ref": f"run-{run_id}",
            "requirements": list(requirements),
            "tickets": [
                {k: v for k, v in t.items() if k != "status"} for t in tickets
            ],
            "assumptions": [],
            "warnings": [],
        }
        store.set_meta(
            plan_meta_key(run_id),
            json.dumps(document, sort_keys=True, ensure_ascii=False),
        )


def complete_fake_run(store, inc, run_id, play) -> RunResult:
    """Shared body of the fake increment/resume: bug tickets from the UAT
    carry-in FIRST (the planner's 003 R11 contract), then the play's
    tickets; cost lands on the run row (Phase 1 R16's aggregate)."""
    requirements, tickets = [], []
    n = 0
    for msg_id in inc.carry_in_msg_ids:
        rid = f"REQ-r{run_id}-{n:03d}"
        tid = f"TKT-r{run_id}-{n:03d}"
        requirements.append(
            {"id": rid, "text": f"fix the problem in {msg_id}", "source_msg": msg_id}
        )
        tickets.append(
            {
                "id": tid,
                "title": f"bug fix for {msg_id}",
                "kind": "bug",
                "covers": [rid],
                "status": "done",
            }
        )
        n += 1
    for suffix, status, kind in play["tickets"]:
        rid = f"REQ-r{run_id}-{n:03d}"
        tid = f"TKT-r{run_id}-{n:03d}"
        requirements.append(
            {
                "id": rid,
                "text": f"requirement {suffix}",
                "source_msg": inc.request_msg_id,
            }
        )
        tickets.append(
            {
                "id": tid,
                "title": f"ticket {suffix}",
                "kind": kind,
                "covers": [rid],
                "status": status,
            }
        )
        n += 1
    persist_increment_plan(store, run_id, requirements, tickets)
    with store.transaction():
        store.conn.execute(
            "UPDATE runs SET total_cost_usd = ? WHERE id = ?",
            (play.get("cost", 0.0), run_id),
        )
    store.set_run_status(run_id, play["status"])
    return RunResult(
        run_id=run_id,
        status=play["status"],
        ticket_statuses={t["id"]: t["status"] for t in tickets},
        detail=play.get("detail", ""),
    )


def scripted_increment(plays):
    """A fake increment stage: one play per increment.

    Each play: ``{"status": ..., "tickets": [(suffix, status, kind), ...],
    "cost": float, "detail": str}``. Creates a REAL run row carrying the
    episode FKs (the contract episode.py enforces), drops a per-increment
    marker file in the workspace (the persistence property), and for
    plan/abort plays returns the bare terminal.
    """
    fn_contexts = []

    def fn(inc):
        fn_contexts.append(inc)
        store = inc.store
        play = plays[len(fn_contexts) - 1]
        run_id = store.create_run(
            f"episode:{inc.episode_id}",
            store.current_snapshot_id(),
            episode_id=inc.episode_id,
            increment_index=inc.increment_index,
        )
        marker = Path(inc.workspace.root) / f"inc-{inc.increment_index}.txt"
        marker.write_text("delivered\n", encoding="utf-8", newline="\n")
        if play["status"] in ("plan_failed", "aborted_quota", "aborted_error"):
            store.set_run_status(run_id, play["status"])
            return RunResult(
                run_id=run_id,
                status=play["status"],
                ticket_statuses={},
                detail=play.get("detail", ""),
            )
        return complete_fake_run(store, inc, run_id, play)

    fn.contexts = fn_contexts
    return fn


def scripted_request():
    """A fake explorer opening-prompt stage: writes the MSG row (FK-checked
    mentions on the slice) exactly where the real explorer would."""
    calls = []

    def fn(ctx, increment_index, slice_ids):
        calls.append(tuple(slice_ids))
        msg_id = f"MSG-e{ctx.episode_id}-inc{increment_index:03d}-open"
        text = f"please build {', '.join(slice_ids)}"
        write_explorer_msg(ctx.store, msg_id, text, slice_ids)
        return SimpleNamespace(msg_id=msg_id, text=text)

    fn.calls = calls
    return fn


def scripted_uat(verdicts):
    """A fake UAT stage: "accepted", or ("rejected", [(content, mentions)])
    — rejection feedback lands as real explorer MSG rows (003 R11)."""
    calls = []

    def fn(ctx, increment_index, run_id, briefing):
        calls.append(
            {"increment": increment_index, "run_id": run_id, "briefing": briefing}
        )
        verdict = verdicts[len(calls) - 1]
        if verdict == "accepted":
            return SimpleNamespace(verdict="accepted", feedback_msg_ids=())
        _, items = verdict
        msg_ids = []
        for n, (content, mentions) in enumerate(items, start=1):
            msg_id = f"MSG-e{ctx.episode_id}-inc{increment_index:03d}-uat{n:02d}"
            write_explorer_msg(ctx.store, msg_id, content, mentions)
            msg_ids.append(msg_id)
        return SimpleNamespace(
            verdict="rejected", feedback_msg_ids=tuple(msg_ids)
        )

    fn.calls = calls
    return fn


def build_stages(increment_fn, uat_verdicts, *, request_fn=None, resume_fn=None):
    """Assemble fake stages around a shared ordered event log."""
    events = []
    request = request_fn or scripted_request()
    uat = scripted_uat(uat_verdicts)

    def reset_target():
        events.append("target_reset")

    def settle(ctx):
        # The settle stage's contract (run_settlement's behavior): reset the
        # target to seed through the episode's hook, THEN run the rubric.
        ctx.reset_target()
        events.append("rubric")

    stages = EpisodeStages(
        request_fn=request,
        increment_fn=increment_fn,
        uat_fn=uat,
        reset_target_fn=reset_target,
        settle_fn=settle,
        resume_increment_fn=resume_fn,
    )
    return stages, request, uat, events


SUCCESS_PLAY = {"status": "success", "tickets": [("a", "done", "feature")]}


# --- plan test scenarios -----------------------------------------------------------


def test_two_increment_episode_accumulates_msg_req_across_increments(tmp_path):
    store = make_store(tmp_path / "lib")
    mint_feats(store, ["alpha", "bravo", "charlie", "delta"])
    ws = make_ws(tmp_path / "ws")
    inner = scripted_increment(
        [
            {"status": "success", "tickets": [("a", "done", "feature")]},
            {"status": "success", "tickets": [("b", "done", "feature")]},
        ]
    )
    seen_at_inc2 = {}

    def increment(inc):
        if inc.increment_index == 2:
            # What increment 2's planner can see at call time: increment 1's
            # transcript (its MSG rows and extracted REQs) is in the store.
            row = inc.store.conn.execute(
                "SELECT content FROM trace_msg WHERE id = ?",
                (f"MSG-e{inc.episode_id}-inc001-open",),
            ).fetchone()
            seen_at_inc2["inc1_opening"] = row["content"] if row else None
            seen_at_inc2["req_count"] = inc.store.conn.execute(
                "SELECT COUNT(*) AS n FROM trace_req"
            ).fetchone()["n"]
        return inner(inc)

    stages, request, uat, _ = build_stages(
        increment, ["accepted", "accepted"]
    )
    result = run_episode(
        store, make_cfg(ws, max_increments=2, slice_size=2), stages
    )

    assert result.status == "budget_spent"
    assert result.increments_completed == 2
    assert len(result.run_ids) == 2
    # increment 2's planner saw increment 1's transcript
    assert seen_at_inc2["inc1_opening"] is not None
    assert seen_at_inc2["req_count"] >= 1
    # and the accumulation persists: both increments' MSG and REQ rows coexist
    msg_count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM trace_msg WHERE id LIKE 'MSG-e%-open'"
    ).fetchone()["n"]
    assert msg_count == 2
    for run_id in result.run_ids:
        assert plan_report(store, run_id)["requirements"]


def test_uat_rejection_injects_bug_tickets_prepended_and_budget_counted(tmp_path):
    store = make_store(tmp_path / "lib")
    feats = mint_feats(store, ["alpha", "bravo", "charlie", "delta"])
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment(
        [
            {"status": "success", "tickets": [("a", "done", "feature")]},
            {"status": "success", "tickets": [("b", "done", "feature")]},
        ]
    )
    stages, request, uat, _ = build_stages(
        increment,
        [("rejected", [("the list page loses my tags", [feats[0]])]), "accepted"],
    )
    result = run_episode(
        store, make_cfg(ws, max_increments=2, slice_size=2), stages
    )
    assert result.status == "budget_spent"

    episode_id = result.episode_id
    feedback_id = f"MSG-e{episode_id}-inc001-uat01"
    # the rejection feedback was carried into increment 2
    assert increment.contexts[1].carry_in_msg_ids == (feedback_id,)
    # and its plan prepends a bug ticket covering a REQ sourced from it
    document = plan_report(store, result.run_ids[1])
    first = document["tickets"][0]
    assert first["kind"] == "bug"
    req = {r["id"]: r for r in document["requirements"]}[first["covers"][0]]
    assert req["source_msg"] == feedback_id
    # the bug ticket is an ordinary plan ticket: inside the increment's own
    # ticket list, ahead of the new frontier work (counted in its budget)
    kinds = [t["kind"] for t in document["tickets"]]
    assert kinds == ["bug", "feature"]
    # acceptance landed on both run rows (the R2 acceptance stage)
    acceptances = [
        store.get_run(run_id)["acceptance"] for run_id in result.run_ids
    ]
    assert acceptances == ["rejected", "accepted"]


def test_escalated_ticket_carries_into_next_plan_and_skips_uat(tmp_path):
    store = make_store(tmp_path / "lib")
    mint_feats(store, ["alpha", "bravo", "charlie", "delta"])
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment(
        [
            {
                "status": "partial",
                "tickets": [("a", "done", "feature"), ("b", "escalated", "feature")],
            },
            {"status": "success", "tickets": [("c", "done", "feature")]},
        ]
    )
    stages, request, uat, _ = build_stages(
        increment, ["accepted", "accepted"]
    )
    result = run_episode(
        store, make_cfg(ws, max_increments=2, slice_size=2), stages
    )

    document = plan_report(store, result.run_ids[0])
    done_id = document["tickets"][0]["id"]
    escalated_id = document["tickets"][1]["id"]
    # the UAT briefing carries the delivered subset only: the escalated
    # ticket never appears (it is not re-discovered via UAT, R2)
    briefing = uat.calls[0]["briefing"]
    assert done_id in briefing
    assert escalated_id not in briefing
    assert f"MSG-e{result.episode_id}-inc001-open" in briefing
    # and it carries into the next increment's plan automatically
    assert increment.contexts[1].carry_in_ticket_ids == (escalated_id,)


def test_budget_cap_at_increment_boundary_settles_budget_spent(tmp_path):
    store = make_store(tmp_path / "lib")
    mint_feats(store, ["alpha", "bravo", "charlie", "delta"])
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment([dict(SUCCESS_PLAY)])
    stages, request, uat, events = build_stages(increment, ["accepted"])
    result = run_episode(
        store, make_cfg(ws, max_increments=1, slice_size=2), stages
    )

    assert result.status == "budget_spent"
    assert store.get_episode(result.episode_id)["status"] == "budget_spent"
    assert result.increments_completed == 1
    # the cap tripped, not the frontier: selectable rows remain
    assert select_slice(store, TARGET, 2)
    # settlement DID run (terminal settles via the rubric, HTD diagram)
    assert "rubric" in events


def test_cost_ceiling_trips_from_aggregated_run_costs(tmp_path):
    store = make_store(tmp_path / "lib")
    mint_feats(store, ["alpha", "bravo", "charlie", "delta", "echo", "fox"])
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment(
        [
            {"status": "success", "tickets": [("a", "done", "feature")], "cost": 0.6},
            {"status": "success", "tickets": [("b", "done", "feature")], "cost": 0.6},
        ]
    )
    stages, request, uat, _ = build_stages(increment, ["accepted", "accepted"])
    result = run_episode(
        store,
        make_cfg(ws, max_increments=10, cost_ceiling_usd=1.0, slice_size=2),
        stages,
    )

    # 0.6 after increment 1 (under), 1.2 after increment 2 (ceiling tripped)
    assert result.status == "budget_spent"
    assert result.increments_completed == 2
    assert episode_cost(store, result.episode_id) == pytest.approx(1.2)
    assert select_slice(store, TARGET, 2)  # frontier was not the terminal


def test_quota_mid_increment_suspends_and_resumes_to_completion(tmp_path):
    store = make_store(tmp_path / "lib")
    mint_feats(store, ["alpha", "bravo"])
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment(
        [{"status": "aborted_quota", "detail": "5h window exhausted"}]
    )
    resumed = {}

    def resume_increment(inc, run_id):
        # Phase 1's checkpoint re-enters the SAME run and completes it.
        resumed["run_id"] = run_id
        return complete_fake_run(
            inc.store,
            inc,
            run_id,
            {"status": "success", "tickets": [("a", "done", "feature")]},
        )

    stages, request, uat, _ = build_stages(
        increment, ["accepted"], resume_fn=resume_increment
    )
    cfg = make_cfg(ws, max_increments=1, slice_size=2)
    result = run_episode(store, cfg, stages)

    # R3: quota suspends — resumable, and NEVER budget_spent
    assert result.status == "suspended"
    episode_id = result.episode_id
    assert store.get_episode(episode_id)["status"] == "suspended"
    run_row = store.conn.execute(
        "SELECT * FROM runs WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    assert run_row["status"] == "aborted_quota"
    assert run_row["increment_index"] == 1

    final = resume_episode(store, episode_id, stages)
    assert resumed["run_id"] == run_row["id"]
    assert final.status == "budget_spent"  # max_increments=1 boundary
    assert final.increments_completed == 1
    assert final.run_ids == (run_row["id"],)
    assert store.get_run(run_row["id"])["acceptance"] == "accepted"
    # the resumed run kept its episode FKs
    assert store.get_run(run_row["id"])["episode_id"] == episode_id


def test_quota_in_explorer_session_suspends_and_resumes(tmp_path):
    store = make_store(tmp_path / "lib")
    mint_feats(store, ["alpha", "bravo"])
    ws = make_ws(tmp_path / "ws")
    inner_request = scripted_request()
    attempts = {"n": 0}

    def flaky_request(ctx, increment_index, slice_ids):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise SessionQuotaExhausted("quota during the opening session")
        return inner_request(ctx, increment_index, slice_ids)

    increment = scripted_increment([dict(SUCCESS_PLAY)])
    stages, _, uat, _ = build_stages(
        increment, ["accepted"], request_fn=flaky_request
    )
    cfg = make_cfg(ws, max_increments=1, slice_size=2)
    result = run_episode(store, cfg, stages)
    assert result.status == "suspended"
    assert store.get_episode(result.episode_id)["status"] == "suspended"
    assert not increment.contexts  # no run was started

    final = resume_episode(store, result.episode_id, stages)
    assert final.status == "budget_spent"
    assert final.increments_completed == 1


def test_plan_failed_halts_with_human_escalation_record(tmp_path):
    store = make_store(tmp_path / "lib")
    mint_feats(store, ["alpha", "bravo"])
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment(
        [{"status": "plan_failed", "detail": "planner cap of 3 exhausted"}]
    )
    stages, request, uat, events = build_stages(increment, [])
    result = run_episode(store, make_cfg(ws, slice_size=2), stages)

    assert result.status == "aborted_error"
    assert store.get_episode(result.episode_id)["status"] == "aborted_error"
    assert "plan_failed" in result.detail
    # the human-escalation record (R2): an open review_queue row naming the
    # halted run and increment
    row = store.conn.execute(
        "SELECT * FROM review_queue WHERE episode_id = ? AND kind = ?",
        (result.episode_id, "plan_failed"),
    ).fetchone()
    assert row is not None
    assert row["status"] == "open"
    payload = json.loads(row["payload_json"])
    assert payload["increment_index"] == 1
    assert payload["run_id"] == increment.contexts[0].store.conn.execute(
        "SELECT id FROM runs WHERE episode_id = ?", (result.episode_id,)
    ).fetchone()["id"]
    # the halt escalates to the human: no UAT, no settlement rubric
    assert not uat.calls
    assert "rubric" not in events


def test_settlement_handoff_resets_target_before_rubric(tmp_path):
    store = make_store(tmp_path / "lib")
    feats = mint_feats(store, ["alpha", "bravo"])
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment([dict(SUCCESS_PLAY)])
    stages, request, uat, events = build_stages(increment, ["accepted"])
    result = run_episode(store, make_cfg(ws, slice_size=2), stages)

    # the whole 2-FEAT frontier was explored in increment 1
    assert result.status == "frontier_exhausted"
    assert store.get_episode(result.episode_id)["status"] == "frontier_exhausted"
    # R6 both halves, in order: episode-start reset first, then the
    # settlement handoff resets again immediately before the rubric
    assert events[0] == "target_reset"
    assert events[-2:] == ["target_reset", "rubric"]
    assert events.count("target_reset") == 2
    # the R10 mention-coverage audit ran at settlement: both FEATs were
    # mentioned (the opening prompt), so neither is force-scheduled
    for fid in feats:
        assert frontier_row(store, fid)["force_scheduled"] == 0


def test_frontier_exhausted_settles_terminal(tmp_path):
    """An empty opening slice settles immediately — explore nothing (R3/R10)."""
    store = make_store(tmp_path / "lib")  # no FEATs minted: frontier empty
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment([])
    stages, request, uat, events = build_stages(increment, [])
    result = run_episode(store, make_cfg(ws, slice_size=2), stages)
    assert result.status == "frontier_exhausted"
    assert result.increments_completed == 0
    assert not increment.contexts and not uat.calls
    assert "rubric" in events


def test_digest_mismatch_at_episode_setup_is_hard_error(tmp_path):
    store = make_store(tmp_path / "lib")
    mint_feats(store, ["alpha"])
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment([])
    stages, *_ = build_stages(increment, [])
    cfg = make_cfg(ws, digest="sha256:somethingelse")
    with pytest.raises(EpisodeError, match="digest mismatch"):
        run_episode(store, cfg, stages)
    # hard setup error: no episode row was minted
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM episodes"
    ).fetchone()["n"] == 0


# --- verification: the episode lifecycle property test ------------------------------


def _library_fingerprint(store: Store) -> tuple:
    return (
        store.conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"],
        store.conn.execute("SELECT COUNT(*) AS n FROM insights").fetchone()["n"],
        store.conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"],
        store.current_snapshot_id(),
    )


@pytest.mark.parametrize(
    "script",
    [
        # accept / accept
        {
            "plays": [
                {"status": "success", "tickets": [("a", "done", "feature")]},
                {"status": "success", "tickets": [("b", "done", "feature")]},
            ],
            "verdicts": ["accepted", "accepted"],
        },
        # reject then accept (bug carry-in)
        {
            "plays": [
                {"status": "success", "tickets": [("a", "done", "feature")]},
                {"status": "success", "tickets": [("b", "done", "feature")]},
            ],
            "verdicts": [("rejected", [("broken", None)]), "accepted"],
        },
        # partial with escalation, then accept
        {
            "plays": [
                {
                    "status": "partial",
                    "tickets": [
                        ("a", "done", "feature"),
                        ("b", "escalated", "feature"),
                    ],
                },
                {"status": "success", "tickets": [("c", "done", "feature")]},
            ],
            "verdicts": ["accepted", "accepted"],
        },
    ],
    ids=["accept-accept", "reject-accept", "escalate-accept"],
)
def test_episode_lifecycle_property_invariants(tmp_path, script):
    """Any fake sequence preserves: library frozen (no writes), IDs globally
    unique, workspace persists across increments, every run carries episode
    FKs (the U6 verification clause)."""
    store = make_store(tmp_path / "lib")
    feats = mint_feats(store, ["alpha", "bravo", "charlie", "delta"])
    ws = make_ws(tmp_path / "ws")
    # late-bind feedback mentions to a real FEAT id
    verdicts = [
        v if v == "accepted" else ("rejected", [(v[1][0][0], [feats[0]])])
        for v in script["verdicts"]
    ]
    increment = scripted_increment(script["plays"])
    stages, request, uat, _ = build_stages(increment, verdicts)
    before = _library_fingerprint(store)

    result = run_episode(
        store, make_cfg(ws, max_increments=2, slice_size=2), stages
    )
    assert result.status == "budget_spent"

    # library frozen: episodes never write the skill library or mint snapshots
    assert _library_fingerprint(store) == before
    # every run carries episode FKs, with increment indexes 1..n
    runs = store.conn.execute(
        "SELECT id, episode_id, increment_index FROM runs ORDER BY id"
    ).fetchall()
    assert [r["episode_id"] for r in runs] == [result.episode_id] * 2
    assert [r["increment_index"] for r in runs] == [1, 2]
    # IDs globally unique across increments (episode-scoped only for display)
    all_tkts = [
        t["id"]
        for run_id in result.run_ids
        for t in plan_report(store, run_id)["tickets"]
    ]
    assert len(all_tkts) == len(set(all_tkts))
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM trace_tkt"
    ).fetchone()["n"] == len(all_tkts)
    # workspace persists across increments: one root throughout, and
    # increment 1's work is still present when increment 2 runs and after
    roots = {str(c.workspace.root) for c in increment.contexts}
    assert roots == {str(ws)}
    assert (ws / "inc-1.txt").exists() and (ws / "inc-2.txt").exists()
    assert (ws / ".git").exists()  # retained after settlement


# --- the Orchestrator amendment (003 R1: dual-mode + episode FKs) ----------------------


def build_orchestrator(store: Store, ticket_id: str = "TKT-EFK-A") -> Orchestrator:
    return Orchestrator(
        store,
        planner_fn=lambda ctx: [TicketSpec(ticket_id)],
        worker_fn=lambda ctx: None,
        gate_fn=lambda ctx: GateResult(passed=True),
        verifier_fn=lambda ctx: VerifierResult(passed=True),
        ralph_cap=3,
    )


def test_orchestrator_run_carries_episode_fks(tmp_path):
    store = make_store(tmp_path / "lib")
    episode_id = store.create_episode(TARGET, DIGEST, 0)
    ws_root = make_ws(tmp_path / "ws")
    from agent_families.pipeline.workspace import Workspace

    result = build_orchestrator(store).run(
        "spec-episode",
        Workspace(root=ws_root),
        episode_id=episode_id,
        increment_index=1,
    )
    assert result.status == "success"
    run = store.get_run(result.run_id)
    assert run["episode_id"] == episode_id
    assert run["increment_index"] == 1


def test_orchestrator_run_rejects_half_episode_fks(tmp_path):
    store = make_store(tmp_path / "lib")
    ws_root = make_ws(tmp_path / "ws")
    from agent_families.pipeline.workspace import Workspace

    with pytest.raises(OrchestratorError, match="travel together"):
        build_orchestrator(store).run(
            "spec", Workspace(root=ws_root), episode_id=7
        )


def test_orchestrator_run_standalone_instantiates_per_run(tmp_path):
    store = make_store(tmp_path / "lib")
    template = make_template(tmp_path / "template")
    dest = tmp_path / "run-ws"
    result = build_orchestrator(store, "TKT-STA-A").run(
        "spec-standalone",
        workspace_dest=dest,
        template_dir=template,
    )
    assert result.status == "success"
    assert (dest / ".git").exists()
    run = store.get_run(result.run_id)
    assert run["episode_id"] is None and run["increment_index"] is None


def test_orchestrator_run_requires_workspace_or_dest(tmp_path):
    store = make_store(tmp_path / "lib")
    with pytest.raises(OrchestratorError, match="no workspace"):
        build_orchestrator(store).run("spec")


# --- the workspace amendment (003 R1: engagement create-or-load) -----------------------


def test_ensure_engagement_workspace_creates_then_loads(tmp_path):
    template = make_template(tmp_path / "template")
    dest = tmp_path / "engagement"
    first = ensure_engagement_workspace(dest, template)
    assert (dest / ".git").exists()
    # the engagement accumulates product state across episodes...
    (dest / "product-state.txt").write_text("v1\n", encoding="utf-8")
    second = ensure_engagement_workspace(dest, template)
    # ...and a later episode LOADS, never recreates (R1)
    assert second.root == first.root
    assert (dest / "product-state.txt").read_text(encoding="utf-8") == "v1\n"


def test_load_workspace_rejects_missing_and_non_repo(tmp_path):
    with pytest.raises(WorkspaceError, match="not found"):
        load_workspace(tmp_path / "missing")
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(WorkspaceError, match="no .git"):
        load_workspace(bare)


# --- resume guardrails ----------------------------------------------------------------


def test_resume_rejects_settled_or_unknown_episode(tmp_path):
    store = make_store(tmp_path / "lib")
    mint_feats(store, ["alpha", "bravo"])
    ws = make_ws(tmp_path / "ws")
    increment = scripted_increment([dict(SUCCESS_PLAY)])
    stages, *_ = build_stages(increment, ["accepted"])
    result = run_episode(store, make_cfg(ws, slice_size=2), stages)
    assert result.status == "frontier_exhausted"

    with pytest.raises(EpisodeError, match="already settled"):
        resume_episode(store, result.episode_id, stages)
    with pytest.raises(EpisodeError, match="does not exist"):
        resume_episode(store, 999, stages)
    # checkpoint survives for the trace CLI even after settlement
    assert store.get_meta(episode_checkpoint_key(result.episode_id)) is not None


def test_render_uat_briefing_empty_delivery(tmp_path):
    """An increment where nothing reached done briefs honestly: there is
    nothing to UAT-judge (escalated work carries forward, R2)."""
    store = make_store(tmp_path / "lib")
    run_id = store.create_run("spec", 0)
    persist_increment_plan(
        store,
        run_id,
        [{"id": f"REQ-r{run_id}-000", "text": "x", "source_msg": _msg(store)}],
        [
            {
                "id": f"TKT-r{run_id}-000",
                "title": "stuck",
                "kind": "feature",
                "covers": [f"REQ-r{run_id}-000"],
                "status": "escalated",
            }
        ],
    )
    briefing = render_uat_briefing(store, run_id)
    assert "nothing delivered" in briefing
    assert "TKT-" not in briefing


def _msg(store: Store) -> str:
    """One plain MSG row (mentions-free, the Phase 1 planner shape)."""
    store.conn.execute(
        "INSERT INTO trace_msg (id, content) VALUES ('MSG-plain-001', 'spec')"
    )
    return "MSG-plain-001"
