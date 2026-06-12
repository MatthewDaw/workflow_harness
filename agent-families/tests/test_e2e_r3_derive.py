"""plan-009 U9: config wiring + R3 derive-pass end-to-end acceptance (R19, R20).

The R3 **slow loop** driven offline over a real migrated store: a seeded library of
active, *ungrouped* insights (the post-ingest hand-off plan 008 produces — the
gauntlet authors no group; see ``tests/test_e2e_r3_ingest.py``) is run through the
full derive cycle and asserted end to end —

    seed (ungrouped active insights, clustered vectors)
      -> derive            (build mutual-kNN+Tanimoto graph -> propose Leiden/Infomap
                            candidates -> select the §6a-minimal whole partition ->
                            adopt iff it beats the incumbent -> one snapshot)
      -> consolidate        (a per-move delta: a tight community of specifics -> one
                            general parent + dormant children, iff it lowers cost(G))
      -> retrieve           (whole-store, insight-level, cosine-ranked, no family pool)

and asserts the load-bearing R20 invariants: whole-partition acceptance mints exactly
one snapshot; a second pass over the unchanged library is a no-op with byte-identical
module identity; a consolidation move applies only when it lowers ``cost(G)``;
retrieval reaches the whole store regardless of owning module; and the whole cycle is
offline and deterministic (two runs are byte-identical).

The config wiring (R19) is *exercised, not hardcoded*: every tunable the cycle reads —
``knn_k``, ``leiden_resolution_sweep``, ``infomap_seed`` (``[graph]``) and
``module_overhead_bits`` (``[objective]``) — is loaded from a ``thresholds.toml`` via
:func:`load_config` and threaded into ``DeriveParams`` / ``ConsolidationParams``. A
separate test pins the *shipped* template's R19 keys/values and that they match the
code defaults.

Fully offline / zero quota (R20): the derive namer and the consolidation generalizer
are the deterministic offline seams (no ``claude``), there is no judge/NLI call on the
derive path, embeddings are hand-placed angle vectors, and the native partitioner
wheels are optional (the exact pure-Python fallback runs when they are absent). No
``claude`` on PATH, no model download.

## plan-009 U9 deviation — seeding (documented, not hidden)

The plan's Approach reads "ingest (plan 008) -> derive". The plan-008 ingest gauntlet
hands the derive pass an *ungrouped active set*; this e2e seeds that set directly
(real ``Store``/``VecIndex`` inserts with clustered vectors) rather than re-driving
``add_idea_r3`` with its judge/NLI fixtures — the gauntlet itself is covered end to
end by ``tests/test_e2e_r3_ingest.py``. The handoff invariant (zero skills authored at
ingest) is asserted here as the cycle's precondition, so the seam the two plans share
is still pinned. This keeps the U9 e2e focused on the derive/consolidation/retrieval
cycle and its config wiring — the smallest faithful adaptation under the wave's
"one unit, don't touch other units' files" constraint.

## Conformance (009 U9 required acceptance tests -> invariant)

| Invariant (plan-009 U9, R19/R20) | Test |
|---|---|
| the full cycle adopts the lowest-cost partition only because it beats the incumbent and mints EXACTLY ONE snapshot for the whole rewrite | `test_e2e_whole_partition_accepted_one_snapshot` |
| a second derive pass over the unchanged post-derive library is a no-op: zero new snapshots, module ids/names byte-identical | `test_e2e_identity_stable_second_pass_noop` |
| the cycle produces a consolidation move (general parent `provenance=consolidated` + dormant children + `generalizes_from` edges) when and only when it lowers `cost(G)` | `test_e2e_consolidation_move_applied` |
| post-derive retrieval returns insight-granular results ranked by cosine across the whole store regardless of owning module | `test_e2e_insight_level_retrieval_reaches_whole_store` |
| the e2e runs fully offline (no network) and two runs produce byte-identical final membership, snapshot count, and retrieval ordering | `test_e2e_offline_and_deterministic` |

Supporting (guard the config-wiring surface, R19):

- the shipped thresholds.toml carries the R19 `[graph]`/`[objective]` keys with the
  pinned values, and those values match the code defaults: `test_shipped_config_carries_r19_keys`
- the loaded config threads into the derive/consolidation params (wiring, not
  hardcoding): every cycle test builds its params from a loaded `Config`.
"""

from __future__ import annotations

