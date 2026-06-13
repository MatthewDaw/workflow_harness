"""The derive pass + identity tracking (plan-009 U4): propose → select → name → apply.

Fully offline, zero quota, deterministic — real ``Store`` + ``VecIndex`` over
hand-placed angle vectors forming well-separated clusters, no embedding model, no
``claude``, no native partitioner wheel. The pass builds the v1 similarity flow
graph (U2), proposes candidate partitions (U3), selects the §6a-minimal one (U1),
adopts it only if it strictly beats the incumbent, identity-tracks communities to
prior modules by majority-overlap, lazily names only the changed ones, and applies
the whole partition as a single snapshot-minting ``queue_operation("derive")``.

## Conformance (U4 required acceptance tests → invariant)

| Invariant (plan-009 U4, R8/R9/R10) | Test |
|---|---|
| a no-op pass (selection does not beat incumbent) leaves the active set untouched and mints ZERO snapshots | `test_noop_pass_mints_no_snapshot` |
| two passes over an unchanged library map each community to the SAME module id; ids/names/slugs byte-identical | `test_identity_stable_across_two_passes` |
| a re-derived community with a strict majority of a prior module's ids INHERITS it; a genuinely new community gets a fresh id | `test_majority_overlap_reassigns_on_membership_shift` |
| a non-trivial accepted pass mints EXACTLY ONE snapshot for the whole partition rewrite | `test_accepted_pass_mints_exactly_one_snapshot` |
| only communities whose membership changed (and need a name) are passed to the namer; unchanged → zero namer calls | `test_only_changed_communities_renamed` |

Supporting (guard the API surface, not required):

- the incumbent at cold start is all-singletons and the first grouping is adopted: `test_cold_start_adopts_first_grouping`
"""

from __future__ import annotations

import math

import pytest

from agent_families.export import slugify
from agent_families.reflector.derive import DeriveParams, derive_skills
from agent_families.store import Store
from agent_families.vecindex import VecIndex

DIM = 4


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    with store.transaction():
        family_id = store.create_family("library")
        agent_id = store.create_agent(family_id, "librarian")
    yield store, vec, agent_id
    store.close()


def _unit2d(deg: float) -> list[float]:
    """A unit vector at angle ``deg`` in the first two dims (pad to DIM)."""
    r = math.radians(deg)
    return [math.cos(r), math.sin(r), 0.0, 0.0]


def _insert(store, vec, hint, angle, *, status="active"):
    """Insert an active insight + its vec row in one transaction; return id."""
    v = _unit2d(angle)
    with store.transaction():
        iid = store.insert_insight(
            precondition=f"precondition {hint}",
            action=f"action {hint}",
            expected_outcome=f"outcome {hint}",
            content_hash=f"hash-{hint}",
            status=status,
        )
        vec.insert(iid, v, v)
    return iid


def _two_clusters(store, vec):
    """Two well-separated 4-clusters (A near 0°, B near 125°). Returns (a_ids, b_ids).

    With knn_k=3 each cluster's three nearest neighbors are its own members, so the
    mutual-kNN graph is two disjoint 4-cliques — no cross edges, no negative
    Tanimoto weights.
    """
    a = [_insert(store, vec, f"a{i}", angle) for i, angle in enumerate((0, 5, 10, 15))]
    b = [_insert(store, vec, f"b{i}", angle) for i, angle in enumerate((120, 125, 130, 135))]
    return a, b


def _snapshot_count(store) -> int:
    return store.conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]


def _members(store, skill_id) -> list[int]:
    return store.skill_members(skill_id)


def _agent_modules(store, agent_id) -> dict[int, str]:
    """``skill_id -> name`` for the agent's modules (ascending id)."""
    rows = store.conn.execute(
        "SELECT id, name FROM skills WHERE agent_id = ? ORDER BY id ASC", (agent_id,)
    ).fetchall()
    return {r["id"]: r["name"] for r in rows}


PARAMS = DeriveParams(knn_k=3)


# --- cold start: the first grouping is adopted (supporting) ------------------------


def test_cold_start_adopts_first_grouping(env):
    """At cold start the incumbent is all-singletons; the first real grouping
    (two communities for the two clusters) strictly beats it and is adopted."""
    store, vec, agent_id = env
    a, b = _two_clusters(store, vec)

    result = derive_skills(store, vec, agent_id=agent_id, params=PARAMS)

    assert result.adopted is True
    assert result.num_communities == 2
    assert result.selected_cost < result.incumbent_cost
    # the two communities recover the two clusters exactly
    module_members = {sid: set(_members(store, sid)) for sid in result.module_ids}
    assert set(map(frozenset, module_members.values())) == {frozenset(a), frozenset(b)}


# --- R8: a non-trivial accepted pass mints EXACTLY ONE snapshot --------------------


