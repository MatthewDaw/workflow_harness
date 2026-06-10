"""plan-003 U5: the explorer subsystem — prompts, verified-oracle Q&A, UAT,
containment (R11–R15).

Fully offline (fixture/fake-driven per the unit): explorer sessions ride the
Phase 1 scripted-agent fake, the mediated browse channel is a recorded fake
(the R15 structural-containment pattern makes that trivial), and checker
calls replay judge fixtures planted by the tests. Zero quota, no ``claude``
on PATH, no docker.

## Conformance

Unit test scenarios (plan-003 U5) — scenario -> enforcing test:

- prompt MSG rows carry only confirmed FEAT mentions (unconfirmed -> FK
  rejection path exercised)
  -> ``test_opening_prompt_msg_carries_confirmed_mentions`` (the happy path:
  MSG + mention rows land, FK-checked) and
  ``test_opening_prompt_unminted_mention_fk_rejected`` (the trace_msg_mentions
  FK fires on an unminted FEAT and the whole write rolls back) plus
  ``test_msg_write_rejects_deprecated_mention`` (minted-but-not-confirmed is
  rejected belt-and-braces)
- checker pass and contradiction paths
  -> ``test_ask_question_answered_counts_budget`` (pass arm) and
  ``test_checker_rejection_retries_fresh_context_with_contradiction``
  (contradiction arm: fresh-context re-answer embeds the contradiction;
  grader-side retries consume NO question budget); the checker contract
  itself is pinned 1:1 in test_oracle_check.py
- final-failure -> ``answer_unavailable`` + slot refund + review-queue row
  -> ``test_final_failure_refunds_slot_and_queues_review``; the ungrounded
  arm (an answer with no fresh observation never reaches the checker and
  rides the same retry/failure path, R12)
  -> ``test_ungrounded_answer_fails_without_checker``
- budget exhaustion bounce typed and logged
  -> ``test_budget_exhaustion_bounce_typed_and_logged`` (typed qa_log row
  with outcome='budget_exhausted', budget_counted=0, no session consumed,
  the elicitation-efficiency log line emitted)
- assumptions[] converted and budget-counted; over-budget assumption
  recorded ``unverified``
  -> ``test_assumptions_converted_budget_counted_and_overbudget_unverified``
  (the planning.check_plan_assumptions hook closing Phase 1's seam, R13)
- UAT rejection produces MSG feedback rows that the (fake) planner turns
  into ``kind: bug`` tickets with full REQ links
  -> ``test_uat_rejection_feeds_bug_tickets_with_full_req_links`` (UAT MSG
  rows with mentions -> run_planning carry-in -> bug REQ sourced to the UAT
  MSG -> TKT kind 'bug' covering it with a linked AC; also asserts UAT is
  budget-free per R13)
- containment assert fires on a planted Bash tool-use in the transcript
  -> ``test_containment_assert_fires_on_planted_bash_tool_use`` (direct) and
  ``test_containment_fires_through_the_explorer_loop`` (wired into
  run_explorer_task)

Verification clause ("every R12/R13 arm has a 1:1 test") — the R12 arms map
to the Q&A tests above plus ``test_ask_question_requires_mentions``
(mandatory mentions) and test_oracle_check.py (checker contract); the R13
arms map to the budget/assumption tests plus the UAT-budget-free assertion.
R14 (fresh-context per batch) is pinned by
``test_answer_prompt_is_fresh_context_with_history``; R15 (structural
containment + mediated browse) by ``test_explorer_profile_is_toolless``,
``test_browse_loop_mediates_observations``, and the containment tests.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent_families.grading.frontier import select_slice
from agent_families.grading.registry import (
    FeatureCandidate,
    deprecate_feat,
    mint_feat,
)
from agent_families.judge import write_fixture
from agent_families.pipeline.explorer import (
    ANSWER_RESULT_SCHEMA,
    CheckerConfig,
    ExplorerConfig,
    ExplorerContainmentViolation,
    ExplorerError,
    ask_question,
    assert_toolless_transcript,
    author_opening_prompt,
    build_answer_prompt,
    build_exploration_brief,
    explorer_profile,
    qa_history_text,
    questions_spent,
    run_explorer_task,
    run_uat,
    write_explorer_msg,
)
from agent_families.pipeline.oracle_check import (
    CHECKER_SCHEMA,
    build_checker_prompt,
    retrieve_registry_evidence,
)
from agent_families.pipeline.planning import (
    PlanningError,
    build_assumption_question,
    build_planner_prompt,
    check_plan_assumptions,
    plan_report,
    run_planning,
)
from agent_families.pipeline.sessions import planner_profile
from agent_families.store import Store

TARGET = "linkding"
DIGEST = "sha256:" + "ef" * 32
MODEL = "sonnet"
A11Y = {"role": "main", "name": "bookmarks", "children": ["tag-sidebar"]}
QUESTION = "Does the bookmark list support filtering by tag?"
CONTRA = {
    "claim": "filtering happens in a sidebar",
    "evidence_ref": "FEAT-bookmark-tag-filter",
    "observed": "the registry evidence shows filtering via the search box",
}
SPEC = "# Toy bookmarks app\n\nUsers can view a dashboard.\n"


# --- helpers -------------------------------------------------------------------


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "library.db")
    store.migrate()
    return store


def cand(key: str) -> FeatureCandidate:
    return FeatureCandidate(
        key=key,
        area="bookmarks",
        behavior=f"user can {key.replace('-', ' ')}",
        route="bookmarks/urls.py",
        confirm_steps=(
            {"action": "goto", "selector": "", "args": {"url": "/bookmarks"}},
        ),
        scenario_steps=("Open the bookmarks page",),
        expected_outcome="the behavior is observable",
        tier="must",
    )


def mint(store: Store, tmp_path: Path, key: str) -> str:
    evidence = tmp_path / "evidence" / f"{key}.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {
                "feat_key": key,
                "digest": DIGEST,
                "captured": [
                    {
                        "step": {"action": "goto", "selector": "", "args": {}},
                        "a11y": {"role": "list", "name": key},
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return mint_feat(store, cand(key), str(evidence), target=TARGET, digest=DIGEST)


class FakeBrowse:
    """The orchestrator-mediated browse channel, scripted (R15)."""

    def __init__(self, a11y=None) -> None:
        self.calls: list[dict] = []
        self.a11y = A11Y if a11y is None else a11y

    def __call__(self, request: dict) -> dict:
        self.calls.append(request)
        return {"status": "ok", "a11y": self.a11y}


def envelope(output: dict) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 25,
        "num_turns": 1,
        "result": "ok",
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 50, "output_tokens": 20},
        "structured_output": output,
    }


def browse_request(url: str = "/bookmarks") -> dict:
    return {"action": "goto", "selector": "", "args": {"url": url}}


def browse_step(request: dict | None = None) -> dict:
    return {
        "role": "explorer",
        "envelope": envelope(
            {
                "action": "browse",
                "request": request or browse_request(),
                "result": None,
            }
        ),
    }


def finish_step(result: dict, **extra) -> dict:
    step = {
        "role": "explorer",
        "envelope": envelope(
            {"action": "finish", "request": None, "result": result}
        ),
    }
    step.update(extra)
    return step


def make_cfg(
    tmp_path: Path,
    browse: FakeBrowse,
    steps: list[dict],
    *,
    name: str = "explorer-script.json",
    max_steps: int = 4,
) -> ExplorerConfig:
    script = tmp_path / name
    script.write_text(
        json.dumps({"steps": steps}, indent=2), encoding="utf-8", newline="\n"
    )
    return ExplorerConfig(
        profile=explorer_profile(model=MODEL, max_turns=1, timeout_s=60.0),
        browse=browse,
        transcript_dir=tmp_path / "transcripts",
        max_steps=max_steps,
        max_retries=0,
        mode="scripted",
        script_path=script,
    )


def make_checker(tmp_path: Path, *, answer_retries: int = 0) -> CheckerConfig:
    return CheckerConfig(
        model=MODEL,
        judge_retries=0,
        answer_retries=answer_retries,
        mode="replay",
        fixtures_dir=tmp_path / "fixtures",
    )


def plant_verdict(
    store: Store, tmp_path: Path, question: str, answer: str, fid: str, output: dict
) -> None:
    """Plant the replay fixture for the exact checker call ask_question makes."""
    evidence = retrieve_registry_evidence(store, (fid,))
    prompt = build_checker_prompt(question, answer, A11Y, evidence)
    write_fixture(
        tmp_path / "fixtures", prompt, CHECKER_SCHEMA, MODEL, envelope(output)
    )


def qa_rows(store: Store):
    return store.conn.execute("SELECT * FROM qa_log ORDER BY id").fetchall()


def mention_feats(store: Store, msg_id: str) -> list[str]:
    rows = store.conn.execute(
        "SELECT feat_id FROM trace_msg_mentions WHERE msg_id = ?"
        " ORDER BY feat_id",
        (msg_id,),
    ).fetchall()
    return [r["feat_id"] for r in rows]


# --- R15: tool-less profile and the mediated browse loop ---------------------------


def test_explorer_profile_is_toolless():
    profile = explorer_profile(model=MODEL, max_turns=1, timeout_s=60.0)
    assert profile.tools == ""  # --tools "" : no tools at all (R15)
    assert profile.allowed_tools is None
    assert profile.role == "explorer"


def test_browse_loop_mediates_observations(tmp_path):
    store = make_store(tmp_path)
    browse = FakeBrowse()
    request = browse_request("/bookmarks?tag=dev")
    cfg = make_cfg(
        tmp_path,
        browse,
        [browse_step(request), finish_step({"answer": "two bookmarks shown"})],
    )
    task = run_explorer_task(
        "Look around.",
        ANSWER_RESULT_SCHEMA,
        cfg,
        transcript_path=tmp_path / "transcripts" / "task.jsonl",
        store=store,
    )
    # the ORCHESTRATOR executed the request; the session never had a tool
    assert browse.calls == [request]
    assert task.result == {"answer": "two bookmarks shown"}
    assert task.steps == 2
    assert task.fresh_a11y() == A11Y
    assert task.observations[0]["request"] == request


def test_explorer_task_max_steps_is_a_hard_error(tmp_path):
    store = make_store(tmp_path)
    cfg = make_cfg(
        tmp_path, FakeBrowse(), [browse_step(), browse_step()], max_steps=2
    )
    with pytest.raises(ExplorerError, match="did not finish within 2"):
        run_explorer_task(
            "Look around.",
            ANSWER_RESULT_SCHEMA,
            cfg,
            transcript_path=tmp_path / "transcripts" / "task.jsonl",
            store=store,
        )


# --- R15 defense-in-depth: the transcript-scan containment assert ------------------


def test_containment_assert_fires_on_planted_bash_tool_use(tmp_path):
    transcript = tmp_path / "planted.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                "non-json noise line",
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {"command": "curl evil"},
                                }
                            ]
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ExplorerContainmentViolation, match="Bash"):
        assert_toolless_transcript(transcript)


def test_containment_passes_on_clean_transcript(tmp_path):
    transcript = tmp_path / "clean.jsonl"
    transcript.write_text(
        json.dumps({"type": "result", "is_error": False}) + "\nnoise\n",
        encoding="utf-8",
        newline="\n",
    )
    assert_toolless_transcript(transcript)  # no raise


def test_containment_fires_through_the_explorer_loop(tmp_path):
    store = make_store(tmp_path)
    planted = finish_step(
        {"answer": "done"},
        transcript_events=[
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "rm -rf /"},
                        }
                    ]
                },
            }
        ],
    )
    cfg = make_cfg(tmp_path, FakeBrowse(), [planted])
    with pytest.raises(ExplorerContainmentViolation, match="tool-less"):
        run_explorer_task(
            "Look around.",
            ANSWER_RESULT_SCHEMA,
            cfg,
            transcript_path=tmp_path / "transcripts" / "task.jsonl",
            store=store,
        )


# --- R11: opening prompt as MSG rows with FK-checked mentions ----------------------


def test_opening_prompt_msg_carries_confirmed_mentions(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    fid_a = mint(store, tmp_path, "bookmark-create")
    fid_b = mint(store, tmp_path, "bookmark-tag-filter")
    browse = FakeBrowse()
    cfg = make_cfg(
        tmp_path,
        browse,
        [
            browse_step(),
            finish_step(
                {
                    "prompt": "Please build tag filtering for the list page.",
                    "mentions": [fid_b, fid_a, fid_b],  # dupes deduplicate
                }
            ),
        ],
    )
    opening = author_opening_prompt(
        store,
        episode,
        1,
        cfg,
        target=TARGET,
        slice_feat_ids=[fid_b, fid_a],
    )
    assert opening.msg_id == f"MSG-e{episode}-inc001-open"
    assert opening.mentions == (fid_b, fid_a)
    row = store.conn.execute(
        "SELECT content FROM trace_msg WHERE id = ?", (opening.msg_id,)
    ).fetchone()
    assert row["content"] == "Please build tag filtering for the list page."
    assert mention_feats(store, opening.msg_id) == sorted([fid_a, fid_b])
    assert browse.calls == [browse_request()]  # the exploration happened


def test_opening_prompt_unminted_mention_fk_rejected(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    fid = mint(store, tmp_path, "bookmark-create")
    cfg = make_cfg(
        tmp_path,
        FakeBrowse(),
        [
            finish_step(
                {"prompt": "Build the thing.", "mentions": ["FEAT-never-minted"]}
            )
        ],
    )
    before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM trace_msg"
    ).fetchone()["n"]
    with pytest.raises(ExplorerError, match="not mentionable"):
        author_opening_prompt(
            store, episode, 1, cfg, target=TARGET, slice_feat_ids=[fid]
        )
    # the FK fired and the whole write rolled back: no MSG, no mentions
    after = store.conn.execute("SELECT COUNT(*) AS n FROM trace_msg").fetchone()["n"]
    assert after == before
    assert (
        store.conn.execute(
            "SELECT COUNT(*) AS n FROM trace_msg_mentions"
        ).fetchone()["n"]
        == 0
    )


def test_msg_write_rejects_deprecated_mention(tmp_path):
    store = make_store(tmp_path)
    fid = mint(store, tmp_path, "bookmark-archive")
    deprecate_feat(store, fid)
    with pytest.raises(ExplorerError, match="non-confirmed"):
        write_explorer_msg(store, "MSG-e1-q0001", "is archiving supported?", [fid])
    assert (
        store.conn.execute("SELECT COUNT(*) AS n FROM trace_msg").fetchone()["n"]
        == 0
    )


def test_msg_write_requires_mentions(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ExplorerError, match="no FEAT mentions"):
        write_explorer_msg(store, "MSG-e1-q0001", "untagged utterance", [])


def test_exploration_brief_orders_force_scheduled_first(tmp_path):
    store = make_store(tmp_path)
    fid_a = mint(store, tmp_path, "bookmark-create")
    fid_b = mint(store, tmp_path, "bookmark-edit")
    fid_c = mint(store, tmp_path, "bookmark-tag-filter")
    store.conn.execute(
        "UPDATE frontier SET force_scheduled = 1 WHERE feat_id = ?", (fid_c,)
    )
    ordered = select_slice(store, TARGET, 3)
    assert ordered[0] == fid_c  # force-scheduled jumps the queue (R10)
    brief = build_exploration_brief(
        target=TARGET, slice_feat_ids=ordered, built_history="Tag page built."
    )
    assert brief.index(f"1. {fid_c}") < brief.index(f"2. {fid_a}")
    assert "force-scheduled first" in brief
    assert "Tag page built." in brief  # the accepted-delivery history (R11)
    assert "BEYOND the already-built set" in brief
    with pytest.raises(ExplorerError, match="non-empty frontier slice"):
        build_exploration_brief(target=TARGET, slice_feat_ids=[])


# --- R12/R13/R14: the verified-oracle question round-trip --------------------------


def test_ask_question_requires_mentions(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    cfg = make_cfg(tmp_path, FakeBrowse(), [])
    with pytest.raises(ExplorerError, match="mandatory FEAT mentions"):
        ask_question(
            store, episode, QUESTION, [], cfg, make_checker(tmp_path), cap=5
        )


def test_ask_question_answered_counts_budget(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    fid = mint(store, tmp_path, "bookmark-tag-filter")
    answer = "Yes - the sidebar lists tags and clicking one filters the list."
    cfg = make_cfg(
        tmp_path, FakeBrowse(), [browse_step(), finish_step({"answer": answer})]
    )
    plant_verdict(
        store, tmp_path, QUESTION, answer, fid,
        {"verdict": "pass", "contradiction": None},
    )
    outcome = ask_question(
        store, episode, QUESTION, [fid], cfg, make_checker(tmp_path), cap=5
    )
    assert outcome.outcome == "answered"
    assert outcome.answer == answer
    assert outcome.retries == 0
    assert outcome.question_msg_id == f"MSG-e{episode}-q0001"
    assert outcome.answer_msg_id == f"MSG-e{episode}-q0001-a"
    # both utterances landed as MSG rows tagged with the mentions (R11/R12)
    assert mention_feats(store, outcome.question_msg_id) == [fid]
    assert mention_feats(store, outcome.answer_msg_id) == [fid]
    (row,) = qa_rows(store)
    assert row["outcome"] == "answered"
    assert row["checker_verdict"] == "pass"
    assert row["budget_counted"] == 1
    assert row["retries"] == 0
    assert questions_spent(store, episode) == 1
    # the answered pair is now the persistent Q&A log a fresh instance reads
    assert QUESTION in qa_history_text(store, episode)


def test_checker_rejection_retries_fresh_context_with_contradiction(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    fid = mint(store, tmp_path, "bookmark-tag-filter")
    wrong = "Filtering happens in a sidebar."
    right = "Filtering happens through the search box tag syntax."
    cfg = make_cfg(
        tmp_path,
        FakeBrowse(),
        [
            browse_step(),
            finish_step({"answer": wrong}),
            browse_step(),
            finish_step({"answer": right}),
        ],
    )
    plant_verdict(
        store, tmp_path, QUESTION, wrong, fid,
        {"verdict": "fail", "contradiction": CONTRA},
    )
    plant_verdict(
        store, tmp_path, QUESTION, right, fid,
        {"verdict": "pass", "contradiction": None},
    )
    outcome = ask_question(
        store, episode, QUESTION, [fid], cfg,
        make_checker(tmp_path, answer_retries=1), cap=5,
    )
    assert outcome.outcome == "answered"
    assert outcome.answer == right
    assert outcome.retries == 1
    (row,) = qa_rows(store)
    assert row["retries"] == 1
    assert row["budget_counted"] == 1
    # grader-side retries consume NO question budget: still ONE counted slot
    assert questions_spent(store, episode) == 1


def test_final_failure_refunds_slot_and_queues_review(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    fid = mint(store, tmp_path, "bookmark-tag-filter")
    bad_one, bad_two = "It filters by color.", "It filters by author."
    cfg = make_cfg(
        tmp_path,
        FakeBrowse(),
        [
            browse_step(),
            finish_step({"answer": bad_one}),
            browse_step(),
            finish_step({"answer": bad_two}),
        ],
    )
    for answer in (bad_one, bad_two):
        plant_verdict(
            store, tmp_path, QUESTION, answer, fid,
            {"verdict": "fail", "contradiction": CONTRA},
        )
    outcome = ask_question(
        store, episode, QUESTION, [fid], cfg,
        make_checker(tmp_path, answer_retries=1), cap=5,
    )
    assert outcome.outcome == "answer_unavailable"
    assert outcome.answer is None
    assert outcome.contradiction == CONTRA
    (row,) = qa_rows(store)
    assert row["outcome"] == "answer_unavailable"
    assert row["checker_verdict"] == "fail"
    assert row["budget_counted"] == 0  # the slot was refunded (R12)
    assert json.loads(row["contradiction_json"]) == CONTRA
    assert questions_spent(store, episode) == 0
    # the tuple is queued for human review
    (review,) = store.conn.execute(
        "SELECT * FROM review_queue ORDER BY id"
    ).fetchall()
    assert review["kind"] == "answer_unavailable"
    assert review["qa_log_id"] == row["id"]
    assert review["status"] == "open"
    payload = json.loads(review["payload_json"])
    assert payload["question"] == QUESTION
    assert payload["last_answer"] == bad_two
    assert payload["contradiction"] == CONTRA


def test_ungrounded_answer_fails_without_checker(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    fid = mint(store, tmp_path, "bookmark-tag-filter")
    # the explorer finishes WITHOUT browsing: no fresh observation exists,
    # so the answer never reaches the checker (no fixture planted) and the
    # slot ends answer_unavailable through the same retry/failure arms
    cfg = make_cfg(tmp_path, FakeBrowse(), [finish_step({"answer": "Yes."})])
    outcome = ask_question(
        store, episode, QUESTION, [fid], cfg, make_checker(tmp_path), cap=5
    )
    assert outcome.outcome == "answer_unavailable"
    assert outcome.contradiction["evidence_ref"] == "fresh-observation"
    assert "no fresh UI observation" in outcome.contradiction["observed"]
    assert questions_spent(store, episode) == 0


def test_budget_exhaustion_bounce_typed_and_logged(tmp_path, caplog):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    fid = mint(store, tmp_path, "bookmark-tag-filter")
    answer = "Yes - tags filter the list."
    cfg = make_cfg(
        tmp_path, FakeBrowse(), [browse_step(), finish_step({"answer": answer})]
    )
    plant_verdict(
        store, tmp_path, QUESTION, answer, fid,
        {"verdict": "pass", "contradiction": None},
    )
    first = ask_question(
        store, episode, QUESTION, [fid], cfg, make_checker(tmp_path), cap=1
    )
    assert first.outcome == "answered"
    msgs_before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM trace_msg"
    ).fetchone()["n"]
    with caplog.at_level(logging.INFO, logger="agent_families.pipeline.explorer"):
        bounced = ask_question(
            store, episode, "And does it archive?", [fid], cfg,
            make_checker(tmp_path), cap=1,
        )
    # question N+1 gets the TYPED bounce (R13): no session ran (the script
    # has no steps left and was not consumed), no MSG written, a typed
    # qa_log row records it for the elicitation-efficiency metric
    assert bounced.outcome == "budget_exhausted"
    assert bounced.question_msg_id is None
    rows = qa_rows(store)
    assert rows[-1]["outcome"] == "budget_exhausted"
    assert rows[-1]["budget_counted"] == 0
    assert (
        store.conn.execute("SELECT COUNT(*) AS n FROM trace_msg").fetchone()["n"]
        == msgs_before
    )
    assert questions_spent(store, episode) == 1
    assert any("budget exhausted" in r.message for r in caplog.records)


def test_answer_prompt_is_fresh_context_with_history():
    history = "Q: earlier question\nA: earlier answer"
    prompt = build_answer_prompt(QUESTION, history)
    # R14: a NEW instance reads the persistent Q&A log; nothing else carries over
    assert "fresh explorer instance" in prompt
    assert history in prompt
    retry = build_answer_prompt(QUESTION, history, contradiction=CONTRA)
    # R12: the retry embeds the checker's contradiction object verbatim
    assert json.dumps(CONTRA, sort_keys=True, ensure_ascii=False) in retry
    assert "rejected by the grader-side checker" in retry


# --- R13: the plan-checker's assumptions[] conversion (closes the Phase 1 seam) ----


def planner_step(plan: dict) -> dict:
    return {"role": "planner", "envelope": envelope(plan)}


def write_planner_script(tmp_path: Path, plans: list[dict], name: str) -> Path:
    script = tmp_path / name
    script.write_text(
        json.dumps({"steps": [planner_step(p) for p in plans]}, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return script


def write_spec(tmp_path: Path) -> Path:
    spec = tmp_path / "toy-spec.md"
    if not spec.exists():
        spec.write_text(SPEC, encoding="utf-8", newline="\n")
    return spec


def simple_plan(run_id: int, *, assumptions: list[str]) -> dict:
    return {
        "requirements": [
            {
                "id": "REQ-1",
                "text": "dashboard lists bookmarks",
                "source_msg": f"MSG-r{run_id}-p001",
            }
        ],
        "tickets": [
            {
                "id": "TKT-1",
                "title": "dashboard",
                "description": "build the dashboard",
                "covers": ["REQ-1"],
                "depends_on": [],
                "files": ["src/dashboard.ts"],
                "acceptance_criteria": [
                    {"id": "AC-1", "text": "bookmarks listed", "req": "REQ-1"}
                ],
            }
        ],
        "assumptions": assumptions,
    }


def test_assumptions_converted_budget_counted_and_overbudget_unverified(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    fid = mint(store, tmp_path, "bookmark-tag-filter")
    run_id = store.create_run("specs/toy.md", 0, episode_id=episode, increment_index=1)
    assumptions = ["tags are case-insensitive", "untagged bookmarks are listed"]
    run_planning(
        store,
        run_id,
        write_spec(tmp_path),
        planner_profile(model=MODEL, max_turns=1, timeout_s=60.0),
        transcript_dir=tmp_path / "transcripts",
        cap=1,
        max_retries=0,
        size_budget=4,
        mode="scripted",
        script_path=write_planner_script(
            tmp_path,
            [simple_plan(run_id, assumptions=assumptions)],
            "planner-script.json",
        ),
    )
    answer = "Confirmed: tag matching ignores case."
    cfg = make_cfg(
        tmp_path, FakeBrowse(), [browse_step(), finish_step({"answer": answer})]
    )
    plant_verdict(
        store, tmp_path, build_assumption_question(assumptions[0]), answer, fid,
        {"verdict": "pass", "contradiction": None},
    )
    records = check_plan_assumptions(
        store,
        run_id,
        lambda q: ask_question(
            store, episode, q, [fid], cfg, make_checker(tmp_path),
            cap=1, run_id=run_id,
        ),
    )
    # conversion 1 rode the budget and verified; conversion 2 hit the cap
    # and is recorded `unverified` — a typed risk, not a blocker (R13)
    assert records[0]["status"] == "verified"
    assert records[0]["answer"] == answer
    assert records[1]["status"] == "unverified"
    assert records[1]["reason"] == "budget_exhausted"
    assert plan_report(store, run_id)["assumption_checks"] == records
    rows = qa_rows(store)
    assert [r["outcome"] for r in rows] == ["answered", "budget_exhausted"]
    assert [r["budget_counted"] for r in rows] == [1, 0]
    assert questions_spent(store, episode, run_id) == 1


# --- R11: UAT feedback -> carry-in -> kind: bug tickets with full REQ links --------


def test_uat_rejection_feeds_bug_tickets_with_full_req_links(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    fid = mint(store, tmp_path, "bookmark-tag-filter")
    run1 = store.create_run("specs/toy.md", 0, episode_id=episode, increment_index=1)
    feedback = "The tag filter forgets my selection after a page refresh."
    uat_cfg = make_cfg(
        tmp_path,
        FakeBrowse(),
        [
            browse_step(),
            finish_step(
                {
                    "verdict": "rejected",
                    "feedback": [{"content": feedback, "mentions": [fid]}],
                }
            ),
        ],
        name="uat-script.json",
    )
    uat = run_uat(
        store, episode, 1, uat_cfg,
        briefing=f"Delivered: tag filtering ({fid}).",
        run_id=run1,
    )
    assert uat.verdict == "rejected"
    assert store.get_run(run1)["acceptance"] == "rejected"
    uat_msg = uat.feedback_msg_ids[0]
    assert uat_msg == f"MSG-e{episode}-inc001-uat01"
    assert mention_feats(store, uat_msg) == [fid]
    # UAT is budget-free: acceptance is not elicitation (R13)
    assert qa_rows(store) == []

    # the next increment's (fake) planner extracts the bug REQ from the UAT
    # MSG and writes an ordinary ticket tagged kind: bug, fully linked
    run2 = store.create_run("specs/toy.md", 0, episode_id=episode, increment_index=2)
    bug_plan = {
        "requirements": [
            {
                "id": "REQ-1",
                "text": "dashboard lists bookmarks",
                "source_msg": f"MSG-r{run2}-p001",
            },
            {
                "id": "REQ-BUG",
                "text": "tag filter selection survives refresh",
                "source_msg": uat_msg,
            },
        ],
        "tickets": [
            {
                "id": "TKT-BUG",
                "title": "fix tag filter persistence",
                "description": "persist the selected tag across refreshes",
                "covers": ["REQ-BUG"],
                "depends_on": [],
                "files": ["src/tags.ts"],
                "acceptance_criteria": [
                    {
                        "id": "AC-B",
                        "text": "selection survives refresh",
                        "req": "REQ-BUG",
                    }
                ],
                "kind": "bug",
            },
            {
                "id": "TKT-1",
                "title": "dashboard",
                "description": "build the dashboard",
                "covers": ["REQ-1"],
                "depends_on": [],
                "files": ["src/dashboard.ts"],
                "acceptance_criteria": [
                    {"id": "AC-1", "text": "bookmarks listed", "req": "REQ-1"}
                ],
            },
        ],
        "assumptions": [],
    }
    result = run_planning(
        store,
        run2,
        write_spec(tmp_path),
        planner_profile(model=MODEL, max_turns=1, timeout_s=60.0),
        transcript_dir=tmp_path / "transcripts",
        cap=1,
        max_retries=0,
        size_budget=4,
        mode="scripted",
        script_path=write_planner_script(tmp_path, [bug_plan], "planner2.json"),
        carry_in_msg_ids=[uat_msg],
    )
    bug_tkt, feature_tkt = result.plan["tickets"]
    assert bug_tkt["kind"] == "bug"
    assert feature_tkt["kind"] == "feature"  # the default: kind is a tag
    # full §12 traceability: REQ -> the UAT MSG, TKT covers REQ, AC -> REQ
    bug_req = store.conn.execute(
        "SELECT * FROM trace_req WHERE source_msg_id = ?", (uat_msg,)
    ).fetchone()
    assert bug_req is not None
    covers = store.conn.execute(
        "SELECT tkt_id FROM trace_tkt_covers WHERE req_id = ?", (bug_req["id"],)
    ).fetchall()
    assert [c["tkt_id"] for c in covers] == [bug_tkt["id"]]
    ac = store.conn.execute(
        "SELECT * FROM trace_ac WHERE ticket_id = ?", (bug_tkt["id"],)
    ).fetchone()
    assert ac["req_id"] == bug_req["id"]


def test_uat_accepted_records_acceptance_and_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    run1 = store.create_run("specs/toy.md", 0, episode_id=episode, increment_index=1)
    cfg = make_cfg(
        tmp_path,
        FakeBrowse(),
        [finish_step({"verdict": "accepted", "feedback": []})],
    )
    uat = run_uat(store, episode, 1, cfg, briefing="Delivered: dashboard.", run_id=run1)
    assert uat.verdict == "accepted"
    assert uat.feedback_msg_ids == ()
    assert store.get_run(run1)["acceptance"] == "accepted"
    assert (
        store.conn.execute("SELECT COUNT(*) AS n FROM trace_msg").fetchone()["n"]
        == 0
    )


def test_uat_rejected_without_feedback_is_an_error(tmp_path):
    store = make_store(tmp_path)
    episode = store.create_episode(TARGET, DIGEST, 0)
    cfg = make_cfg(
        tmp_path,
        FakeBrowse(),
        [finish_step({"verdict": "rejected", "feedback": []})],
    )
    with pytest.raises(ExplorerError, match="feedback"):
        run_uat(store, episode, 1, cfg, briefing="Delivered: dashboard.")


def test_planner_prompt_carries_uat_feedback_and_bug_instruction(tmp_path):
    store = make_store(tmp_path)
    run_id = store.create_run("specs/toy.md", 0)
    from agent_families.pipeline.planning import synthesize_messages

    messages = synthesize_messages(store, run_id, write_spec(tmp_path))
    prompt = build_planner_prompt(
        "toy-spec.md",
        messages,
        4,
        carry_in=[("MSG-e1-inc001-uat01", "The tag filter forgets my selection.")],
    )
    assert "[MSG-e1-inc001-uat01] The tag filter forgets my selection." in prompt
    assert "kind is 'bug'" in prompt


def test_planning_rejects_unknown_carry_in_msg(tmp_path):
    store = make_store(tmp_path)
    run_id = store.create_run("specs/toy.md", 0)
    with pytest.raises(PlanningError, match="carry-in message"):
        run_planning(
            store,
            run_id,
            write_spec(tmp_path),
            planner_profile(model=MODEL, max_turns=1, timeout_s=60.0),
            transcript_dir=tmp_path / "transcripts",
            cap=1,
            max_retries=0,
            size_budget=4,
            mode="scripted",
            script_path=write_planner_script(
                tmp_path, [simple_plan(run_id, assumptions=[])], "p.json"
            ),
            carry_in_msg_ids=["MSG-does-not-exist"],
        )
