"""Retrieval tests — R3 §4 insight-level whole-store rewrite (plan-009 U6, R13).

Fully offline: vectors are hand-written (the U5 registration write shape), the
store is exercised directly, and no embedding model / judge / subprocess runs.

## Conformance

Each named MUST-test from plan-009 U6 maps 1:1 to a behavioral test here:

- `test_ownership_not_reachability_total` → an insight owned by a module the query
  has no relationship to is retrieved purely by cosine; passing an *unrelated*
  `family_id` cannot gate it (the inert no-op proves no family-pool gating is
  possible — the U6 deviation seam, see the retrieval module docstring).
- `test_no_own_skills_weighting` → `own_skills_share` / `DEFAULT_OWN_SKILLS_SHARE`
  are gone (absent on the dataclass and the module); two cosine-tied insights rank
  in stable cosine/id order, NOT own-first.
- `test_ranks_insights_not_skills` → the result carries insight-granular items and
  the budget fills insight-by-insight; two insights from the SAME skill are ranked
  and budgeted independently (no whole-skill aggregation node in the ranked output).
- `test_quarantine_visibility_by_mode_preserved` → a quarantined insight is hidden
  in `training` and visible only in its batch's `trial` (both directions asserted).
- `test_boundary_ticket_subsystem_deleted` → the R14c boundary-ticket code path is
  gone from the module (its symbols no longer exist / import).

U6 deviation (documented, not hidden — mirrors the retrieval module docstring):
under the wave's one-unit constraint the unmigrated callers (run-memory composer,
planner/worker run-assembly) are not touched, so `retrieve` keeps inert
`family_id`/`working_agent_id` no-op params and `RetrievalResult` keeps a *derived*
`skills` provenance tuple. `test_ownership_not_reachability_total` /
`test_no_own_skills_weighting` assert the no-ops cannot gate and carry no weight;
`test_ranks_insights_not_skills` asserts `insights` (not `skills`) is the ranked
granularity. U7's demotion sweep deletes the seams once callers migrate.

Supporting scenario coverage: whole-store reach (not a family pool), budget cap +
whole-insight injection, relevance-gate drops, drop-log rank+size (R3),
byte-stability, trial-requires-batch / unknown-mode guards, the prompt-assembly
seam, and the R2 query builders.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent_families.library.retrieval as retrieval_module
from agent_families.library.retrieval import (
    DROP_BUDGET,
    DROP_RELEVANCE_GATE,
    InsightCandidate,
    RetrievalError,
    RetrievalParams,
    count_tokens,
    planner_query,
    render_injection_section,
    retrieve,
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


def _insert_insight(env, *, vector, skill_id=None, status="active", batch=None):
    """Insert insight + vec row (+ optional membership); promote to active unless
    quarantined. Mirrors the U5 registration write shape used elsewhere."""
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
        if skill_id is not None:
            env.store.append_member(skill_id, insight_id)
    if status == "active":
        with env.store.queue_operation("promote", f"seed {n}") as snap:
            env.store.set_status(insight_id, "active", snap)
    return insight_id


def make_skill(env, name, *, agent_id=None, description="A description."):
    return env.store.create_skill(
        agent_id if agent_id is not None else env.agent_id, name, description
    )


def params(budget, *, floor=0.5):
    return RetrievalParams(budget_tokens=budget, relevance_floor=floor)


# === MUST-test: ownership ≠ reachability, total ==============================


def test_ownership_not_reachability_total(env):
    # A second family/agent the query has NO relationship to owns a relevant skill.
    other_family = env.store.create_family("unrelated")
    other_agent = env.store.create_agent(other_family, "stranger")
    far_skill = make_skill(env, "far-away", agent_id=other_agent)
    far_insight = _insert_insight(env, vector=NEAR_A, skill_id=far_skill)

    # Retrieval reaches it purely by cosine — no scope argument at all.
    result = retrieve(env.store, query_vector=QUERY_A, params=params(10_000))
    assert far_insight in result.insights
    assert far_insight in result.pool_insight_ids

    # The inert family_id no-op cannot gate it: passing the WRONG family (the
    # working family, not the owner's) still returns the cross-module insight —
    # proving no family-pool gating is possible.
    gated = retrieve(
        env.store, query_vector=QUERY_A, params=params(10_000),
        family_id=env.family_id, working_agent_id=env.agent_id,
    )
    assert far_insight in gated.insights
    assert gated.insights == result.insights  # the no-op changed nothing


# === MUST-test: no own-skills weighting =====================================


def test_no_own_skills_weighting(env):
    # The own-skills prior is gone from the public surface entirely.
    p = RetrievalParams(budget_tokens=1, relevance_floor=0.5)
    assert not hasattr(p, "own_skills_share")
    assert not hasattr(retrieval_module, "DEFAULT_OWN_SKILLS_SHARE")

    # Two cosine-tied insights, one "owned" by the working agent, one by a sibling.
    own_skill = make_skill(env, "own", agent_id=env.agent_id)
    sibling_agent = env.store.create_agent(env.family_id, "sibling")
    sib_skill = make_skill(env, "sib", agent_id=sibling_agent)
    own_insight = _insert_insight(env, vector=NEAR_A, skill_id=own_skill)
    sib_insight = _insert_insight(env, vector=NEAR_A, skill_id=sib_skill)

    result = retrieve(
        env.store, query_vector=QUERY_A, params=params(10_000),
        working_agent_id=env.agent_id,
    )
    # Identical cosine → stable cosine/id order (lower id first), NOT own-first.
    ordered = [c.insight_id for c in result.candidates]
    assert ordered.index(own_insight) < ordered.index(sib_insight)  # only because id is lower
    assert min(own_insight, sib_insight) == own_insight  # own was inserted first
    # The tie is broken by id, not ownership: scores are equal.
    by_id = {c.insight_id: c.score for c in result.candidates}
    assert by_id[own_insight] == by_id[sib_insight]


# === MUST-test: ranks insights, not skills ==================================


def test_ranks_insights_not_skills(env):
    # TWO insights in the SAME skill, at different relevances.
    skill = make_skill(env, "multi")
    near = _insert_insight(env, vector=NEAR_A, skill_id=skill)  # cosine 1.0
    mid = _insert_insight(env, vector=[0.8, 0.6, 0.0, 0.0], skill_id=skill)  # ~0.8

    result = retrieve(env.store, query_vector=QUERY_A, params=params(10_000, floor=0.0))
    # The result carries insight-granular items (insight ids), both from one skill.
    assert set(result.insights) == {near, mid}
    assert all(isinstance(c, InsightCandidate) for c in result.candidates)
    assert all(hasattr(c, "insight_id") for c in result.candidates)
    # Ranked by per-insight cosine, not aggregated to the skill: near outranks mid.
    assert result.insights.index(near) < result.insights.index(mid)

    # Budget fills insight-by-insight: a budget that fits only the higher-cosine
    # insight drops the other — same-skill insights are budgeted INDEPENDENTLY,
    # not as one whole-skill block.
    one = next(c for c in result.candidates if c.insight_id == near)
    tight = retrieve(
        env.store, query_vector=QUERY_A,
        params=params(one.token_count, floor=0.0),
    )
    assert tight.insights == (near,)
    assert any(d.insight_id == mid and d.reason == DROP_BUDGET for d in tight.drops)


# === MUST-test: quarantine visibility by mode ===============================


def test_quarantine_visibility_by_mode_preserved(env):
    active = _insert_insight(env, vector=NEAR_A)
    quar = _insert_insight(env, vector=NEAR_A, status="quarantined", batch="b1")
    batch_id = env.store.ensure_batch("b1")

    # training: the quarantined insight is HIDDEN — not in pool, not injected.
    training = retrieve(
        env.store, query_vector=QUERY_A, params=params(10_000), mode="training"
    )
    assert quar not in training.pool_insight_ids
    assert quar not in training.insights
    assert active in training.insights  # the active one is still reachable

    # trial for its own batch: the quarantined insight becomes VISIBLE.
    trial = retrieve(
        env.store, query_vector=QUERY_A, params=params(10_000),
        mode="trial", batch_id=batch_id,
    )
    assert quar in trial.pool_insight_ids
    assert quar in trial.insights


def test_quarantine_other_batch_invisible_in_trial(env):
    # A trial for batch b1 must not surface batch b2's quarantined insight.
    other = _insert_insight(env, vector=NEAR_A, status="quarantined", batch="b2")
    b1 = env.store.ensure_batch("b1")
    trial = retrieve(
        env.store, query_vector=QUERY_A, params=params(10_000),
        mode="trial", batch_id=b1,
    )
    assert other not in trial.pool_insight_ids
    assert other not in trial.insights


# === MUST-test: boundary-ticket subsystem deleted ===========================


def test_boundary_ticket_subsystem_deleted():
    for name in (
        "classify_boundary",
        "run_boundary_ticket",
        "injected_agent_clusters",
        "BoundaryDecision",
        "BoundaryRefinementResult",
        "BoundaryPass",
        "Persona",
        "SkillCandidate",  # the old whole-skill candidate shape is gone too
    ):
        assert not hasattr(retrieval_module, name), f"{name} should be deleted (R13)"
    with pytest.raises(ImportError):
        from agent_families.library.retrieval import (  # noqa: F401
            run_boundary_ticket,
        )


# === supporting: whole-store reach ==========================================


def test_retrieval_reaches_whole_store_not_family_pool(env):
    # Insights scattered across two families; none share the query's family.
    fam2 = env.store.create_family("other")
    ag2 = env.store.create_agent(fam2, "g")
    s1 = make_skill(env, "s1", agent_id=env.agent_id)
    s2 = make_skill(env, "s2", agent_id=ag2)
    i1 = _insert_insight(env, vector=NEAR_A, skill_id=s1)
    i2 = _insert_insight(env, vector=NEAR_A, skill_id=s2)
    # An unattached insight (no skill at all) is reachable too.
    i3 = _insert_insight(env, vector=NEAR_A, skill_id=None)

    result = retrieve(env.store, query_vector=QUERY_A, params=params(10_000))
    assert {i1, i2, i3} <= set(result.insights)
    assert {i1, i2, i3} <= result.pool_insight_ids


# === supporting: budget caps injection, whole insights only =================


def test_budget_caps_injection(env):
    skill = make_skill(env, "skill")
    for _ in range(10):
        _insert_insight(env, vector=NEAR_A, skill_id=skill)

    probe = retrieve(env.store, query_vector=QUERY_A, params=params(10_000))
    per_insight = probe.candidates[0].token_count
    assert per_insight >= 3
    budget = 3 * per_insight + 1

    result = retrieve(env.store, query_vector=QUERY_A, params=params(budget))
    # Bloat invariant: total injected tokens never exceed the budget.
    assert result.injected_token_count <= budget
    assert len(result.candidates) == 10
    assert len(result.insights) <= 3
    assert result.drops  # the overflow is dropped, not truncated

    # Every injected insight is WHOLE: its full rendered bytes appear intact.
    included = [c for c in result.candidates if c.insight_id in result.insights]
    for cand in included:
        assert cand.content in result.injected_bytes
    assert b"\n".join(c.content for c in included) == result.injected_bytes
    assert all(d.reason == DROP_BUDGET for d in result.drops)
    assert all(d.rank > 0 and d.token_count > 0 for d in result.drops)


# === supporting: relevance gate is self-focusing ============================


def test_relevance_is_self_focusing(env):
    a = [_insert_insight(env, vector=NEAR_A) for _ in range(3)]
    b = [_insert_insight(env, vector=CLUSTER_B) for _ in range(3)]

    result = retrieve(
        env.store, query_vector=QUERY_A, params=params(10_000, floor=0.5)
    )
    # Zero cluster-B insights injected — they scored below the floor.
    assert set(result.insights) == set(a)
    assert not (set(result.insights) & set(b))
    # Cluster-B insights are visible (in the pool) but gated out of the injection.
    for iid in b:
        assert iid in result.pool_insight_ids
    gated = {d.insight_id for d in result.drops if d.reason == DROP_RELEVANCE_GATE}
    assert gated == set(b)


# === supporting: byte stability =============================================


def test_injection_byte_stable(env):
    skill = make_skill(env, "skill")
    for _ in range(4):
        _insert_insight(env, vector=NEAR_A, skill_id=skill)

    first = retrieve(env.store, query_vector=QUERY_A, params=params(10_000))
    second = retrieve(env.store, query_vector=QUERY_A, params=params(10_000))
    assert first.injected_bytes == second.injected_bytes
    assert first.insights == second.insights
    assert first.injected_bytes  # non-empty (the section actually rendered)


# === supporting: derived skills provenance (the U6 back-compat seam) =========


def test_skills_is_derived_provenance_of_retrieved_insights(env):
    skill = make_skill(env, "elicitation")
    iid = _insert_insight(env, vector=NEAR_A, skill_id=skill)
    result = retrieve(env.store, query_vector=QUERY_A, params=params(10_000))
    assert iid in result.insights
    # The derived provenance names the owning skill of the retrieved insight.
    assert skill in result.skills
    # And the rendered section carries the skill name as provenance + the body.
    assert "Skill: elicitation" in result.injected_text
    assert "action 1".rstrip() in result.injected_text  # the insight body verbatim
    # Nothing retrieved → empty provenance.
    empty = retrieve(env.store, query_vector=CLUSTER_B, params=params(10_000, floor=0.9))
    assert empty.insights == ()
    assert empty.skills == ()


# === guards =================================================================


def test_trial_requires_batch(env):
    with pytest.raises(RetrievalError, match="trial-mode retrieval requires"):
        retrieve(env.store, query_vector=QUERY_A, params=params(10_000), mode="trial")


def test_unknown_mode_rejected(env):
    with pytest.raises(RetrievalError, match="unknown run mode"):
        retrieve(env.store, query_vector=QUERY_A, params=params(10_000), mode="nonsense")


def test_params_validate_ranges():
    with pytest.raises(RetrievalError, match="budget_tokens"):
        RetrievalParams(budget_tokens=0, relevance_floor=0.5)
    with pytest.raises(RetrievalError, match="relevance_floor"):
        RetrievalParams(budget_tokens=1, relevance_floor=1.5)


# === the prompt-assembly seam ===============================================


def test_prompt_injection_seam_round_trips(env):
    skill = make_skill(env, "elicitation")
    _insert_insight(env, vector=NEAR_A, skill_id=skill)
    result = retrieve(env.store, query_vector=QUERY_A, params=params(10_000))
    section = render_injection_section(result)
    assert "Skill: elicitation" in section
    assert "Insight" in section

    ticket = {
        "id": "TKT-A", "title": "login", "description": "build login",
        "files": ["src/login.ts"],
        "acceptance_criteria": [{"id": "AC-1", "text": "user can log in"}],
    }
    ledger = "(no prior iterations)"
    injected = build_worker_prompt(ticket, ledger, injected_skills=section)
    assert "Skill: elicitation" in injected
    # The seam is inert by default — empty injection reproduces the bare prompt.
    assert build_worker_prompt(ticket, ledger, injected_skills="") == (
        build_worker_prompt(ticket, ledger)
    )


def test_empty_store_injects_nothing(env):
    result = retrieve(env.store, query_vector=QUERY_A, params=params(10_000))
    assert result.insights == ()
    assert result.injected_bytes == b""
    assert result.skills == ()
    assert render_injection_section(result) == ""


# === R2 query builders ======================================================


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
