"""plan-007 U10: provenance & world telemetry plumbing (R7).

Mechanical plumbing, fully offline (the default suite):

- ``af add-idea --provenance`` round-trips through the real CLI handler with a
  fake embedder (the ``cli._embedding_service`` seam) and replayed judge fixtures,
  so zero quota and no ``claude`` on PATH.
- the reflector auto-stamp is the label-convention seam (``reflect-ep%`` ->
  ``reflector``), the same predicate U1's migration backfills on.
- the provenance x world fitness table is a pure query over the append-only
  fitness log (world joined via ``fitness_events.episode_id -> episodes.world``).
- the validation-class substrate routing is a pure function plus a default-deny
  guard in ``validate_batch``: an elicitation batch is held in quarantine until
  its greenfield substrate (U13b) exists, never validated on the wrong instrument.

## Conformance

Required acceptance tests (plan-007 U10) -> the invariant each enforces:

- ``test_provenance_flag_roundtrips_and_autostamp`` -- ``af add-idea
  --provenance`` round-trips (default ``manual``); reflector batches auto-stamp
  ``reflector``. [enforced by ``cli._cmd_add_idea`` ->
  :func:`lifecycle.stamp_insight_provenance` + :func:`lifecycle.auto_stamp_batch_provenance`]
- ``test_provenance_world_fitness_grouping`` -- the maintenance fitness table
  groups correctly by provenance x world (world joined via
  ``fitness_events.episode_id -> episodes.world``). [enforced by
  :func:`maintenance.provenance_world_fitness`]
- ``test_elicitation_batch_quarantined_pre_substrate`` -- before U13b's substrate
  exists, an elicitation-class batch stays quarantined with a TYPED hold reason
  (default-deny; never silently validated on the wrong instrument). [enforced by
  :func:`validate.route_substrates` + the :func:`validate.validate_batch` guard]

Test-scenario / invariant (plan-007 U10) -> test:

- flag round-trips; reflector auto-stamp: ``test_provenance_flag_roundtrips_and_autostamp``
- provenance x world grouping on fixture events: ``test_provenance_world_fitness_grouping``
- an elicitation-class batch pre-U13b stays quarantined with a typed hold reason:
  ``test_elicitation_batch_quarantined_pre_substrate``
- substrate routing per class (code/elicitation/general): ``test_route_substrates_per_class``
- validation_class set at registration: ``test_set_batch_validation_class``
- provenance stamp validates its enum: ``test_provenance_stamps_reject_unknown``
- the training channel filter excludes trial/benchmark fitness:
  ``test_provenance_world_fitness_channel_filter``
"""

from __future__ import annotations

import math

import pytest

from agent_families import cli, judge, lifecycle
from agent_families.embedding import EmbeddingService
from agent_families.judge import write_fixture
from agent_families.lifecycle import LifecycleError
from agent_families.pipeline import (
    JUDGE_SCHEMA,
    build_idea_text,
    build_taxonomy_prompt,
)
from agent_families.reflector import maintenance as m
from agent_families.reflector import validate as v
from agent_families.store import Store

DIM = 8

# Small-dim config so hand-chosen cosines drive the routing; thresholds match the
# shipped template's tunables (the test_e2e precedent).
THRESHOLDS = """\
[embedding]
model = "fake-embedder"
dim = 8
device = "cpu"

[merge]
cosine_threshold = 0.92

[retrieval]
ann_top_k = 10
relevance_floor = 0.5

[judge]
model = "sonnet"
max_retries = 1
bare = false

[lifecycle]
active_cap = 50

[store]
busy_timeout_ms = 5000
"""


# === offline store fixture (telemetry + routing tests) =========================


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


def _insight(store: Store, n: int, *, batch_id=None, status="active") -> int:
    return store.insert_insight(
        precondition=f"pre {n}",
        action=f"act {n}",
        expected_outcome=f"out {n}",
        content_hash=f"hash-{n}",
        status=status,
        batch_id=batch_id,
    )


