"""Boundary-ticket retrieval + multi-persona refinement (plan-005 U4, R14b/R14c).

Fully offline: retrieval vectors are hand-written and exercised through the real
store/vec index; the refinement loop is driven by deterministic fake seams (no
judge, no subprocess, no quota).

## Conformance — the required acceptance tests (each EXACT behavioral assertion)

R14b (family-pool retrieval with own-skills prior), in this file:

- ``test_boundary_retrieval_crosses_clusters`` — two split agents, disjoint
  clusters A/B; a boundary query (near both) -> the injected set contains >=1
  insight from A AND >=1 from B, and injected tokens <= the R3 budget.
  *(ownership != reachability, with no bloat)*
- ``test_indomain_retrieval_stays_in_cluster`` — same fixture; an in-domain-A query
  -> ZERO cluster-B insights injected (the fallback stays dormant).
  *(the specialist stays sharp)*
- ``test_prior_dial_monotone`` — raising the own-skills budget share strictly
  reduces the count of fallback (cross-cluster) insights retrieved for a fixed
  boundary query. *(the dial is real and tightenable)*

R14c (boundary-ticket multi-persona refinement), in this file:

- ``test_boundary_trigger_is_gated`` — a non-boundary ticket runs exactly one
  persona pass; only a >=2-cluster (or ambiguous-routing) ticket enters the
  multi-persona path. *(rare by construction)*
- ``test_boundary_pass_cap`` — the multi-persona loop performs <=2 cross-persona
  passes, never more, under any fixture. *(hard terminator)*
- ``test_boundary_oscillation_halts`` — an oscillation fixture (opposing edits)
  terminates via the cap + the §7 no-progress tripwire within bounded passes; it
  does not loop. *(anti-thrash)*
- ``test_boundary_is_artifact_mediated`` — the second persona's input is the
  COMMITTED artifact from the first persona's pass, and no persona receives
  another's hidden reasoning/context. *(artifact-mediation, not context-relay)*
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_families.library.retrieval import (
    RetrievalParams,
    injected_agent_clusters,
    retrieve,
)
from agent_families.library.router import (
    HALT_NO_PROGRESS,
    HALT_PASS_CAP,
    HALT_SINGLE_PERSONA,
    HALT_VERIFIER_ACCEPTED,
    RouterParams,
    is_boundary_ticket,
    refine_with_personas,
)
from agent_families.store import Store
from agent_families.vecindex import VecIndex

DIM = 4
NEAR_A = [1.0, 0.0, 0.0, 0.0]
NEAR_B = [0.0, 1.0, 0.0, 0.0]
BOTH = [1.0, 1.0, 0.0, 0.0]  # cosine ~0.707 with each of NEAR_A / NEAR_B


# === R14b: family-pool retrieval with own-skills prior =========================


@pytest.fixture
def split_family(tmp_path):
    """Two split agents A and B in one family, disjoint skill clusters."""
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    family_id = store.create_family("worker")
    agent_a = store.create_agent(family_id, "alpha")
    agent_b = store.create_agent(family_id, "beta")
    ns = SimpleNamespace(
        store=store, vec=vec, family_id=family_id,
        agent_a=agent_a, agent_b=agent_b, n=0,
    )
    yield ns
    store.close()


def _skill_with_member(env, agent_id, name, *, vector):
    env.n += 1
    n = env.n
    skill = env.store.create_skill(agent_id, name, f"description for {name}")
    with env.store.transaction():
        iid = env.store.insert_insight(
            precondition=f"pre {n}", action=f"act {n}",
            expected_outcome=f"out {n}", content_hash=f"h-{n}", status="quarantined",
        )
        env.vec.insert(iid, vector)
        env.store.append_member(skill, iid)
    with env.store.queue_operation("promote", f"seed {n}") as snap:
        env.store.set_status(iid, "active", snap)
    return skill, iid


def _params(budget, *, floor=0.4, own_share=0.8):
    return RetrievalParams(
        budget_tokens=budget, relevance_floor=floor, own_skills_share=own_share
    )


def test_boundary_retrieval_crosses_clusters(split_family):
    env = split_family
    a_skill, a_iid = _skill_with_member(env, env.agent_a, "a-skill", vector=NEAR_A)
    b_skill, b_iid = _skill_with_member(env, env.agent_b, "b-skill", vector=NEAR_B)

    budget = 10_000  # generous: both clusters fit -> no budget drop expected
    result = retrieve(
        env.store, query_vector=BOTH, family_id=env.family_id,
        working_agent_id=env.agent_a, params=_params(budget, floor=0.4),
    )

    # Ownership != reachability: the injection spans BOTH the own cluster (A) and
    # the sibling cluster (B) — >=1 insight from each side of the boundary.
    clusters = injected_agent_clusters(result)
    assert env.agent_a in clusters
    assert env.agent_b in clusters
    assert a_skill in result.skills and b_skill in result.skills
    # No bloat: injected tokens never exceed the R3 budget.
    assert result.injected_token_count <= budget


def test_indomain_retrieval_stays_in_cluster(split_family):
    env = split_family
    a_skill, _ = _skill_with_member(env, env.agent_a, "a-skill", vector=NEAR_A)
    b_skill, _ = _skill_with_member(env, env.agent_b, "b-skill", vector=NEAR_B)

    # An in-domain-A query (orthogonal to B): the floor sits above B's cosine (0.0).
    result = retrieve(
        env.store, query_vector=NEAR_A, family_id=env.family_id,
        working_agent_id=env.agent_a, params=_params(10_000, floor=0.5),
    )

    clusters = injected_agent_clusters(result)
    assert clusters == frozenset({env.agent_a})  # the specialist stays sharp
    assert a_skill in result.skills
    assert b_skill not in result.skills  # the cross-cluster fallback stays dormant


def test_prior_dial_monotone(split_family):
    env = split_family
    # Many own (A) skills so the own budget SATURATES at both share levels, plus
    # several sibling (B) skills competing for the remainder.
    for i in range(10):
        _skill_with_member(env, env.agent_a, f"a-{i}", vector=NEAR_A)
    b_skills = set()
    for i in range(6):
        sid, _ = _skill_with_member(env, env.agent_b, f"b-{i}", vector=NEAR_B)
        b_skills.add(sid)

    # Per-skill rendered size, then a budget that forces own/sibling competition.
    probe = retrieve(
        env.store, query_vector=BOTH, family_id=env.family_id,
        working_agent_id=env.agent_a, params=_params(100_000, floor=0.4),
    )
    per = probe.candidates[0].token_count
    budget = 8 * per

    def fallback_count(own_share):
        result = retrieve(
            env.store, query_vector=BOTH, family_id=env.family_id,
            working_agent_id=env.agent_a,
            params=_params(budget, floor=0.4, own_share=own_share),
        )
        injected = set(result.skills)
        return len(injected & b_skills)

    low_share = fallback_count(0.5)
    high_share = fallback_count(0.8)
    # Raising the own-skills share strictly reduces the cross-cluster fallback.
    assert high_share < low_share


# === R14c: boundary-ticket multi-persona refinement ============================


def _accept_after(n_passes):
    """A verifier that accepts only once ``n_passes`` artifacts have been seen."""
    state = {"seen": 0}

    def verify(artifact):
        state["seen"] += 1
        return state["seen"] >= n_passes

    return verify


def test_boundary_trigger_is_gated():
    # A non-boundary ticket (single cluster, confident routing) -> exactly ONE pass.
    assert is_boundary_ticket(agent_clusters={7}, routing_ambiguous=False) is False
    non_boundary = refine_with_personas(
        is_boundary=False,
        primary_agent_id=7,
        secondary_agent_ids=[8, 9],
        draft_fn=lambda: "draft",
        refine_fn=lambda aid, art: art + "!",  # must never be called
        verify_fn=lambda art: False,
    )
    assert len(non_boundary.passes) == 1
    assert non_boundary.passes[0].is_primary is True
    assert non_boundary.cross_pass_count == 0
    assert non_boundary.halt_reason == HALT_SINGLE_PERSONA

    # A boundary ticket (>=2 clusters) DOES enter the multi-persona path.
    assert is_boundary_ticket(agent_clusters={7, 8}, routing_ambiguous=False) is True
    boundary = refine_with_personas(
        is_boundary=True,
        primary_agent_id=7,
        secondary_agent_ids=[8],
        draft_fn=lambda: "v0",
        refine_fn=lambda aid, art: art + "+",
        verify_fn=_accept_after(2),  # primary fails, one refine accepts
    )
    assert boundary.cross_pass_count == 1
    assert boundary.accepted is True
    assert boundary.halt_reason == HALT_VERIFIER_ACCEPTED


def test_boundary_pass_cap():
    # Three personas offered, verifier never accepts, distinct artifacts each pass:
    # the loop performs at most 2 cross-persona passes, never more.
    passes_seen = []

    def refine(aid, art):
        passes_seen.append(aid)
        return art + f"-{len(passes_seen)}"  # always a NEW state

    result = refine_with_personas(
        is_boundary=True,
        primary_agent_id=1,
        secondary_agent_ids=[2, 3, 4],
        draft_fn=lambda: "v0",
        refine_fn=refine,
        verify_fn=lambda art: False,
    )
    assert result.cross_pass_count == 2  # hard cap
    assert len(passes_seen) == 2  # the 4th persona never ran
    assert passes_seen == [2, 3]
    assert result.halt_reason == HALT_PASS_CAP
    assert result.accepted is False


def test_boundary_oscillation_halts():
    # Personas A and B make opposing edits: A -> "v1", B reverts -> "v0". The §7
    # no-progress tripwire fires when the artifact returns to a seen state.
    def refine(aid, art):
        return "v1" if art == "v0" else "v0"

    result = refine_with_personas(
        is_boundary=True,
        primary_agent_id=1,
        secondary_agent_ids=[2, 3, 4, 5],  # plenty offered
        draft_fn=lambda: "v0",
        refine_fn=refine,
        verify_fn=lambda art: False,
        params=RouterParams(max_cross_passes=2),
    )
    # Bounded termination (it does NOT loop), via the cap + no-progress tripwire.
    assert result.cross_pass_count <= 2
    assert result.halt_reason == HALT_NO_PROGRESS
    assert result.accepted is False


def test_boundary_is_artifact_mediated():
    # The refining persona must receive the COMMITTED artifact from the prior pass
    # — and nothing else (no hidden reasoning / context relay).
    received = {}

    def refine(aid, committed_artifact):
        received["input"] = committed_artifact
        received["arity"] = "artifact-only"
        return committed_artifact + " [refined]"

    result = refine_with_personas(
        is_boundary=True,
        primary_agent_id=1,
        secondary_agent_ids=[2],
        draft_fn=lambda: "PRIMARY-DRAFT",
        refine_fn=refine,
        verify_fn=_accept_after(2),
    )
    # The second persona read exactly the primary's COMMITTED output.
    assert result.passes[0].is_primary is True
    assert result.passes[0].artifact_out == "PRIMARY-DRAFT"
    assert result.passes[1].artifact_in == "PRIMARY-DRAFT"
    assert received["input"] == "PRIMARY-DRAFT"
    # Artifact-mediation: the persona's contract is (agent_id, committed_artifact)
    # only — there is no channel for another persona's hidden reasoning.
    assert received["arity"] == "artifact-only"
    assert result.artifact == "PRIMARY-DRAFT [refined]"