import math
import textwrap
from pathlib import Path

import pytest

from agent_families import judge, nli
from agent_families.config import load_config
from agent_families.export import slugify
from agent_families.library.retrieval import RetrievalParams, retrieve
from agent_families.reflector import consolidation as cons
from agent_families.reflector import objective as obj
from agent_families.reflector import partition as part
from agent_families.reflector.derive import DeriveParams, derive_skills
from agent_families.store import Store
from agent_families.vecindex import VecIndex

REPO_THRESHOLDS = Path(__file__).resolve().parent.parent / "thresholds.toml"

DIM = 4

# A small-knn e2e thresholds.toml: knn_k=3 separates the hand-placed 4-node clusters
# (k>=cluster size would connect everything). Every other key is a shipped default;
# the [graph]/[objective] keys are exactly the R19 keys this unit wires, loaded and
# threaded into the derive/consolidation params below (the wiring is exercised).
E2E_THRESHOLDS = """\
[embedding]
model = "fake-embedder"
dim = 4
device = "cpu"
[merge]
cosine_threshold = 0.92
candidate_floor = 0.80
[retrieval]
ann_top_k = 10
relevance_floor = 0.0
[judge]
model = "sonnet"
max_retries = 1
bare = false
[lifecycle]
active_cap = 50
[store]
busy_timeout_ms = 5000
[graph]
knn_k = 3
edge_weight = "tanimoto"
leiden_resolution_sweep = [0.5, 1.0, 2.0]
infomap_seed = 1234
[objective]
module_overhead_bits = 4.0
"""


# --- config wiring: a loaded Config -> the derive/consolidation params (R19) -------


def _load_e2e_config(tmp_path):
    path = tmp_path / "thresholds.toml"
    path.write_text(E2E_THRESHOLDS, encoding="utf-8")
    return load_config(path)


def _derive_params(cfg) -> DeriveParams:
    """Build DeriveParams FROM config — the R19 wiring, not a hardcoded literal."""
    return DeriveParams(
        knn_k=cfg.graph.knn_k,
        resolutions=cfg.graph.leiden_resolution_sweep,
        seed=cfg.graph.infomap_seed,
        module_overhead_bits=cfg.objective.module_overhead_bits,
    )


def _consolidation_params(cfg) -> cons.ConsolidationParams:
    return cons.ConsolidationParams(
        knn_k=cfg.graph.knn_k,
        module_overhead_bits=cfg.objective.module_overhead_bits,
    )


# --- offline seeded library (the post-ingest ungrouped active set) -----------------


def _unit(deg: float) -> list[float]:
    r = math.radians(deg)
    return [math.cos(r), math.sin(r), 0.0, 0.0]


def _fresh_env(tmp_path, name: str):
    store = Store(tmp_path / f"{name}.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    with store.transaction():
        family_id = store.create_family("library")
        agent_id = store.create_agent(family_id, "librarian")
    return store, vec, agent_id


def _insert(store, vec, hint, angle, *, status="active"):
    v = _unit(angle)
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
    """Two well-separated 4-clusters (A near 0°, B near 125°)."""
    a = [_insert(store, vec, f"a{i}", angle) for i, angle in enumerate((0, 5, 10, 15))]
    b = [_insert(store, vec, f"b{i}", angle) for i, angle in enumerate((120, 125, 130, 135))]
    return a, b


def _snapshot_count(store) -> int:
    return store.conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]


def _agent_modules(store, agent_id) -> dict[int, str]:
    rows = store.conn.execute(
        "SELECT id, name FROM skills WHERE agent_id = ? ORDER BY id ASC", (agent_id,)
    ).fetchall()
    return {r["id"]: r["name"] for r in rows}


def _status(store, insight_id) -> str:
    return store.get_insight(insight_id)["status"]


# --- R20: whole-partition acceptance mints exactly one snapshot --------------------


