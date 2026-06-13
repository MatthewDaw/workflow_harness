"""The assign stage — plan-010 U1 (R1–R3).

Fully offline: the store is exercised directly, retrieval vectors are
hand-written (the U5 registration write shape), ``embed_query`` is a scripted
fake, and the seam-wiring tests short-circuit ``run_session`` so no embedding
model / judge / subprocess ever runs.

## Conformance

Each named MUST-test from plan-010 U1 maps 1:1 to a behavioral test here:

- `test_empty_retrieval_is_byte_identical_to_today` → over an empty store assign
  yields ``injected_skills == ""`` and the three built prompts
  (`build_worker_prompt`/`build_verifier_prompt`/`build_planner_prompt`) fed that
  output are BYTE-IDENTICAL to the bare empty-`injected_skills=""` prompts — the
  inert seam, populated with nothing, perturbs not a single byte.
- `test_assign_retrieves_whole_store_not_family_scoped` → an insight owned by a
  *different* family/agent than the ticket is retrieved purely by relevance, and
  a spy on `retrieve` proves assign passes NO `family_id`/`working_agent_id`
  (ownership ≠ reachability, R3 §4).
- `test_no_router_or_persona_invoked` → with `library.router.route` patched to
  RAISE, assign still completes and still retrieves — knowledge specialization
  comes only from the index, never a per-ticket router (R3).
- `test_file_territory_matches_file_ownership_conflicts` → the `file_territory`
  assign emits is exactly `planning.file_ownership_conflicts` over the same
  ticket set — no net-new allocation logic, identical partition.
- `test_injected_skills_wired_to_three_seams` → assign's `injected_skills` output
  reaches `ticket_loop.py:417` (worker), `ticket_loop.py:521` (verifier), and
  `planning.py:1232` (planner); with no provider the seam stays empty (no new
  branch — control flow is unchanged).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent_families.library.router as router_module
import agent_families.pipeline.assign as assign_module
import agent_families.pipeline.planning as planning_module
import agent_families.pipeline.ticket_loop as ticket_loop_module
from agent_families.library.retrieval import RetrievalParams, retrieve
from agent_families.pipeline.assign import (
    STAGE_PLANNER,
    STAGE_VERIFIER,
    STAGE_WORKER,
    AssignError,
    assign,
)
from agent_families.pipeline.orchestrator import TicketContext
from agent_families.pipeline.planning import (
    build_planner_prompt,
    file_ownership_conflicts,
    run_planning,
)
from agent_families.pipeline.sessions import (
    planner_profile,
    verifier_profile,
    worker_profile,
)
from agent_families.pipeline.ticket_loop import (
    TicketLoop,
    TicketLoopConfig,
    build_verifier_prompt,
    build_worker_prompt,
)
from agent_families.store import Store
from agent_families.vecindex import VecIndex

DIM = 4
QUERY_A = [1.0, 0.0, 0.0, 0.0]  # the focused query direction
NEAR_A = [1.0, 0.0, 0.0, 0.0]  # cosine 1.0 with QUERY_A
FAR_A = [0.0, 1.0, 0.0, 0.0]  # cosine 0.0 with QUERY_A


# --- store / seeding scaffolding (mirrors test_retrieval) ---------------------


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    yield SimpleNamespace(store=store, vec=vec, counter=0)
    store.close()


def _insert_insight(env, *, vector, skill_id=None, status="active"):
    """Insert insight + vec row (+ optional membership); promote to active."""
    env.counter += 1
    n = env.counter
    with env.store.transaction():
        insight_id = env.store.insert_insight(
            precondition=f"precondition {n}",
            action=f"action {n}",
            expected_outcome=f"outcome {n}",
            content_hash=f"hash-{n}",
            status="quarantined",
        )
        env.vec.insert(insight_id, vector)
        if skill_id is not None:
            env.store.append_member(skill_id, insight_id)
    if status == "active":
        with env.store.queue_operation("promote", f"seed {n}") as snap:
            env.store.set_status(insight_id, "active", snap)
    return insight_id


def make_skill(env, name, *, family_name="worker", agent_name="generalist"):
    family_id = env.store.create_family(family_name)
    agent_id = env.store.create_agent(family_id, agent_name)
    return env.store.create_skill(agent_id, name, "A description.")


def fixed_embed(vector):
    """A scripted ``embed_query`` that ignores the text and returns ``vector``
    (the embed seam owns the ``search_query:`` prefix; this fake stands in for
    EmbeddingService.embed_query so the test stays offline)."""

    def _embed(_text: str) -> list[float]:
        return list(vector)

    return _embed


def params(budget=10_000, *, floor=0.5):
    return RetrievalParams(budget_tokens=budget, relevance_floor=floor)


TICKET = {
    "id": "TKT-A",
    "title": "login",
    "description": "build login",
    "files": ["src/login.ts"],
    "acceptance_criteria": [{"id": "AC-1", "text": "user can log in"}],
}
LEDGER = "(no prior iterations)"


# === byte-identity: the inert seam populated with nothing =====================


def test_empty_retrieval_is_byte_identical_to_today(env):
    """Empty store → assign injects nothing → the three built prompts are
    byte-identical to the bare empty-`injected_skills=""` prompts."""
    embed = fixed_embed(QUERY_A)

    worker_ctx = assign(
        store=env.store, stage=STAGE_WORKER, embed_query=embed,
        params=params(), ticket=TICKET,
    )
    verifier_ctx = assign(
        store=env.store, stage=STAGE_VERIFIER, embed_query=embed,
        params=params(), ticket=TICKET,
    )
    planner_ctx = assign(
        store=env.store, stage=STAGE_PLANNER, embed_query=embed,
        params=params(), increment_request_msgs=["want login"], qa_transcript=[],
    )

    assert worker_ctx.injected_skills == ""
    assert verifier_ctx.injected_skills == ""
    assert planner_ctx.injected_skills == ""

    # Built through assign's own output — proves byte-identity, not a green run.
    assert build_worker_prompt(
        TICKET, LEDGER, injected_skills=worker_ctx.injected_skills
    ) == build_worker_prompt(TICKET, LEDGER)
    assert build_verifier_prompt(
        TICKET, None, injected_skills=verifier_ctx.injected_skills
    ) == build_verifier_prompt(TICKET, None)
    assert build_planner_prompt(
        "toy.md", [], 4, injected_skills=planner_ctx.injected_skills
    ) == build_planner_prompt("toy.md", [], 4)


# === whole-store reachability, no family scope ================================


def test_assign_retrieves_whole_store_not_family_scoped(env, monkeypatch):
    """An insight owned by a different family/agent than the ticket is retrieved
    purely by relevance, and assign passes NO family/agent filter to retrieve."""
    # Owned by an unrelated family/agent — ownership must not gate reachability.
    other_skill = make_skill(
        env, "elicitation", family_name="explorer", agent_name="probe"
    )
    insight_id = _insert_insight(env, vector=NEAR_A, skill_id=other_skill)

    captured = {}

    def spy_retrieve(*args, **kwargs):
        captured["kwargs"] = kwargs
        return retrieve(*args, **kwargs)

    monkeypatch.setattr(assign_module, "retrieve", spy_retrieve)

    ctx = assign(
        store=env.store, stage=STAGE_WORKER, embed_query=fixed_embed(QUERY_A),
        params=params(), ticket=TICKET,
    )

    # The cross-family insight is reachable purely by relevance.
    assert ctx.retrieval is not None
    assert insight_id in ctx.retrieval.insights
    assert ctx.injected_skills != ""
    assert "Skill: elicitation" in ctx.injected_skills

    # assign passes NO family/agent scope — the whole store is the candidate set.
    assert captured["kwargs"].get("family_id") is None
    assert captured["kwargs"].get("working_agent_id") is None


# === no router / persona machinery ===========================================


def test_no_router_or_persona_invoked(env, monkeypatch):
    """With library.router.route patched to RAISE, assign still completes and
    still retrieves — specialization comes only from the index, never a router."""
    skill = make_skill(env, "elicitation")
    _insert_insight(env, vector=NEAR_A, skill_id=skill)

    def explode(*args, **kwargs):
        raise AssertionError(
            "assign must never consult a per-ticket router or persona seam (R3)"
        )

    monkeypatch.setattr(router_module, "route", explode)
    # run_boundary_ticket was deleted in plan 009; guard it too if it reappears.
    if hasattr(router_module, "run_boundary_ticket"):
        monkeypatch.setattr(router_module, "run_boundary_ticket", explode)

    ctx = assign(
        store=env.store, stage=STAGE_WORKER, embed_query=fixed_embed(QUERY_A),
        params=params(), ticket=TICKET,
    )
    assert ctx.injected_skills != ""


# === file territory ==========================================================


def test_file_territory_matches_file_ownership_conflicts(env):
    """assign's file_territory is exactly planning.file_ownership_conflicts over
    the same ticket set — no net-new allocation logic, identical partition."""
    ticket_files = {
        "TKT-A": ["src/login.ts", "src/shared.ts"],
        "TKT-B": ["src/search.ts", "src/shared.ts"],
        "TKT-C": ["src/tags.ts"],
    }
    ctx = assign(
        store=env.store, stage=STAGE_WORKER, embed_query=fixed_embed(QUERY_A),
        params=params(), ticket=TICKET, ticket_files=ticket_files,
    )
    assert ctx.file_territory == file_ownership_conflicts(ticket_files)
    # The contested file is the partition's datum; uncontested files are absent.
    assert ctx.file_territory == {"src/shared.ts": ("TKT-A", "TKT-B")}


def test_file_territory_empty_when_no_ticket_set(env):
    ctx = assign(
        store=env.store, stage=STAGE_WORKER, embed_query=fixed_embed(QUERY_A),
        params=params(), ticket=TICKET,
    )
    assert ctx.file_territory == {}


# === stage guards ============================================================


def test_unknown_stage_rejected(env):
    with pytest.raises(AssignError, match="unknown stage"):
        assign(
            store=env.store, stage="router", embed_query=fixed_embed(QUERY_A),
            params=params(),
        )


def test_worker_stage_requires_ticket(env):
    with pytest.raises(AssignError, match="requires the ticket"):
        assign(
            store=env.store, stage=STAGE_WORKER, embed_query=fixed_embed(QUERY_A),
            params=params(),
        )


# === the three seams are wired ===============================================


class _StopSession(Exception):
    """Sentinel: short-circuit run_session after capturing the built prompt."""


def _seed_ticket(tmp_path):
    """Store + run + one seeded ticket (trace_tkt + ACs) for direct stage calls."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "library.db")
    store.migrate()
    run_id = store.create_run("specs/toy.md", 0)
    store.conn.execute("INSERT INTO trace_tkt (id) VALUES ('TKT-A')")
    with store.transaction():
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_msg (id, content) VALUES ('MSG-x', 'seed')"
        )
        store.conn.execute(
            "INSERT INTO trace_req (id, source_msg_id) VALUES ('REQ-x', 'MSG-x')"
        )
        store.conn.execute(
            "INSERT INTO trace_ac (id, ticket_id, req_id)"
            " VALUES ('AC-1', 'TKT-A', 'REQ-x')"
        )
    return store, run_id


