"""Retrieval-into-prompts tests (plan-004 U2, R2/R3/R4).

Fully offline: vectors are hand-written (the U5 registration write shape), the
store is exercised directly, and no embedding model / judge / subprocess runs.

## Conformance

Each named invariant from the plan's "Required acceptance tests" maps 1:1 to a
behavioral test here:

- `test_budget_caps_injection` → R3 bloat invariant: injected tokens ≤ budget AND
  every injected skill is whole (no mid-skill truncation).
- `test_relevance_is_self_focusing` → a wider pool does not dilute a focused query
  (zero cluster-B skills in the injection for a cluster-A query).
- `test_pool_is_family_scoped` → the candidate pool is the family's active set
  (not one agent's partition); the own-skills-prior parameter exists and defaults
  to the configured share.
- `test_ranking_honors_full_text_cosine` → ranking uses member-body cosine, not
  skill-description similarity (SkillRouter body-signal invariant).
- `test_quarantine_visibility_by_mode` → quarantined insight invisible in
  `training`, visible only in its batch's `trial`.
- `test_injection_byte_stable` → identical inputs → byte-identical injected section.

Supporting scenario coverage: drop-log rank+size (R3), relevance-gate drops,
trial-requires-batch / unknown-mode guards, the prompt-assembly seam, and the R2
query builders.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_families.library.retrieval import (
    DEFAULT_OWN_SKILLS_SHARE,
    DROP_BUDGET,
    DROP_RELEVANCE_GATE,
    RetrievalError,
    RetrievalParams,
    count_tokens,
    planner_query,
    retrieve,
    render_injection_section,
    verifier_query,
    worker_query,
)
from agent_families.pipeline.ticket_loop import build_worker_prompt
from agent_families.store import Store
from agent_families.vecindex import VecIndex

DIM = 4
QUERY_A = [1.0, 0.0, 0.0, 0.0]  # the focused query / cluster-A direction
NEAR_A = [1.0, 0.0, 0.0, 0.0]  # cosine 1.0 with QUERY_A
CLUSTER_B = [0.0, 1.0, 0.0, 0.0]  # cosine 0.0 with QUERY_A


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    family_id = store.create_family("worker")
    agent_id = store.create_agent(family_id, "generalist")
    yield SimpleNamespace(
        store=store,
        vec=vec,
        family_id=family_id,
        agent_id=agent_id,
        counter=0,
    )
    store.close()


def _insert_member(
    env,
    skill_id,
    *,
    vector,
    status="active",
    batch=None,
):
    """Insert insight + vec row + membership; promote to active unless quarantined.

    Mirrors the U5 registration write shape used elsewhere in the suite.
    """
    env.counter += 1
    n = env.counter
    with env.store.transaction():
        batch_id = env.store.ensure_batch(batch) if batch is not None else None
        insight_id = env.store.insert_insight(
            precondition=f"precondition {n}",
            action=f"action {n}",
            expected_outcome=f"outcome {n}",
            content_hash=f"hash-{n}",
            status="quarantined",
            batch_id=batch_id,
        )
        env.vec.insert(insight_id, vector)
        env.store.append_member(skill_id, insight_id)
    if status == "active":
        with env.store.queue_operation("promote", f"seed {n}") as snap:
            env.store.set_status(insight_id, "active", snap)
    return insight_id


def make_skill(env, name, *, agent_id=None, description="A description."):
    return env.store.create_skill(
        agent_id if agent_id is not None else env.agent_id, name, description
    )


def params(budget, *, floor=0.5, own_share=DEFAULT_OWN_SKILLS_SHARE):
    return RetrievalParams(
        budget_tokens=budget, relevance_floor=floor, own_skills_share=own_share
    )


# --- R3: budget caps injection, whole skills only ----------------------------


def test_budget_caps_injection(env):
    # >=3x budget of relevant skills available; each near the query.
    skill_ids = []
    for i in range(10):
        sid = make_skill(env, f"skill-{i}", description=f"desc {i}")
        _insert_member(env, sid, vector=NEAR_A)
        skill_ids.append(sid)

    # Per-skill rendered size, then a budget that fits only ~3 of them.
    renderer_result = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000),
    )
    per_skill = renderer_result.candidates[0].token_count
    assert per_skill >= 3  # the budget math below relies on this
    budget = 3 * per_skill + 1

    result = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(budget),
    )

    # Bloat invariant: total injected tokens never exceed the budget.
    assert result.injected_token_count <= budget
    # 10 relevant skills available, far more than 3x the budget's capacity.
    assert len(result.candidates) == 10
    assert len(result.skills) <= 3
    assert result.drops  # the overflow is dropped, not truncated

    # Every injected skill is WHOLE: its full rendered bytes appear intact.
    for cand in result.candidates:
        if cand.skill_id in result.skills:
            assert cand.content in result.injected_bytes
    # Reassembling the included skills' bytes reproduces the injection exactly.
    included = [c for c in result.candidates if c.skill_id in result.skills]
    assert b"\n".join(c.content for c in included) == result.injected_bytes
    # Overflow skills were dropped for budget, with rank and size logged.
    assert all(d.reason == DROP_BUDGET for d in result.drops)
    assert all(d.rank > 0 and d.token_count > 0 for d in result.drops)


# --- a wider pool does not dilute a focused query ----------------------------


def test_relevance_is_self_focusing(env):
    a_skills = []
    for i in range(3):
        sid = make_skill(env, f"a-{i}")
        _insert_member(env, sid, vector=NEAR_A)
        a_skills.append(sid)
    b_skills = []
    b_insights = []
    for i in range(3):
        sid = make_skill(env, f"b-{i}")
        b_insights.append(_insert_member(env, sid, vector=CLUSTER_B))
        b_skills.append(sid)

    # Floor sits between cluster A (cosine 1.0) and cluster B (cosine 0.0).
    result = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000, floor=0.5),
    )

    # The injection contains ZERO cluster-B skills — B scored below the margin.
    assert set(result.skills) == set(a_skills)
    assert not (set(result.skills) & set(b_skills))
    # Cluster-B insights are in the family pool but gated out of the injection.
    for iid in b_insights:
        assert iid in result.pool_insight_ids
    gated = {d.skill_id for d in result.drops if d.reason == DROP_RELEVANCE_GATE}
    assert gated == set(b_skills)


# --- the pool is family-scoped, not agent-partitioned ------------------------


def test_pool_is_family_scoped(env):
    # Two agents in the same family; the working agent owns only its partition.
    sibling_agent = env.store.create_agent(env.family_id, "sibling")
    own_skill = make_skill(env, "own", agent_id=env.agent_id)
    own_insight = _insert_member(env, own_skill, vector=NEAR_A)
    sib_skill = make_skill(env, "sibling-skill", agent_id=sibling_agent)
    sib_insight = _insert_member(env, sib_skill, vector=NEAR_A)

    result = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        working_agent_id=env.agent_id, params=params(10_000),
    )

    # The candidate POOL spans the family — both the agent's own and the
    # sibling's active insights — not just the working agent's partition.
    assert own_insight in result.pool_insight_ids
    assert sib_insight in result.pool_insight_ids
    scored = {c.skill_id for c in result.candidates}
    assert {own_skill, sib_skill} <= scored

    # The own-skills-prior parameter exists and defaults to the configured share.
    assert RetrievalParams(budget_tokens=1, relevance_floor=0.5).own_skills_share == (
        DEFAULT_OWN_SKILLS_SHARE
    )
    # The own skill is ranked ahead of the equally-relevant sibling (the prior).
    own_rank = next(i for i, c in enumerate(result.candidates) if c.skill_id == own_skill)
    sib_rank = next(i for i, c in enumerate(result.candidates) if c.skill_id == sib_skill)
    assert own_rank < sib_rank


# --- ranking uses member-body cosine, not skill-description similarity --------


def test_ranking_honors_full_text_cosine(env):
    # Skill whose DESCRIPTION looks query-shaped but whose member BODY is far.
    desc_skill = make_skill(
        env, "desc-similar", description="login authentication query match"
    )
    _insert_member(env, desc_skill, vector=CLUSTER_B)  # body irrelevant
    # Skill with a plain description but a member BODY near the query.
    body_skill = make_skill(env, "body-relevant", description="unrelated words")
    _insert_member(env, body_skill, vector=NEAR_A)  # body relevant

    result = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000, floor=0.0),
    )

    # Ranking is by member-body cosine: the body-relevant skill outranks the
    # description-similar-but-body-irrelevant one.
    assert result.candidates[0].skill_id == body_skill
    by_id = {c.skill_id: c.score for c in result.candidates}
    assert by_id[body_skill] > by_id[desc_skill]


# --- quarantine visibility is mode-keyed (R4) --------------------------------


def test_quarantine_visibility_by_mode(env):
    active_skill = make_skill(env, "active-skill")
    _insert_member(env, active_skill, vector=NEAR_A)
    quar_skill = make_skill(env, "quar-skill")
    quar_insight = _insert_member(
        env, quar_skill, vector=NEAR_A, status="quarantined", batch="b1"
    )
    batch_id = env.store.ensure_batch("b1")

    # training: the quarantined insight is invisible — not in the pool, not injected.
    training = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000), mode="training",
    )
    assert quar_insight not in training.pool_insight_ids
    assert quar_skill not in training.skills

    # trial for its own batch: the quarantined insight becomes visible.
    trial = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000), mode="trial", batch_id=batch_id,
    )
    assert quar_insight in trial.pool_insight_ids
    assert quar_skill in trial.skills


def test_quarantine_other_batch_invisible_in_trial(env):
    # A trial for batch b1 must not surface batch b2's quarantined insight.
    quar_skill = make_skill(env, "other-batch")
    other = _insert_member(
        env, quar_skill, vector=NEAR_A, status="quarantined", batch="b2"
    )
    b1 = env.store.ensure_batch("b1")
    trial = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000), mode="trial", batch_id=b1,
    )
    assert other not in trial.pool_insight_ids


# --- byte stability ----------------------------------------------------------


def test_injection_byte_stable(env):
    for i in range(4):
        sid = make_skill(env, f"s-{i}", description=f"desc {i}")
        _insert_member(env, sid, vector=NEAR_A)

    first = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000),
    )
    second = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000),
    )
    assert first.injected_bytes == second.injected_bytes
    assert first.skills == second.skills
    assert first.injected_bytes  # non-empty (the section actually rendered)


# --- guards ------------------------------------------------------------------


def test_trial_requires_batch(env):
    with pytest.raises(RetrievalError, match="trial-mode retrieval requires"):
        retrieve(
            env.store, query_vector=QUERY_A, family_id=env.family_id,
            params=params(10_000), mode="trial",
        )


def test_unknown_mode_rejected(env):
    with pytest.raises(RetrievalError, match="unknown run mode"):
        retrieve(
            env.store, query_vector=QUERY_A, family_id=env.family_id,
            params=params(10_000), mode="nonsense",
        )


def test_params_validate_ranges():
    with pytest.raises(RetrievalError, match="budget_tokens"):
        RetrievalParams(budget_tokens=0, relevance_floor=0.5)
    with pytest.raises(RetrievalError, match="relevance_floor"):
        RetrievalParams(budget_tokens=1, relevance_floor=1.5)
    with pytest.raises(RetrievalError, match="own_skills_share"):
        RetrievalParams(budget_tokens=1, relevance_floor=0.5, own_skills_share=2.0)


# --- the prompt-assembly seam ------------------------------------------------


def test_prompt_injection_seam_round_trips(env):
    sid = make_skill(env, "elicitation")
    _insert_member(env, sid, vector=NEAR_A)
    result = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000),
    )
    section = render_injection_section(result)
    assert "Skill: elicitation" in section

    ticket = {
        "id": "TKT-A", "title": "login", "description": "build login",
        "files": ["src/login.ts"],
        "acceptance_criteria": [{"id": "AC-1", "text": "user can log in"}],
    }
    ledger = "(no prior iterations)"
    injected = build_worker_prompt(ticket, ledger, injected_skills=section)
    # A planted high-relevance skill appears in the (fake) session's prompt.
    assert "Skill: elicitation" in injected
    # The seam is inert by default — empty injection reproduces the bare prompt.
    assert build_worker_prompt(ticket, ledger, injected_skills="") == (
        build_worker_prompt(ticket, ledger)
    )


def test_empty_pool_injects_nothing(env):
    result = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        params=params(10_000),
    )
    assert result.skills == ()
    assert result.injected_bytes == b""
    assert render_injection_section(result) == ""


# --- R2 query builders -------------------------------------------------------


def test_query_builders_assemble_per_family_signals():
    assert planner_query(["want tags"], ["Q: how? A: like so"]) == (
        "want tags\nQ: how? A: like so"
    )
    ticket = {
        "title": "Login", "description": "build it",
        "acceptance_criteria": [{"id": "AC-1", "text": "logs in"}],
    }
    wq = worker_query(ticket)
    assert "Login" in wq and "build it" in wq and "logs in" in wq
    vq = verifier_query(ticket, ["[verifier_check] AC-1 failed"])
    assert "logs in" in vq and "verifier_check" in vq


def test_count_tokens_is_whitespace_runs():
    assert count_tokens("one two   three\nfour") == 4
    assert count_tokens("   ") == 0