def test_e2e_whole_partition_accepted_one_snapshot(tmp_path):
    """The full cycle adopts the lowest-cost partition ONLY because it beats the
    incumbent and mints EXACTLY ONE snapshot for the whole rewrite — whole-partition
    acceptance, never per-move cherry-pick. The params are loaded from config (R19)."""
    cfg = _load_e2e_config(tmp_path)
    store, vec, agent_id = _fresh_env(tmp_path, "accept")
    a, b = _two_clusters(store, vec)

    # plan-008 hand-off precondition: the ingest gauntlet authored ZERO groups.
    assert _agent_modules(store, agent_id) == {}

    before = _snapshot_count(store)
    result = derive_skills(store, vec, agent_id=agent_id, params=_derive_params(cfg))
    after = _snapshot_count(store)

    assert result.adopted is True
    assert result.selected_cost < result.incumbent_cost  # adopted BECAUSE it beats
    assert result.num_communities == 2
    assert after - before == 1  # exactly one snapshot for the whole partition
    op = store.conn.execute(
        "SELECT operation FROM promotion_queue WHERE snapshot_id = ?",
        (result.snapshot_id,),
    ).fetchone()
    assert op["operation"] == "derive"
    # the two communities recover the two clusters exactly
    members = {sid: set(store.skill_members(sid)) for sid in result.module_ids}
    assert set(map(frozenset, members.values())) == {frozenset(a), frozenset(b)}
    store.close()


# --- R20: identity stable, second pass is a no-op ----------------------------------


def test_e2e_identity_stable_second_pass_noop(tmp_path):
    """A second derive pass over the unchanged post-derive library is a no-op: zero
    new snapshots, and module ids / lazy names / SKILL.md slugs byte-identical to the
    first pass (identity stability end-to-end)."""
    cfg = _load_e2e_config(tmp_path)
    store, vec, agent_id = _fresh_env(tmp_path, "identity")
    _two_clusters(store, vec)

    first = derive_skills(store, vec, agent_id=agent_id, params=_derive_params(cfg))
    assert first.adopted is True
    modules_1 = _agent_modules(store, agent_id)
    slugs_1 = {sid: slugify(name) for sid, name in modules_1.items()}
    snaps_after_first = _snapshot_count(store)

    second = derive_skills(store, vec, agent_id=agent_id, params=_derive_params(cfg))

    assert second.adopted is False  # unchanged library cannot beat the incumbent
    assert second.snapshot_id is None
    assert _snapshot_count(store) == snaps_after_first  # ZERO new snapshots
    modules_2 = _agent_modules(store, agent_id)
    slugs_2 = {sid: slugify(name) for sid, name in modules_2.items()}
    assert modules_2 == modules_1   # same ids AND names, byte-identical
    assert slugs_2 == slugs_1
    assert all(name is not None for name in modules_1.values())
    store.close()


# --- R20: a consolidation move applies iff it lowers cost(G) -----------------------


def test_e2e_consolidation_move_applied(tmp_path):
    """The cycle produces a consolidation move — a general parent
    (`provenance=consolidated`) + dormant children with `generalizes_from` edges —
    WHEN AND ONLY WHEN it lowers `cost(G)`. A tight, isolated community of specifics
    compresses (adopted); specifics straddling two distinct communities bridge them
    (rejected, nothing written)."""
    cfg = _load_e2e_config(tmp_path)
    params = _consolidation_params(cfg)

    # (a) ADOPTED: a tight cluster of 4 specifics + a far cluster; contracting the
    # four children shrinks the store, so cost(G) falls.
    store, vec, _ = _fresh_env(tmp_path, "cons-ok")
    children = [_insert(store, vec, f"c{i}", angle) for i, angle in enumerate((0, 3, 6, 9))]
    [_insert(store, vec, f"o{i}", angle) for i, angle in enumerate((180, 183, 186, 189))]

    before = _snapshot_count(store)
    ok = cons.consolidate(store, vec, child_insight_ids=children, params=params)

    assert ok.adopted is True
    assert ok.cost_delta < 0                      # adopted BECAUSE it lowers cost
    assert _snapshot_count(store) - before == 1   # exactly one snapshot for the move
    parent = store.get_insight(ok.parent_insight_id)
    assert parent["provenance"] == "consolidated"
    assert parent["status"] == "active"
    for child in children:
        assert _status(store, child) == "dormant"  # demoted, preserved, never retired
        assert _status(store, child) != "retired"
    linked = [
        r["dst"]
        for r in store.conn.execute(
            "SELECT dst FROM insight_edges WHERE src = ? AND kind = 'generalizes_from'"
            " ORDER BY dst ASC",
            (ok.parent_insight_id,),
        ).fetchall()
    ]
    assert linked == sorted(children)             # one edge to EVERY child
    store.close()

    # (b) REJECTED: two specifics from DISTINCT clusters bridge them -> cost rises.
    store2, vec2, _ = _fresh_env(tmp_path, "cons-no")
    x = [_insert(store2, vec2, f"x{i}", angle) for i, angle in enumerate((0, 3, 6, 9))]
    y = [_insert(store2, vec2, f"y{i}", angle) for i, angle in enumerate((120, 123, 126, 129))]
    snaps_before = _snapshot_count(store2)
    consolidated_before = store2.conn.execute(
        "SELECT COUNT(*) AS n FROM insights WHERE provenance = 'consolidated'"
    ).fetchone()["n"]

    no = cons.consolidate(store2, vec2, child_insight_ids=[x[3], y[3]], params=params)

    assert no.adopted is False
    assert no.cost_delta >= 0                      # the gate saw a non-improving move
    assert no.parent_insight_id is None
    assert _snapshot_count(store2) == snaps_before  # nothing written
    assert store2.conn.execute(
        "SELECT COUNT(*) AS n FROM insights WHERE provenance = 'consolidated'"
    ).fetchone()["n"] == consolidated_before
    assert all(_status(store2, iid) == "active" for iid in x + y)
    assert store2.conn.execute(
        "SELECT COUNT(*) AS n FROM insight_edges WHERE kind = 'generalizes_from'"
    ).fetchone()["n"] == 0
    store2.close()


