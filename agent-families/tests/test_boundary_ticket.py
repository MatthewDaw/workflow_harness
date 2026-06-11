"""Boundary-ticket retrieval + multi-persona refinement (plan-005 U4, R14b/R14c).

Fully offline: vectors are hand-written (the U5 registration write shape), the
persona draft/refine/verify calls are injected seams, and no embedder / judge /
subprocess runs. Zero quota, no ``claude`` on PATH.

## Conformance — the U4 "Required acceptance tests for R14b/R14c"

Each named invariant maps 1:1 to a behavioral test here (the EXACT names the plan
mandates; none weakened, renamed, skipped, or made to pass trivially):

R14b — family-pool retrieval with own-skills prior (in retrieval.retrieve):
- ``test_boundary_retrieval_crosses_clusters`` — a boundary query reaches BOTH
  clusters (≥1 from A and ≥1 from B) within the budget (ownership ≠ reachability,
  no bloat).
- ``test_indomain_retrieval_stays_in_cluster`` — an in-domain-A query returns ZERO
  cluster-B insights (the specialist stays sharp; the fallback is dormant).
- ``test_prior_dial_monotone`` — raising the own-skills budget share STRICTLY
  reduces the count of cross-cluster (fallback) insights retrieved for a fixed
  boundary query (the dial is real and tightenable).

R14c — boundary-ticket multi-persona refinement (retrieval.run_boundary_ticket):
- ``test_boundary_trigger_is_gated`` — a non-boundary ticket runs exactly one
  persona pass; only a cluster-spanning (or ambiguous) ticket enters the
  multi-persona path (rare by construction).
- ``test_boundary_pass_cap`` — the multi-persona loop performs ≤2 cross-persona
  passes, never more, under any fixture (the hard terminator).
- ``test_boundary_oscillation_halts`` — an oscillation fixture terminates via the
  cap + §7 no-progress tripwire within bounded passes; it does not loop.
- ``test_boundary_is_artifact_mediated`` — the second persona's input is the
  COMMITTED artifact from the first persona's pass, and no persona receives
  another's hidden reasoning/context (artifact-mediation, not context-relay).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_families.library.retrieval import (
    DEFAULT_OWN_SKILLS_SHARE,
    Persona,
    RetrievalParams,
    classify_boundary,
    injected_agent_clusters,
    retrieve,
    run_boundary_ticket,
)
from agent_families.store import Store
from agent_families.vecindex import VecIndex

DIM = 4
A_DIR = [1.0, 0.0, 0.0, 0.0]
B_DIR = [0.0, 1.0, 0.0, 0.0]
QUERY_A = [1.0, 0.0, 0.0, 0.0]              # in-domain A
QUERY_BOUNDARY = [0.7071, 0.7071, 0.0, 0.0]  # ~0.707 cosine to BOTH A and B


@pytest.fixture
def env(tmp_path):
    """A family with two split agents A and B, each owning a disjoint skill cluster."""
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    family_id = store.create_family("worker")
    agent_a = store.create_agent(family_id, "A", description="cluster A specialist")
    agent_b = store.create_agent(family_id, "B", description="cluster B specialist")
    yield SimpleNamespace(
        store=store, vec=vec, family_id=family_id,
        agent_a=agent_a, agent_b=agent_b, counter=0,
    )
    store.close()


def _member(env, skill_id, *, vector):
    """Insert insight + vec row + membership; promote to active (U5 write shape)."""
    env.counter += 1
    n = env.counter
    with env.store.transaction():
        iid = env.store.insert_insight(
            precondition=f"pre {n}", action=f"act {n}",
            expected_outcome=f"out {n}", content_hash=f"hash-{n}",
            status="quarantined",
        )
        env.vec.insert(iid, vector)
        env.store.append_member(skill_id, iid)
    with env.store.queue_operation("promote", f"seed {n}") as snap:
        env.store.set_status(iid, "active", snap)
    return iid


def _skill(env, agent_id, name):
    return env.store.create_skill(agent_id, name, f"{name} description")


def _seed_two_clusters(env, *, n_a=6, n_b=6):
    """``n_a`` A-skills (cluster A) + ``n_b`` B-skills (cluster B), one member each."""
    a_skills, b_skills = [], []
    for i in range(n_a):
        sid = _skill(env, env.agent_a, f"a-{i}")
        _member(env, sid, vector=[1.0, 0.0, 0.0, i * 0.001])
        a_skills.append(sid)
    for i in range(n_b):
        sid = _skill(env, env.agent_b, f"b-{i}")
        _member(env, sid, vector=[0.0, 1.0, 0.0, i * 0.001])
        b_skills.append(sid)
    return a_skills, b_skills


def _params(budget, *, floor=0.5, own_share=DEFAULT_OWN_SKILLS_SHARE):
    return RetrievalParams(
        budget_tokens=budget, relevance_floor=floor, own_skills_share=own_share
    )


def _injected_by_agent(env, result):
    clusters = injected_agent_clusters(env.store, result)
    return clusters.get(env.agent_a, ()), clusters.get(env.agent_b, ())


# === R14b: family-pool retrieval with own-skills prior =========================


def test_boundary_retrieval_crosses_clusters(env):
    """A boundary query reaches BOTH clusters within the budget (ownership is not
    reachability) — and never exceeds the token budget."""
    a_skills, b_skills = _seed_two_clusters(env)
    budget = 100_000  # generous: both clusters fit
    result = retrieve(
        env.store, query_vector=QUERY_BOUNDARY, family_id=env.family_id,
        working_agent_id=env.agent_a, params=_params(budget),
    )
    from_a, from_b = _injected_by_agent(env, result)
    # ≥1 insight from A AND ≥1 from B (own + relevance-gated sibling fallback).
    assert len(from_a) >= 1
    assert len(from_b) >= 1
    # No bloat: injected tokens stay within the budget (the R3 invariant).
    assert result.injected_token_count <= budget


def test_indomain_retrieval_stays_in_cluster(env):
    """An in-domain-A query surfaces ZERO cluster-B insights — the specialist stays
    sharp and the cross-cluster fallback is dormant."""
    _seed_two_clusters(env)
    result = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        working_agent_id=env.agent_a, params=_params(100_000, floor=0.5),
    )
    from_a, from_b = _injected_by_agent(env, result)
    assert len(from_a) >= 1            # the in-domain cluster is reached
    assert from_b == ()               # the other cluster is gated out entirely


def test_prior_dial_monotone(env):
    """Raising the own-skills budget share STRICTLY reduces the count of
    cross-cluster (fallback) insights for a fixed boundary query."""
    _seed_two_clusters(env, n_a=6, n_b=6)
    # Equal per-skill rendered size lets us size the budget to bind the dial.
    probe = retrieve(
        env.store, query_vector=QUERY_BOUNDARY, family_id=env.family_id,
        working_agent_id=env.agent_a, params=_params(1_000_000),
    )
    sizes = {c.token_count for c in probe.candidates}
    assert len(sizes) == 1, f"skills must be equal-sized for the dial test: {sizes}"
    per = sizes.pop()
    budget = 6 * per  # fits ~6 whole skills total; the dial splits them own/sibling

    def fallback_count(own_share):
        result = retrieve(
            env.store, query_vector=QUERY_BOUNDARY, family_id=env.family_id,
            working_agent_id=env.agent_a, params=_params(budget, own_share=own_share),
        )
        _, from_b = _injected_by_agent(env, result)
        return len(from_b)

    low = fallback_count(0.2)
    mid = fallback_count(0.5)
    high = fallback_count(0.9)
    # Strictly monotone decreasing as the own-skills prior tightens.
    assert low > mid > high
    assert high >= 0


# === R14c: boundary-ticket multi-persona refinement ============================


def _boundary_result(env):
    """A retrieval whose injected skills span BOTH agents (a boundary ticket).

    Assumes the env is already seeded (one ``_seed_two_clusters`` per test)."""
    result = retrieve(
        env.store, query_vector=QUERY_BOUNDARY, family_id=env.family_id,
        working_agent_id=env.agent_a, params=_params(100_000, floor=0.5),
    )
    from_a, from_b = _injected_by_agent(env, result)
    assert from_a and from_b  # precondition: it really is a boundary ticket
    return result


def _indomain_result(env):
    """A retrieval confined to agent A (a non-boundary ticket). Env pre-seeded."""
    result = retrieve(
        env.store, query_vector=QUERY_A, family_id=env.family_id,
        working_agent_id=env.agent_a, params=_params(100_000, floor=0.5),
    )
    _, from_b = _injected_by_agent(env, result)
    assert from_b == ()
    return result


def test_boundary_trigger_is_gated(env):
    """A non-boundary ticket runs exactly ONE persona pass; only a cluster-spanning
    ticket enters the multi-persona path."""
    _seed_two_clusters(env)
    primary = Persona(env.agent_a, "A")
    secondary = Persona(env.agent_b, "B")

    # Non-boundary: one persona pass, no cross-persona refinement.
    nb = run_boundary_ticket(
        env.store, _indomain_result(env),
        primary=primary, secondaries=[secondary],
        draft_fn=lambda p: "draft", refine_fn=lambda p, art: "refined",
        verifier_fn=lambda art: False,
    )
    assert nb.decision.is_boundary is False
    assert nb.halt_reason == "single_persona"
    assert nb.cross_persona_passes == 0
    assert len(nb.passes) == 1

    # Boundary: enters the multi-persona path.
    b = run_boundary_ticket(
        env.store, _boundary_result(env),
        primary=primary, secondaries=[secondary],
        draft_fn=lambda p: "draft", refine_fn=lambda p, art: "refined",
        verifier_fn=lambda art: art == "refined",
    )
    assert b.decision.is_boundary is True
    assert b.cross_persona_passes == 1
    assert b.accepted is True


def test_boundary_ambiguous_routing_triggers_even_within_one_cluster(env):
    """Routing ambiguity alone makes a single-cluster ticket a boundary ticket."""
    _seed_two_clusters(env)
    result = _indomain_result(env)  # injected skills all from A
    decision = classify_boundary(
        env.store, result, routing_ambiguous=True, primary_agent_id=env.agent_a
    )
    assert decision.is_boundary is True
    assert decision.reason == "routing_ambiguous"


def test_boundary_pass_cap(env):
    """The multi-persona loop performs ≤2 cross-persona passes, never more."""
    _seed_two_clusters(env)
    primary = Persona(env.agent_a, "A")
    # Three eager secondaries, each producing a NEW artifact, verifier never passes.
    secondaries = [Persona(env.agent_b, "B"), Persona(env.agent_a, "A2"),
                   Persona(env.agent_b, "B2")]
    counter = {"n": 0}

    def refine(persona, art):
        counter["n"] += 1
        return f"{art}|{counter['n']}"  # always new -> no oscillation halt

    result = run_boundary_ticket(
        env.store, _boundary_result(env),
        primary=primary, secondaries=secondaries,
        draft_fn=lambda p: "D0", refine_fn=refine, verifier_fn=lambda art: False,
        max_cross_persona_passes=2,
    )
    assert result.cross_persona_passes == 2  # capped, never 3
    assert result.halt_reason == "pass_cap"
    assert result.accepted is False
    # The cap also bounds the number of cross-persona pass records.
    cross = [p for p in result.passes if p.is_cross_persona]
    assert len(cross) == 2


def test_boundary_oscillation_halts(env):
    """An A/B opposing-edit oscillation halts via the cap + §7 no-progress tripwire
    within bounded passes — it does not loop."""
    _seed_two_clusters(env)
    primary = Persona(env.agent_a, "A")
    b = Persona(env.agent_b, "B")
    a2 = Persona(env.agent_a, "A")
    secondaries = [b, a2, b, a2]  # would oscillate forever without a terminator

    def refine(persona, art):
        # B appends a marker; A removes it -> the artifact toggles base <-> base|B.
        if persona.name == "B":
            return art + "|B"
        return art.rsplit("|B", 1)[0]

    result = run_boundary_ticket(
        env.store, _boundary_result(env),
        primary=primary, secondaries=secondaries,
        draft_fn=lambda p: "base", refine_fn=refine, verifier_fn=lambda art: False,
        max_cross_persona_passes=2,
    )
    assert result.accepted is False
    assert result.cross_persona_passes <= 2          # bounded
    assert result.halt_reason == "no_progress"        # the §7 tripwire fired
    # pass 1: base -> base|B (new); pass 2: base|B -> base (a SEEN state) -> halt.
    assert result.final_artifact == "base"


def test_boundary_is_artifact_mediated(env):
    """The second persona's input is the COMMITTED artifact from the first pass, and
    no persona receives another's hidden reasoning/context."""
    _seed_two_clusters(env)
    primary = Persona(env.agent_a, "A")
    secondary = Persona(env.agent_b, "B")
    received = []

    def refine(persona, art):
        # All a persona ever gets is (its identity, the committed artifact string) —
        # never another persona's reasoning. Record exactly what was passed in.
        received.append((persona.name, art))
        return art + " + B-edit"

    result = run_boundary_ticket(
        env.store, _boundary_result(env),
        primary=primary, secondaries=[secondary],
        draft_fn=lambda p: "PRIMARY-DRAFT", refine_fn=refine,
        # Reject the bare draft; accept only once B has refined it (so the
        # secondary pass actually runs and we can audit its input).
        verifier_fn=lambda art: art.endswith("B-edit"),
    )
    # The committed primary draft is exactly what the secondary received.
    assert result.passes[0].output_artifact == "PRIMARY-DRAFT"
    assert result.passes[1].input_artifact == "PRIMARY-DRAFT"
    assert received == [("B", "PRIMARY-DRAFT")]
    # The audit row confirms artifact-mediation: input == prior committed output.
    assert result.passes[1].input_artifact == result.passes[0].output_artifact
    assert result.accepted is True