def _episode_with_world(store: Store, world: str, *, mode="training") -> int:
    ep = store.create_episode("linkding", "digest", 0, mode=mode)
    store.conn.execute("UPDATE episodes SET world = ? WHERE id = ?", (world, ep))
    return ep


def _register_quarantined_batch(store: Store, label: str, n: int = 2):
    batch_id = store.ensure_batch(label)
    ids = [
        store.insert_insight(
            precondition=f"when {label} {i}",
            action=f"do {label} {i}",
            expected_outcome=f"expect {label} {i}",
            content_hash=f"{label}-hash-{i}",
            status="quarantined",
            batch_id=batch_id,
        )
        for i in range(n)
    ]
    return batch_id, ids


def _statuses(store: Store, ids):
    return {iid: store.get_insight(iid)["status"] for iid in ids}


# === required: provenance flag round-trip + reflector auto-stamp ===============


class _MappedEncoder:
    """text -> hand-chosen vector; any unexpected embed is a hard failure."""

    def __init__(self, mapping):
        self.mapping = dict(mapping)

    def encode(self, text):
        if text not in self.mapping:
            raise AssertionError(f"unexpected embed text: {text!r}")
        return list(self.mapping[text])


def _install_fake_embedder(monkeypatch, mapping):
    monkeypatch.setattr(
        cli,
        "_embedding_service",
        lambda config: EmbeddingService(config.embedding, encoder=_MappedEncoder(mapping)),
    )


def _envelope(output: dict) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 10,
        "num_turns": 1,
        "result": "ok",
        "total_cost_usd": 0.0,
        "structured_output": output,
    }


def _taxonomy_new_skill(agent_id: int, name: str) -> dict:
    return {
        "outcome": "new_skill",
        "scope_tag": {"value": "universal", "justification": "applies to any web target"},
        "lint": {"verdict": "pass"},
        "confidence": 0.9,
        "new_skill": {
            "agent_id": agent_id,
            "name": name,
            "description": "an elicitation discipline",
        },
    }