# --- R20: insight-level retrieval reaches the whole store --------------------------


def test_e2e_insight_level_retrieval_reaches_whole_store(tmp_path):
    """Post-derive retrieval returns insight-granular results ranked by cosine across
    the WHOLE store regardless of owning module — no family pool, no own-skills bias.
    A query near cluster B retrieves B's insights even though they live in a module
    the (inert) working_agent_id has no relationship to."""
    cfg = _load_e2e_config(tmp_path)
    store, vec, agent_id = _fresh_env(tmp_path, "retrieve")
    a, b = _two_clusters(store, vec)

    # Derive first so the insights actually live in modules (A -> one, B -> another).
    derived = derive_skills(store, vec, agent_id=agent_id, params=_derive_params(cfg))
    assert derived.adopted is True and derived.num_communities == 2

    params = RetrievalParams(budget_tokens=100_000, relevance_floor=cfg.retrieval.relevance_floor)
    query = _unit(125)  # near cluster B's centre
    result = retrieve(
        store,
        query_vector=query,
        params=params,
        mode="training",
        working_agent_id=999_999,  # an unrelated agent: must NOT scope reachability
        family_id=888_888,          # an unrelated family: likewise inert
    )

    all_active = set(a) | set(b)
    # whole visible store is the candidate pool — no family scoping.
    assert result.pool_insight_ids == frozenset(all_active)
    # the result is insight-granular (a tuple of insight ids), not whole skills.
    assert all(isinstance(iid, int) for iid in result.insights)
    # ownership != reachability, total: B's insights (in their own module) are
    # retrieved purely by cosine even with an unrelated working_agent_id/family_id.
    assert set(b) <= set(result.insights)
    # ranked by cosine: every B insight (near the query) outranks every A insight.
    order = [c.insight_id for c in result.candidates]
    last_b = max(order.index(iid) for iid in b)
    first_a = min(order.index(iid) for iid in a)
    assert last_b < first_a
    store.close()


# --- R20: the whole cycle is offline + deterministic -------------------------------


def _run_full_cycle(tmp_path, cfg, name: str):
    """seed -> derive -> consolidate (a separate tight community) -> retrieve.

    Returns a canonical, comparable summary of the final state so two independent
    runs can be asserted byte-identical (membership, snapshot count, retrieval order).
    """
    store, vec, agent_id = _fresh_env(tmp_path, name)
    # A and B (derive communities) + D, a third tight community we then consolidate.
    a = [_insert(store, vec, f"a{i}", angle) for i, angle in enumerate((0, 5, 10, 15))]
    b = [_insert(store, vec, f"b{i}", angle) for i, angle in enumerate((120, 125, 130, 135))]
    d = [_insert(store, vec, f"d{i}", angle) for i, angle in enumerate((235, 240, 245, 250))]

    derived = derive_skills(store, vec, agent_id=agent_id, params=_derive_params(cfg))
    consolidated = cons.consolidate(
        store, vec, child_insight_ids=d, params=_consolidation_params(cfg)
    )
    result = retrieve(
        store,
        query_vector=_unit(0),  # near cluster A
        params=RetrievalParams(budget_tokens=100_000, relevance_floor=0.0),
        mode="training",
    )

    membership = sorted(
        (sid, tuple(store.skill_members(sid)))
        for sid in _agent_modules(store, agent_id)
    )
    summary = {
        "derive_adopted": derived.adopted,
        "derive_communities": derived.num_communities,
        "consolidation_adopted": consolidated.adopted,
        "consolidation_parent_active": (
            consolidated.parent_insight_id is not None
            and _status(store, consolidated.parent_insight_id) == "active"
        ),
        "membership": membership,
        "snapshot_count": _snapshot_count(store),
        "retrieval_order": result.insights,
        "module_names": tuple(sorted(_agent_modules(store, agent_id).values())),
    }
    store.close()
    return summary


