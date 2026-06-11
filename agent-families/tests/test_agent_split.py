"""Agent split (the family self-reorganization) tests (plan-005 U4, R13, DESIGN §6).

Fully offline: skill-description vectors are hand-written (the run-assembly embeds
them live), the namer / routing-replay / benchmark gates are injected seams, and the
lineage mutations drive the real store. Zero quota, no ``claude`` on PATH. Routing
replay runs the REAL router primitive over the post-split candidate set, driven by a
scripted judge fake.

## Conformance

plan-005 U4 split test scenario -> test:

- split candidacy refuses below the decision-count gate:
  ``test_split_candidacy_refuses_below_decision_gate``
- two planted skill clusters split with ≥90% replay agreement:
  ``test_planted_clusters_split_with_replay_agreement``
- replay below threshold rejects and preserves the parent:
  ``test_replay_below_threshold_rejects_and_preserves_parent``
- a reverted split restores routing identically:
  ``test_reverted_split_restores_routing_identically``
- base-prompt residue blocks the split with the conversion warning:
  ``test_base_prompt_residue_blocks_split``
- children inherit correct skill partitions including quarantined members:
  ``test_children_inherit_skill_partitions_including_quarantined``
- one fixture split survives the full transaction incl. a benchmark-pass gate:
  ``test_split_survives_full_transaction_with_benchmark_gate``
- a failed benchmark gate reverts the split:
  ``test_failed_benchmark_gate_reverts_split``
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_families.library import router
from agent_families.reflector import agent_split as a
from agent_families.store import Store
from agent_families.vecindex import VecIndex

DIM = 4
TAG_DIR = [1.0, 0.0, 0.0, 0.0]
SEARCH_DIR = [0.0, 1.0, 0.0, 0.0]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    VecIndex(s, DIM).migrate()
    try:
        yield s
    finally:
        s.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _FakeResult:
    def __init__(self, output: dict) -> None:
        self.output = output


def make_judge(choose):
    """A routing judge fake: ``choose(request_text) -> chosen_candidate_name``.

    Reads the request text out of the routing prompt (which embeds it under a
    ``## Request`` heading) and returns the candidate name to route to.
    """

    def judge(prompt, schema, model, *, max_retries, mode=None, fixtures_dir=None):
        request_text = prompt.split("## Request\n", 1)[1].split("\n\n", 1)[0]
        name = choose(request_text)
        return _FakeResult({"chosen_agent": name, "confidence": 0.99})

    return judge


_HASH_SEQ = [0]


def _make_skill(store, agent_id, name, *, quarantined_member=False):
    _HASH_SEQ[0] += 1
    h = _HASH_SEQ[0]
    sid = store.create_skill(agent_id, name, f"{name} description")
    with store.transaction():
        iid = store.insert_insight(
            precondition=f"pre {name}", action=f"act {name}",
            expected_outcome=f"out {name}", content_hash=f"hash-{name}-{h}",
            status="quarantined" if quarantined_member else "active",
        )
        store.append_member(sid, iid)
    return sid, iid


def _log_decision(store, family_id, parent_id, request_text):
    router.ensure_routing_log(store)
    store.conn.execute(
        "INSERT INTO routing_decisions (family_id, request_hash, request_text,"
        " candidates_json, chosen_agent_id, confidence, ambiguous, snapshot_id,"
        " created_at) VALUES (?, ?, ?, '[]', ?, 1.0, 0, 0, ?)",
        (family_id, "h", request_text, parent_id, _utcnow()),
    )


_FAM_SEQ = [0]


def make_fixture(store, *, with_sibling=False, decisions=10, routing_decisions=60,
                 base_prompt_specialty="", quarantined_member=False):
    """A generic agent with two skill clusters (tag/search), its decision log, and
    description vectors. Returns a dict of ids + the skill_vectors map.

    The family name is unique per call (so a test may build two fixtures) while the
    agent is always named ``worker`` (children become ``worker-1`` / ``worker-2``)."""
    _FAM_SEQ[0] += 1
    fam = store.create_family(f"worker-fam-{_FAM_SEQ[0]}", charter="builds the app")
    parent = store.create_agent(
        fam, "worker", description="generalist",
        base_prompt_specialty=base_prompt_specialty,
    )
    store.conn.execute(
        "UPDATE agents SET routing_decisions = ? WHERE id = ?",
        (routing_decisions, parent),
    )
    skill_vectors = {}
    tag_skills, search_skills = [], []
    quarantined_iid = None
    for i in range(3):
        q = quarantined_member and i == 0
        sid, iid = _make_skill(store, parent, f"tag-{i}", quarantined_member=q)
        skill_vectors[sid] = [1.0, 0.0, 0.0, i * 0.001]
        tag_skills.append(sid)
        if q:
            quarantined_iid = iid
    for i in range(3):
        sid, _ = _make_skill(store, parent, f"search-{i}")
        skill_vectors[sid] = [0.0, 1.0, 0.0, i * 0.001]
        search_skills.append(sid)

    sibling = store.create_agent(fam, "sibling", description="unrelated") \
        if with_sibling else None

    for i in range(decisions):
        text = "tag this bookmark" if i % 2 == 0 else "search the bookmarks"
        _log_decision(store, fam, parent, text)

    return dict(
        fam=fam, parent=parent, sibling=sibling, skill_vectors=skill_vectors,
        tag_skills=tag_skills, search_skills=search_skills,
        quarantined_iid=quarantined_iid,
    )


# --- R13: the decision-count gate ----------------------------------------------


def test_split_candidacy_refuses_below_decision_gate(store):
    fx = make_fixture(store, routing_decisions=10)
    params = a.AgentSplitParams(min_routing_decisions=50)
    candidacy = a.evaluate_split_candidacy(
        store, fx["parent"], skill_vectors=fx["skill_vectors"], params=params
    )
    assert candidacy.eligible is False
    assert "min_routing_decisions" in candidacy.reason
    assert candidacy.routing_decisions == 10


# --- R13: base-prompt residue check --------------------------------------------


def test_base_prompt_residue_blocks_split(store):
    fx = make_fixture(
        store, routing_decisions=60,
        base_prompt_specialty="Always prefer tag-based search over full text.",
    )
    candidacy = a.evaluate_split_candidacy(
        store, fx["parent"], skill_vectors=fx["skill_vectors"]
    )
    assert candidacy.eligible is False
    assert candidacy.residue_warning is not None
    assert "residue" in candidacy.reason
    # A clean base prompt does NOT trigger the residue refusal.
    clean = make_fixture(store, routing_decisions=60)
    assert a.check_base_prompt_residue(
        store.conn.execute(
            "SELECT base_prompt_specialty FROM agents WHERE id = ?",
            (clean["parent"],),
        ).fetchone()
    ) is None


# --- R13: clustering + execute partition (incl. quarantined) -------------------


def test_children_inherit_skill_partitions_including_quarantined(store):
    fx = make_fixture(store, routing_decisions=60, quarantined_member=True)
    candidacy = a.evaluate_split_candidacy(
        store, fx["parent"], skill_vectors=fx["skill_vectors"]
    )
    assert candidacy.eligible is True
    clusters = {frozenset(candidacy.cluster0_skill_ids),
                frozenset(candidacy.cluster1_skill_ids)}
    assert clusters == {frozenset(fx["tag_skills"]), frozenset(fx["search_skills"])}

    split = a.execute_split(store, candidacy)
    child0, child1 = split.child_agent_ids

    def owned(agent_id):
        return {
            r["id"] for r in store.conn.execute(
                "SELECT id FROM skills WHERE agent_id = ?", (agent_id,)
            ).fetchall()
        }

    assert owned(child0) == set(candidacy.cluster0_skill_ids)
    assert owned(child1) == set(candidacy.cluster1_skill_ids)
    assert owned(fx["parent"]) == set()
    assert store.conn.execute(
        "SELECT lineage_status FROM agents WHERE id = ?", (fx["parent"],)
    ).fetchone()["lineage_status"] == a.LINEAGE_SPLIT_PENDING
    # The quarantined member followed its skill (tag-0) to whichever child owns it.
    q_skill = fx["tag_skills"][0]
    owner = store.conn.execute(
        "SELECT agent_id FROM skills WHERE id = ?", (q_skill,)
    ).fetchone()["agent_id"]
    assert owner in (child0, child1)
    assert fx["quarantined_iid"] in store.skill_members(q_skill)


# --- R13: routing replay + the full transaction --------------------------------


def _route_to_either_child(request_text):
    # Both worker-1 and worker-2 are this split's children -> any choice is a hit.
    return "worker-1" if "tag" in request_text else "worker-2"


def test_planted_clusters_split_with_replay_agreement(store):
    fx = make_fixture(store, routing_decisions=60)
    candidacy = a.evaluate_split_candidacy(
        store, fx["parent"], skill_vectors=fx["skill_vectors"]
    )
    assert candidacy.eligible and candidacy.silhouette >= 0.35

    split = a.execute_split(store, candidacy)
    agree = a.replay_agreement(
        store, split, judge_fn=make_judge(_route_to_either_child)
    )
    assert agree >= 0.9  # every logged request re-routes into the lineage
    a.revert_split(store, split)


def test_split_survives_full_transaction_with_benchmark_gate(store):
    fx = make_fixture(store, routing_decisions=60)
    benchmarks = []
    outcome = a.perform_agent_split(
        store, fx["parent"], skill_vectors=fx["skill_vectors"],
        judge_fn=make_judge(_route_to_either_child),
        benchmark_fn=lambda split: benchmarks.append(split) or True,
    )
    assert outcome.committed is True
    assert outcome.replay_score >= 0.9
    assert outcome.benchmark_passed is True
    assert len(benchmarks) == 1  # the benchmark gate ran exactly once
    assert outcome.split.finalized is True
    # Parent retired (frozen anchor, never deleted); children active + parented.
    assert store.conn.execute(
        "SELECT lineage_status FROM agents WHERE id = ?", (fx["parent"],)
    ).fetchone()["lineage_status"] == a.LINEAGE_RETIRED
    for cid in outcome.split.child_agent_ids:
        row = store.conn.execute(
            "SELECT parent_id, lineage_status FROM agents WHERE id = ?", (cid,)
        ).fetchone()
        assert row["parent_id"] == fx["parent"]
        assert row["lineage_status"] is None
    assert {c.name for c in router.family_candidates(store, fx["fam"])} == {
        "worker-1", "worker-2"
    }


def test_failed_benchmark_gate_reverts_split(store):
    fx = make_fixture(store, routing_decisions=60)
    outcome = a.perform_agent_split(
        store, fx["parent"], skill_vectors=fx["skill_vectors"],
        judge_fn=make_judge(_route_to_either_child),
        benchmark_fn=lambda split: False,  # replay passes, benchmark fails
    )
    assert outcome.committed is False
    assert outcome.replay_score >= 0.9
    assert outcome.benchmark_passed is False
    # Parent preserved active; no children; all six skills restored.
    assert store.conn.execute(
        "SELECT lineage_status FROM agents WHERE id = ?", (fx["parent"],)
    ).fetchone()["lineage_status"] is None
    assert _agent_count(store, fx["fam"]) == 1
    assert _owned_count(store, fx["parent"]) == 6


def test_replay_below_threshold_rejects_and_preserves_parent(store):
    fx = make_fixture(store, with_sibling=True, routing_decisions=60)

    def leaky(request_text):
        # "search" requests leak to the sibling -> agreement ~0.5 < 0.9.
        return "sibling" if "search" in request_text else "worker-1"

    outcome = a.perform_agent_split(
        store, fx["parent"], skill_vectors=fx["skill_vectors"],
        judge_fn=make_judge(leaky),
        benchmark_fn=lambda split: True,
    )
    assert outcome.committed is False
    assert outcome.replay_score is not None and outcome.replay_score < 0.9
    assert outcome.benchmark_passed is None  # never reached the benchmark gate
    # Parent preserved (active); children gone; skills restored; sibling untouched.
    assert store.conn.execute(
        "SELECT lineage_status FROM agents WHERE id = ?", (fx["parent"],)
    ).fetchone()["lineage_status"] is None
    assert _owned_count(store, fx["parent"]) == 6
    assert _agent_count(store, fx["fam"]) == 2  # parent + sibling, no children


def test_reverted_split_restores_routing_identically(store):
    fx = make_fixture(store, routing_decisions=60)
    before = [(c.agent_id, c.name) for c in router.family_candidates(store, fx["fam"])]

    candidacy = a.evaluate_split_candidacy(
        store, fx["parent"], skill_vectors=fx["skill_vectors"]
    )
    split = a.execute_split(store, candidacy)
    # Mid-split the candidate set has changed (children replace the parent).
    assert {c.name for c in router.family_candidates(store, fx["fam"])} == {
        "worker-1", "worker-2"
    }
    a.revert_split(store, split)

    after = [(c.agent_id, c.name) for c in router.family_candidates(store, fx["fam"])]
    assert after == before
    assert _owned_count(store, fx["parent"]) == 6


# --- R14: the explorer-family decision point (documented, not automatic) -------


def _instrument_health(store, episode_id, role, aspect):
    import json as _json

    store.conn.execute(
        "INSERT INTO review_queue (episode_id, kind, payload_json, status,"
        " created_at) VALUES (?, 'instrument_health', ?, 'open', ?)",
        (episode_id, _json.dumps({"primary": {"role": role, "aspect": aspect}}),
         _utcnow()),
    )


def test_explorer_family_evidence_is_advisory(store):
    ep = store.create_episode("linkding", "digest", 0)
    # Below the volume floor -> the sink remains, no seed recommended.
    for _ in range(3):
        _instrument_health(store, ep, "explorer", "prompt_quality")
    ev = a.explorer_family_decision_evidence(store, min_idea_shaped_volume=20)
    assert ev.total_records == 3
    assert ev.by_role["explorer"] == 3
    assert ev.recommend_seed is False
    assert "sink remains" in ev.reason

    # Recurring idea-shaped volume -> the evidence flips to advisory-seed (still a
    # human decision; this function never seeds a family itself).
    for _ in range(25):
        _instrument_health(store, ep, "explorer", "prompt_quality")
    ev2 = a.explorer_family_decision_evidence(store, min_idea_shaped_volume=20)
    assert ev2.by_role_aspect["explorer|prompt_quality"] == 28
    assert ev2.recommend_seed is True


# --- params validation ---------------------------------------------------------


def test_params_range_checks():
    with pytest.raises(a.AgentSplitError):
        a.AgentSplitParams(min_cluster_size=0)
    with pytest.raises(a.AgentSplitError):
        a.AgentSplitParams(replay_agreement_threshold=1.5)
    with pytest.raises(a.AgentSplitError):
        a.AgentSplitParams(compressibility_max_words=0)


# --- helpers -------------------------------------------------------------------


def _agent_count(store, family_id):
    return store.conn.execute(
        "SELECT COUNT(*) AS n FROM agents WHERE family_id = ?", (family_id,)
    ).fetchone()["n"]


def _owned_count(store, agent_id):
    return store.conn.execute(
        "SELECT COUNT(*) AS n FROM skills WHERE agent_id = ?", (agent_id,)
    ).fetchone()["n"]