def _loop(tmp_path, provider):
    config = TicketLoopConfig(
        worker_profile=worker_profile(
            model="sonnet", max_turns=20, timeout_s=60.0, test_commands=("npm test",)
        ),
        verifier_profile=verifier_profile(
            model="sonnet", max_turns=10, timeout_s=60.0,
            check_commands=("npm run build",),
        ),
        gate_commands=(SimpleNamespace(),),  # unused — run_session is short-circuited
        transcript_dir=tmp_path / "transcripts",
        max_retries=0,
        assign_provider=provider,
    )
    return TicketLoop(config)


def test_injected_skills_wired_to_three_seams(tmp_path, monkeypatch):
    provider = lambda ticket, stage: f"INJECT-{stage}"  # noqa: E731

    # --- worker seam (ticket_loop.py:417) ---
    store, run_id = _seed_ticket(tmp_path / "w")
    captured = {}

    def capture_worker(prompt, *a, **k):
        captured["prompt"] = prompt
        raise _StopSession

    monkeypatch.setattr(ticket_loop_module, "run_session", capture_worker)
    loop = _loop(tmp_path / "w", provider)
    ctx = TicketContext(
        store=store, run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        workspace=SimpleNamespace(root=tmp_path / "w"),
    )
    with pytest.raises(_StopSession):
        loop.worker(ctx)
    assert "INJECT-worker" in captured["prompt"]
    store.close()

    # --- verifier seam (ticket_loop.py:521) ---
    store, run_id = _seed_ticket(tmp_path / "v")
    captured = {}
    monkeypatch.setattr(ticket_loop_module, "run_session", capture_worker)
    loop = _loop(tmp_path / "v", provider)
    ctx = TicketContext(
        store=store, run_id=run_id, ticket_id="TKT-A", ralph_iteration=1,
        workspace=SimpleNamespace(root=tmp_path / "v"),
    )
    with pytest.raises(_StopSession):
        loop.verifier(ctx)
    assert "INJECT-verifier" in captured["prompt"]
    store.close()

    # --- planner seam (planning.py:1232) ---
    (tmp_path / "p").mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "p" / "library.db")
    store.migrate()
    spec = tmp_path / "p" / "toy.md"
    spec.write_text("Build a login page.\n", encoding="utf-8", newline="\n")
    run_id = store.create_run(str(spec), 0)
    captured = {}
    monkeypatch.setattr(planning_module, "run_session", capture_worker)
    with pytest.raises(_StopSession):
        run_planning(
            store, run_id, spec,
            planner_profile(model="sonnet", max_turns=5, timeout_s=60.0),
            transcript_dir=tmp_path / "p" / "transcripts",
            cap=1, max_retries=0, size_budget=4,
            assign_provider=lambda: "INJECT-planner",
        )
    assert "INJECT-planner" in captured["prompt"]

    # No provider → the seam stays empty (no new branch; control flow unchanged).
    captured = {}
    run_id2 = store.create_run(str(spec), 0)
    with pytest.raises(_StopSession):
        run_planning(
            store, run_id2, spec,
            planner_profile(model="sonnet", max_turns=5, timeout_s=60.0),
            transcript_dir=tmp_path / "p" / "transcripts2",
            cap=1, max_retries=0, size_budget=4,
        )
    assert "INJECT-planner" not in captured["prompt"]
    store.close()