def test_e2e_offline_and_deterministic(tmp_path, monkeypatch):
    """The whole derive cycle runs fully offline (no `claude`, no model load) and two
    independent runs produce byte-identical final membership, snapshot count, and
    retrieval ordering."""
    # Prove offline: no `claude` resolvable, and any escaped live judge/NLI call hard
    # fails. The derive/consolidation/retrieval path uses only the deterministic
    # offline seams, so none of these fire.
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        judge, "_invoke",
        lambda *a, **k: pytest.fail("a live `claude -p` call escaped the offline cycle"),
    )
    monkeypatch.setattr(
        nli, "_load_encoder",
        lambda *a, **k: pytest.fail("an NLI model load escaped the offline cycle"),
    )

    cfg = _load_e2e_config(tmp_path)
    run1 = _run_full_cycle(tmp_path, cfg, "cycle1")
    run2 = _run_full_cycle(tmp_path, cfg, "cycle2")

    # The cycle did real work (not a vacuous no-op), then repeated it byte-identically.
    assert run1["derive_adopted"] is True
    assert run1["consolidation_adopted"] is True
    assert run1["consolidation_parent_active"] is True
    assert run1 == run2  # byte-identical membership, snapshot count, retrieval order


# --- R19: the shipped config carries the [graph]/[objective] keys -------------------


def test_shipped_config_carries_r19_keys():
    """The shipped thresholds.toml carries the R19 `[graph]`/`[objective]` keys with
    the pinned values, and those values match the code defaults the derive stack uses
    (so config and code cannot silently drift). `active_cap` stays present (demoted in
    place for reversibility); the removed split machinery leaves no silhouette/k-means
    threshold behind."""
    cfg = load_config(REPO_THRESHOLDS)

    # [graph] (R4/R6/R7) — values AND agreement with the code defaults.
    assert cfg.graph.knn_k == 15
    assert cfg.graph.edge_weight == "tanimoto"
    assert cfg.graph.leiden_resolution_sweep == part.DEFAULT_RESOLUTIONS
    assert cfg.graph.infomap_seed == part.DEFAULT_SEED
    # [objective] (R1) — the per-module bit cost, matching the pinned constant.
    assert cfg.objective.module_overhead_bits == pytest.approx(
        obj.DEFAULT_MODULE_OVERHEAD_BITS
    )
    assert cfg.objective.module_overhead_bits == pytest.approx(4.0)

    # active_cap is demoted (R20/D-2) but retained for reversibility, not deleted.
    assert cfg.lifecycle.active_cap == 50
    # No silhouette / split-machinery threshold KEYS survive in the shipped template
    # (R16/R19). Scan only key assignments (`key = ...`), not provenance prose — a
    # comment may legitimately mention "silhouette" as a tuning metric.
    keys = [
        line.split("=", 1)[0].strip().lower()
        for line in REPO_THRESHOLDS.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    for banned in ("silhouette", "kmeans", "split", "tournament", "active_split"):
        assert not any(banned in key for key in keys)


def test_e2e_config_unknown_objective_key_rejected(tmp_path):
    """The loader fail-fasts on an unknown `[objective]` key (R19 discipline: the new
    section is validated, not silently accepted)."""
    from agent_families.config import ConfigError

    bad = E2E_THRESHOLDS + textwrap.dedent(
        """
        bogus_objective_key = 1
        """
    )
    path = tmp_path / "thresholds.toml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "bogus_objective_key" in str(exc.value)