def test_provenance_flag_roundtrips_and_autostamp(tmp_path, monkeypatch, capsys):
    """`af add-idea --provenance` round-trips (default `manual`); reflector
    batches auto-stamp `reflector` (required)."""
    monkeypatch.delenv(judge.MODE_ENV, raising=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    db = lib / "library.db"
    fx = tmp_path / "fixtures"
    (lib / "thresholds.toml").write_text(THRESHOLDS, encoding="utf-8")
    monkeypatch.setenv(judge.FIXTURES_ENV, str(fx))

    idea1 = dict(
        precondition="A new target app needs its requirements gathered",
        action="Surface decisions the informant never thought about",
        expected_outcome="Resolved design decisions enter the requirement set",
    )
    idea2 = dict(
        precondition="An elicitation interview has stalled on open questions",
        action="Offer a small set of ranked options instead of an open prompt",
        expected_outcome="The informant decides quickly from concrete choices",
    )
    # Orthogonal unit vectors: idea2's nearest neighbour (idea1) sits at cosine 0,
    # below the 0.5 relevance floor, so both ideas hit the cold-start taxonomy
    # prompt rather than chaining through placement.
    v1 = [1.0] + [0.0] * (DIM - 1)
    v2 = [0.0, 1.0] + [0.0] * (DIM - 2)
    key1 = "search_document: " + build_idea_text(**idea1)
    key2 = "search_document: " + build_idea_text(**idea2)
    _install_fake_embedder(monkeypatch, {key1: v1, key2: v2})

    assert cli.main(["init", "--dir", str(lib)]) == 0
    with Store(db) as s:
        agent_id = s.conn.execute(
            "SELECT a.id AS id FROM agents a JOIN families f ON f.id = a.family_id"
            " WHERE f.name = 'planner'"
        ).fetchone()["id"]
        prompt1 = build_taxonomy_prompt(s, build_idea_text(**idea1), None)
    write_fixture(fx, prompt1, JUDGE_SCHEMA, "sonnet",
                  _envelope(_taxonomy_new_skill(agent_id, "decision-surfacing")))

    # Explicit --provenance researched round-trips.
    rc = cli.main([
        "add-idea", "--dir", str(lib),
        "--precondition", idea1["precondition"],
        "--action", idea1["action"],
        "--expected-outcome", idea1["expected_outcome"],
        "--batch", "by-hand",
        "--provenance", "researched",
    ])
    assert rc == 0
    with Store(db) as s:
        row = s.conn.execute(
            "SELECT id, provenance FROM insights ORDER BY id"
        ).fetchall()
        assert len(row) == 1
        assert row[0]["provenance"] == "researched"
        prompt2 = build_taxonomy_prompt(s, build_idea_text(**idea2), None)
    write_fixture(fx, prompt2, JUDGE_SCHEMA, "sonnet",
                  _envelope(_taxonomy_new_skill(agent_id, "option-framing")))

    # No --provenance flag defaults to manual.
    rc = cli.main([
        "add-idea", "--dir", str(lib),
        "--precondition", idea2["precondition"],
        "--action", idea2["action"],
        "--expected-outcome", idea2["expected_outcome"],
        "--batch", "by-hand",
    ])
    assert rc == 0
    with Store(db) as s:
        prov = {
            r["id"]: r["provenance"]
            for r in s.conn.execute("SELECT id, provenance FROM insights").fetchall()
        }
        assert prov == {1: "researched", 2: "manual"}

    # Reflector auto-stamp: a `reflect-ep%` batch's insights stamp `reflector`
    # without an explicit value (the stage_b label convention).
    with Store(db) as s:
        rbatch = s.ensure_batch("reflect-ep7-cluster3")
        ri1 = _insight(s, 901, batch_id=rbatch, status="quarantined")
        ri2 = _insight(s, 902, batch_id=rbatch, status="quarantined")
        stamped = lifecycle.auto_stamp_batch_provenance(s, "reflect-ep7-cluster3")
        assert set(stamped) == {ri1, ri2}
        after = {
            r["id"]: r["provenance"]
            for r in s.conn.execute(
                "SELECT id, provenance FROM insights WHERE batch_id = ?", (rbatch,)
            ).fetchall()
        }
        assert after == {ri1: "reflector", ri2: "reflector"}

        # A non-reflector label auto-stamps `manual`.
        hbatch = s.ensure_batch("by-hand-2")
        hi = _insight(s, 903, batch_id=hbatch, status="quarantined")
        lifecycle.auto_stamp_batch_provenance(s, "by-hand-2")
        assert s.get_insight(hi)["provenance"] == "manual"


# === required: provenance x world fitness grouping =============================


def test_provenance_world_fitness_grouping(store):
    """The maintenance fitness table groups by provenance x world; world is joined
    via fitness_events.episode_id -> episodes.world (required)."""
    i_manual = _insight(store, 1)
    i_research = _insight(store, 2)
    lifecycle.stamp_insight_provenance(store, i_research, "researched")

    ep_bf = _episode_with_world(store, "brownfield")
    ep_gf = _episode_with_world(store, "greenfield_backtranslated")

    def ev(insight, kind, *, episode_id, count=1):
        for _ in range(count):
            store.record_fitness_event(insight, kind, "training", 0, episode_id=episode_id)

    ev(i_manual, "retrieval", episode_id=ep_bf, count=2)
    ev(i_manual, "win", episode_id=ep_bf)
    # A NULL-episode event buckets under brownfield (a standalone run has no founder).
    ev(i_manual, "retrieval", episode_id=None)
    ev(i_research, "retrieval", episode_id=ep_gf, count=3)
    ev(i_research, "win", episode_id=ep_gf, count=2)
    ev(i_research, "retrieval", episode_id=ep_bf)
    ev(i_research, "loss", episode_id=ep_bf)

    table = m.provenance_world_fitness(store)
    by_key = {(r.provenance, r.world): r for r in table}

    # manual x brownfield: 2 (ep_bf) + 1 (NULL) retrievals, 1 win, 0 losses.
    mbf = by_key[("manual", "brownfield")]
    assert (mbf.retrievals, mbf.wins, mbf.losses, mbf.net) == (3, 1, 0, 1)
    # researched x greenfield: 3 retrievals, 2 wins.
    rgf = by_key[("researched", "greenfield_backtranslated")]
    assert (rgf.retrievals, rgf.wins, rgf.losses, rgf.net) == (3, 2, 0, 2)
    # the SAME researched insight splits into a losing brownfield bucket.
    rbf = by_key[("researched", "brownfield")]
    assert (rbf.retrievals, rbf.wins, rbf.losses, rbf.net) == (1, 0, 1, -1)
    # no spurious buckets.
    assert set(by_key) == {
        ("manual", "brownfield"),
        ("researched", "greenfield_backtranslated"),
        ("researched", "brownfield"),
    }
    # stable ordering (provenance, world).
    assert [(r.provenance, r.world) for r in table] == sorted(by_key)


def test_provenance_world_fitness_channel_filter(store):
    """trial/benchmark fitness never appears in the default training channel."""
    iid = _insight(store, 10)
    ep = _episode_with_world(store, "greenfield_backtranslated", mode="benchmark")
    store.record_fitness_event(iid, "retrieval", "benchmark", 0, episode_id=ep)
    store.record_fitness_event(iid, "win", "benchmark", 0, episode_id=ep)

    assert m.provenance_world_fitness(store) == []  # training channel: empty
    bench = m.provenance_world_fitness(store, mode="benchmark")
    assert len(bench) == 1
    assert (bench[0].provenance, bench[0].world) == ("manual", "greenfield_backtranslated")
    assert (bench[0].retrievals, bench[0].wins) == (1, 1)

    with pytest.raises(m.MaintenanceError):
        m.provenance_world_fitness(store, mode="bogus")


# === required: elicitation batch held pre-substrate ============================


# A post-bootstrap benchmark that would otherwise PROMOTE (candidate within limits).
_POST_BOOTSTRAP_HISTORY = tuple([0.90] * 12)


def _passing_bench():
    return v.BenchmarkOutcome(0.88, 0.02, _POST_BOOTSTRAP_HISTORY)


def test_elicitation_batch_quarantined_pre_substrate(store):
    """Before U13b's substrate exists an elicitation batch stays quarantined with a
    TYPED hold reason; it is never validated on the brownfield instrument alone
    (required)."""
    _, ids = _register_quarantined_batch(store, "reflect-ep-elicit")
    lifecycle.set_batch_validation_class(store, "reflect-ep-elicit", "elicitation")
    snap = store.current_snapshot_id()

    # greenfield_available defaults False -> held, default-deny, nothing written.
    with pytest.raises(v.SubstrateHeldError) as excinfo:
        v.validate_batch(
            store,
            batch_label="reflect-ep-elicit",
            snapshot_id=snap,
            benchmark=_passing_bench(),
            n_replay_pairs=20,
        )
    msg = str(excinfo.value)
    assert "greenfield" in msg
    assert "quarantined" in msg
    # the batch is untouched: still quarantined, no active members, no record.
    assert v.active_batch_insight_ids(store, "reflect-ep-elicit") == ()
    assert all(st == "quarantined" for st in _statuses(store, ids).values())

    # Once the substrate exists (U13b flips the flag) the same batch validates.
    outcome = v.validate_batch(
        store,
        batch_label="reflect-ep-elicit",
        snapshot_id=snap,
        benchmark=_passing_bench(),
        n_replay_pairs=20,
        greenfield_available=True,
    )
    assert outcome.promoted is True
    assert set(outcome.active_insight_ids) == set(ids)


def test_general_class_validates_pre_substrate(store):
    """A general/code batch keeps a valid brownfield instrument and is NOT held
    before the greenfield substrate exists (only elicitation load-bears on it)."""
    _, ids = _register_quarantined_batch(store, "reflect-ep-general")
    # default validation_class is 'general' (U1 backfill); validate without the
    # greenfield substrate.
    outcome = v.validate_batch(
        store,
        batch_label="reflect-ep-general",
        snapshot_id=store.current_snapshot_id(),
        benchmark=_passing_bench(),
        n_replay_pairs=20,
        greenfield_available=False,
    )
    assert outcome.promoted is True
    assert set(outcome.active_insight_ids) == set(ids)


# === substrate routing (pure function) =========================================


def test_route_substrates_per_class():
    """Each class routes to the substrates KTD5 specifies; the greenfield substrate
    is load-bearing only for elicitation."""
    gf, bf = v.GREENFIELD_SUBSTRATE, v.BROWNFIELD_SUBSTRATE

    # code: brownfield only, never held.
    for avail in (False, True):
        r = v.route_substrates("code", greenfield_available=avail)
        assert r.held is False and r.substrates == (bf,)

    # elicitation: held until greenfield exists, then both.
    held = v.route_substrates("elicitation", greenfield_available=False)
    assert held.held is True and held.substrates == ()
    assert "greenfield" in held.hold_reason
    ok = v.route_substrates("elicitation", greenfield_available=True)
    assert ok.held is False and ok.substrates == (gf, bf)

    # general: brownfield-only pre-substrate (not held), both once it exists.
    g0 = v.route_substrates("general", greenfield_available=False)
    assert g0.held is False and g0.substrates == (bf,)
    g1 = v.route_substrates("general", greenfield_available=True)
    assert g1.held is False and g1.substrates == (gf, bf)

    with pytest.raises(v.ValidateError):
        v.route_substrates("bogus", greenfield_available=True)


def test_route_batch_substrates_reads_stored_class(store):
    """route_batch_substrates reads validation_class off the stored batch row."""
    store.ensure_batch("b-elicit")
    lifecycle.set_batch_validation_class(store, "b-elicit", "elicitation")
    r = v.route_batch_substrates(store, "b-elicit", greenfield_available=False)
    assert r.held is True
    with pytest.raises(v.ValidateError):
        v.route_batch_substrates(store, "missing", greenfield_available=False)


# === lifecycle stamps: validation_class + enum guards ==========================


def test_set_batch_validation_class(store):
    """validation_class is settable at registration and reads back."""
    store.ensure_batch("b1")
    # default is 'general' (the U1 backfill default).
    assert store.conn.execute(
        "SELECT validation_class FROM batches WHERE label = 'b1'"
    ).fetchone()["validation_class"] == "general"
    lifecycle.set_batch_validation_class(store, "b1", "code")
    assert store.conn.execute(
        "SELECT validation_class FROM batches WHERE label = 'b1'"
    ).fetchone()["validation_class"] == "code"


def test_provenance_stamps_reject_unknown(store):
    """The stamps validate against the enums and fail loudly on a bad ref."""
    iid = _insight(store, 1)
    lifecycle.stamp_insight_provenance(store, iid, "seeded")
    assert store.get_insight(iid)["provenance"] == "seeded"

    with pytest.raises(LifecycleError):
        lifecycle.stamp_insight_provenance(store, iid, "bogus")
    with pytest.raises(LifecycleError):
        lifecycle.stamp_insight_provenance(store, 999999, "manual")  # unknown id
    store.ensure_batch("b")
    with pytest.raises(LifecycleError):
        lifecycle.set_batch_validation_class(store, "b", "bogus")
    with pytest.raises(LifecycleError):
        lifecycle.auto_stamp_batch_provenance(store, "b", "bogus")
    with pytest.raises(LifecycleError):
        lifecycle.set_batch_validation_class(store, "missing", "code")