def test_accepted_pass_mints_exactly_one_snapshot(env):
    """The whole partition rewrite goes through one queue_operation('derive') and
    mints exactly one snapshot — not one-per-module, not zero."""
    store, vec, agent_id = env
    _two_clusters(store, vec)

    before = _snapshot_count(store)
    result = derive_skills(store, vec, agent_id=agent_id, params=PARAMS)
    after = _snapshot_count(store)

    assert result.adopted is True
    assert result.snapshot_id is not None
    assert after - before == 1  # exactly one snapshot for the whole partition
    # the snapshot was minted by a 'derive' promotion-queue operation
    op = store.conn.execute(
        "SELECT operation FROM promotion_queue WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert op["operation"] == "derive"


# --- R8: a no-op pass mints ZERO snapshots, active set untouched -------------------


def test_noop_pass_mints_no_snapshot(env):
    """A second pass over the unchanged library cannot beat the incumbent: it
    leaves membership untouched and mints zero snapshots (count before == after)."""
    store, vec, agent_id = env
    _two_clusters(store, vec)

    first = derive_skills(store, vec, agent_id=agent_id, params=PARAMS)
    assert first.adopted is True

    membership_before = {
        sid: _members(store, sid) for sid in _agent_modules(store, agent_id)
    }
    snaps_before = _snapshot_count(store)

    second = derive_skills(store, vec, agent_id=agent_id, params=PARAMS)

    assert second.adopted is False
    assert second.snapshot_id is None
    assert _snapshot_count(store) == snaps_before  # ZERO new snapshots
    # the active set (module membership) is byte-identical — nothing was rewritten
    membership_after = {
        sid: _members(store, sid) for sid in _agent_modules(store, agent_id)
    }
    assert membership_after == membership_before


# --- R9: identity stable across two passes ----------------------------------------


def test_identity_stable_across_two_passes(env):
    """Two passes over an unchanged library map every community to the SAME module
    id; module ids, lazy names, and SKILL.md slugs are byte-identical run-to-run."""
    store, vec, agent_id = env
    _two_clusters(store, vec)

    first = derive_skills(store, vec, agent_id=agent_id, params=PARAMS)
    assert first.adopted is True
    modules_1 = _agent_modules(store, agent_id)
    slugs_1 = {sid: slugify(name) for sid, name in modules_1.items()}

    second = derive_skills(store, vec, agent_id=agent_id, params=PARAMS)
    assert second.adopted is False  # unchanged library → no re-derivation
    modules_2 = _agent_modules(store, agent_id)
    slugs_2 = {sid: slugify(name) for sid, name in modules_2.items()}

    # same module ids (no renumbering), same lazy names, same export slugs
    assert set(modules_2) == set(modules_1)
    assert modules_2 == modules_1
    assert slugs_2 == slugs_1
    # every name is non-NULL (each community was named on the adopting pass)
    assert all(name is not None for name in modules_1.values())


# --- R9: majority-overlap reassigns on a membership shift (a module splits) --------


def test_majority_overlap_reassigns_on_membership_shift(env):
    """A prior module that splits: the community holding a strict majority of its
    ids INHERITS the module id; the other community (no majority) gets a fresh id."""
    store, vec, agent_id = env
    a, b = _two_clusters(store, vec)
    all_ids = a + b

    # Pre-seed ONE coarse prior module S holding every insight (the module that will
    # be split by the derive pass into the two clusters).
    with store.transaction():
        s_id = store.create_skill(agent_id, "coarse", "everything")
        for iid in all_ids:
            store.append_member(s_id, iid)

    result = derive_skills(store, vec, agent_id=agent_id, params=PARAMS)

    assert result.adopted is True
    assert result.num_communities == 2
    # exactly one community inherited S; the other is brand new
    assert s_id in result.inherited_module_ids
    assert len(result.new_module_ids) == 1
    new_id = result.new_module_ids[0]
    assert new_id != s_id

    # the canonical-lowest community (the one containing the lowest-id insight) wins
    # the tie for S; cluster A holds the lowest id, so A inherits S and B is fresh.
    assert set(_members(store, s_id)) == set(a)
    assert set(_members(store, new_id)) == set(b)


# --- R10: only changed communities are renamed ------------------------------------


class _CountingNamer:
    """A namer seam that records every (skill_id, members) call it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[int, ...]]] = []

    def __call__(self, store, skill_id, members):
        self.calls.append((skill_id, tuple(members)))
        return (f"module-{skill_id}", f"desc {skill_id}")


def test_only_changed_communities_renamed(env):
    """With the namer mocked to count calls, only communities whose membership
    changed (and need a name) are passed to it; unchanged communities → zero calls.

    Pass 1 groups clusters A and B. Pass 2 adds a whole new dense cluster C: the
    objective groups C into one fresh module while A and B are inherited unchanged,
    so the namer fires exactly once — for C, never for A or B."""
    store, vec, agent_id = env
    a, b = _two_clusters(store, vec)

    namer = _CountingNamer()
    first = derive_skills(
        store, vec, agent_id=agent_id, params=PARAMS, namer_fn=namer
    )
    assert first.adopted is True
    # cold start: BOTH new communities are named.
    assert len(namer.calls) == 2
    skill_a = first.community_module[0]  # label 0 = cluster A (lowest min member)
    skill_b = first.community_module[1]  # label 1 = cluster B
    names_before = _agent_modules(store, agent_id)

    # Add a whole new dense cluster C, far from A and B → a new community forms while
    # A and B are unchanged.
    c = [_insert(store, vec, f"c{i}", angle) for i, angle in enumerate((235, 240, 245, 250))]

    namer2 = _CountingNamer()
    second = derive_skills(
        store, vec, agent_id=agent_id, params=PARAMS, namer_fn=namer2
    )

    assert second.adopted is True  # grouping the new dense cluster lowers cost
    # exactly ONE namer call — for the new community C, not the unchanged A or B.
    assert second.namer_calls == 1
    assert len(namer2.calls) == 1
    renamed_skill_id, renamed_members = namer2.calls[0]
    assert set(renamed_members) == set(c)
    assert second.new_module_ids == (renamed_skill_id,)
    assert second.renamed_module_ids == (renamed_skill_id,)
    # A and B kept their module ids AND their names (no namer call for either).
    assert skill_a in second.inherited_module_ids
    assert skill_b in second.inherited_module_ids
    names_after = _agent_modules(store, agent_id)
    assert names_after[skill_a] == names_before[skill_a]
    assert names_after[skill_b] == names_before[skill_b]
