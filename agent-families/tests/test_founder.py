"""plan-007 U5: the founder simulator's degradation generator — deterministic
founder models over FEAT+DEC registries with target-side cached, entailment-linted
blur prose (R3, KTD3).

Fully offline: the blur judge and the entailment lint are injected as scripted
fakes (the improvement.DesignJudgeFn precedent), so the suite passes with zero
quota and no ``claude`` on PATH. The cache is exercised live-shaped (real
``founder_blur_cache`` rows), never fixture-masked.

## Conformance

Required acceptance tests (plan-007 U5) — invariant -> enforcing test:

- "same (registry snapshot, seed, params) -> byte-identical founder_models +
  founder_knowledge rows AND identical blur prose across two runs (cache path
  exercised live-shaped, not fixture-masked)"
  -> ``test_degradation_deterministic_including_blur`` (two episodes, same seed;
  identical state log + identical cached blur prose; the blur seam is NOT called
  again on the second run — the target-side cache serves it)
- "the stratification guard refuses an all-core-dropped draw (>=1 core JTBD-linked
  FEAT stays intact); guard-protected items are flagged for metric exclusion"
  -> ``test_core_loop_guard_refuses_all_core_dropped`` (drop_rate=1.0 drops every
  draw; >=1 core FEAT is rescued to intact, deterministically; guard_protected is
  exactly the core-loop set; non-core refs stay dropped)
- "a blur asserting a contradiction of its registry entry is rejected; an
  omitting/underselling blur passes; persistent lint failure falls back to dropped"
  -> ``test_blur_entailment_lint_rejects_contradiction`` (the contradicted entry
  regenerates max_blur_attempts times then falls back to dropped with NULL blur;
  an entailed entry stays blurred with prose)
- "a registry re-extraction (new entry_digest) misses the cache and regenerates"
  -> ``test_blur_cache_keyed_by_digest`` (changing a DEC's description changes its
  content digest; the second generate is a cache miss and re-calls the blur seam)

Unit test scenarios (plan-007 U5) -> tests:

- rates at 0 -> all intact -> ``test_zero_rates_all_intact``
- the deterministic vocab/leak arm of the blur lint
  -> ``test_blur_vocab_leak_rejects_distinctive_token``
- missing registry fails loudly -> ``test_missing_registry_fails_loudly``
- generation is once-per-episode -> ``test_generate_rejects_duplicate_episode``
- goal statement is always intact, derived from the core loop
  -> ``test_goal_statement_derived_from_core_loop``
- the live blur/entailment prompt builders are well-formed
  -> ``test_prompt_builders``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_families.config import GreenfieldConfig
from agent_families.grading.founder import (
    BlurRequest,
    FounderError,
    RegistryEntry,
    blur_vocab_leak,
    build_blur_prompt,
    build_entailment_prompt,
    degradation_params_hash,
    generate_founder_model,
    goal_statement_for,
    read_registry_entries,
    training_seed,
)
from agent_families.grading.registry import (
    DecisionCandidate,
    FeatureCandidate,
    refresh_decisions,
    refresh_registry,
)
from agent_families.store import Store

TARGET = "linkding"
DIGEST = "sha256:" + "ab" * 32


# --- helpers -------------------------------------------------------------------


class FakeBrowse:
    """Scripted browse channel: every confirm step observes ``ok`` with an a11y
    snapshot, so every candidate mints (the running-app confirmation half is not
    what U5 exercises)."""

    def __call__(self, step: dict) -> dict:
        return {
            "status": "ok",
            "a11y": f"snapshot:{step['action']}:{step.get('selector', '')}",
            "screenshot_ref": "shots/step.png",
        }


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "library.db")
    store.migrate()
    return store


def gconfig(**overrides) -> GreenfieldConfig:
    base = dict(
        drop_rate=0.2,
        blur_rate=0.3,
        core_loop_n=5,
        rotation_fraction=0.0,
        gate_k=2,
        grace_window=3,
        seed_occupancy_cap=15,
        benchmark_seed=1234,
    )
    base.update(overrides)
    return GreenfieldConfig(**base)


def feat(key: str, *, tier: str, outcome: str) -> FeatureCandidate:
    return FeatureCandidate(
        key=key,
        area="bookmarks",
        behavior=f"user can {key.replace('-', ' ')}",
        route="bookmarks/urls.py",
        confirm_steps=({"action": "goto", "selector": "", "args": {}},),
        scenario_steps=(f"Exercise {key}",),
        expected_outcome=outcome,
        tier=tier,
    )


def dec(key: str, *, description: str, category: str = "auth-gated") -> DecisionCandidate:
    return DecisionCandidate(
        key=key,
        category=category,
        description=description,
        route="bookmarks/models.py",
        confirm_steps=({"action": "goto", "selector": "", "args": {}},),
    )


def mint_feats(store: Store, tmp_path: Path, feats) -> None:
    refresh_registry(
        store, list(feats), FakeBrowse(), target=TARGET, digest=DIGEST,
        evidence_dir=tmp_path / "evidence",
    )


def mint_decs(store: Store, tmp_path: Path, decs) -> None:
    refresh_decisions(
        store, list(decs), FakeBrowse(), target=TARGET, digest=DIGEST,
        evidence_dir=tmp_path / "evidence",
    )


def make_episode(store: Store) -> int:
    cur = store.conn.execute(
        "INSERT INTO episodes (target, digest, snapshot_id, created_at, world)"
        " VALUES (?, ?, 0, '2026-06-10T00:00:00+00:00', 'greenfield_backtranslated')",
        (TARGET, DIGEST),
    )
    return int(cur.lastrowid)


class FakeBlur:
    """Scripted blur seam, recording which refs it generates for. The constant
    prose is JTBD-level and shares no distinctive token with the fixtures'
    descriptions, so it passes the deterministic vocab/leak arm."""

    def __init__(self, prose: str = "i only have a fuzzy sense of roughly that") -> None:
        self.prose = prose
        self.calls: list[str] = []

    def __call__(self, request: BlurRequest) -> str:
        self.calls.append(request.entry.ref_id)
        return self.prose


class FakeEntailment:
    """Scripted entailment seam: every blur is entailed except for refs named in
    ``contradict`` (those simulate a blur the registry refuses to entail)."""

    def __init__(self, contradict: tuple[str, ...] = ()) -> None:
        self.contradict = set(contradict)
        self.calls: list[str] = []

    def __call__(self, entry: RegistryEntry, blur: str) -> bool:
        self.calls.append(entry.ref_id)
        return entry.ref_id not in self.contradict


def _entails_all(entry: RegistryEntry, blur: str) -> bool:
    return True


# A standard fixture registry: three FEATs (two must = core, one should) + two
# DECs. Descriptions are distinctive and disjoint from FakeBlur's prose.
def seed_registry(store: Store, tmp_path: Path) -> None:
    mint_feats(
        store,
        tmp_path,
        [
            feat("bookmark-create", tier="must",
                 outcome="a freshly saved link appears in the collection"),
            feat("bookmark-search", tier="must",
                 outcome="typing filters which links remain visible"),
            feat("bookmark-tag", tier="should",
                 outcome="labels group related links together"),
        ],
    )
    mint_decs(
        store,
        tmp_path,
        [
            dec("auth-session-cookie",
                description="login relies on a browser cookie credential"),
            dec("delete-soft", category="core-crud",
                description="removed links stay recoverable for a grace window"),
        ],
    )


def knowledge_snapshot(store: Store, episode_id: int):
    rows = store.conn.execute(
        "SELECT fk.ref_kind, fk.ref_id, fk.state, bc.blur_text"
        " FROM founder_knowledge fk"
        " LEFT JOIN founder_blur_cache bc ON bc.id = fk.blur_id"
        " WHERE fk.episode_id = ? ORDER BY fk.ref_id",
        (episode_id,),
    ).fetchall()
    return [(r["ref_kind"], r["ref_id"], r["state"], r["blur_text"]) for r in rows]


# --- REQUIRED: determinism including blur (KTD3) -------------------------------


def test_degradation_deterministic_including_blur(tmp_path):
    """Same (registry snapshot, seed, params) -> byte-identical state log AND
    identical blur prose across two episodes; the target-side cache serves the
    second run without re-calling the blur seam (cache path live-shaped)."""
    store = make_store(tmp_path)
    seed_registry(store, tmp_path)
    # blur_rate=1.0 forces every ref to the blur path, maximally exercising the
    # cache; drop_rate=0.0 keeps the guard out of it.
    config = gconfig(drop_rate=0.0, blur_rate=1.0)
    blur = FakeBlur()
    entail = FakeEntailment()

    ep1 = make_episode(store)
    m1 = generate_founder_model(
        store, episode_id=ep1, target=TARGET, seed=7, config=config,
        blur_fn=blur, entailment_fn=entail,
    )
    calls_after_first = list(blur.calls)
    assert calls_after_first, "every ref should have hit the blur seam once"

    ep2 = make_episode(store)
    m2 = generate_founder_model(
        store, episode_id=ep2, target=TARGET, seed=7, config=config,
        blur_fn=blur, entailment_fn=entail,
    )

    # the blur seam was NOT called again — the cache served the second run
    assert blur.calls == calls_after_first

    # byte-identical state log + blur prose (modulo the episode key)
    assert knowledge_snapshot(store, ep1) == knowledge_snapshot(store, ep2)
    # every ref ended blurred with prose
    snap = knowledge_snapshot(store, ep1)
    assert {row[2] for row in snap} == {"blurred"}
    assert all(row[3] for row in snap)

    # the model objects agree on the degradation-bearing fields
    assert m1.seed == m2.seed
    assert m1.params_hash == m2.params_hash
    assert m1.goal_statement == m2.goal_statement
    assert m1.guard_protected == m2.guard_protected
    assert [(k.ref_id, k.state, k.blur_text) for k in m1.knowledge] == [
        (k.ref_id, k.state, k.blur_text) for k in m2.knowledge
    ]


# --- REQUIRED: stratification guard (KTD3) -------------------------------------


def test_core_loop_guard_refuses_all_core_dropped(tmp_path):
    """drop_rate=1.0 drops every draw; the guard rescues >=1 core FEAT to intact
    (deterministically), guard_protected is exactly the core-loop set, and
    non-core refs stay dropped."""
    store = make_store(tmp_path)
    seed_registry(store, tmp_path)
    # core_loop_n=2 -> the two must-tier FEATs are the core loop.
    config = gconfig(drop_rate=1.0, blur_rate=0.0, core_loop_n=2)
    blur = FakeBlur()
    entail = FakeEntailment()

    ep = make_episode(store)
    model = generate_founder_model(
        store, episode_id=ep, target=TARGET, seed=3, config=config,
        blur_fn=blur, entailment_fn=entail,
    )

    core = {"FEAT-bookmark-create", "FEAT-bookmark-search"}
    assert set(model.guard_protected) == core

    states = {k.ref_id: k.state for k in model.knowledge}
    # >=1 core FEAT stays non-dropped (rescued to intact); the guard never lets
    # the whole core loop drop.
    rescued = [cid for cid in core if states[cid] != "dropped"]
    assert len(rescued) >= 1
    assert all(states[cid] == "intact" for cid in rescued)
    # every non-core ref is dropped (drop_rate=1.0, and they are not rescued)
    assert states["FEAT-bookmark-tag"] == "dropped"
    assert states["DEC-auth-session-cookie"] == "dropped"
    assert states["DEC-delete-soft"] == "dropped"
    # guard-protected flag rides the knowledge rows
    assert {k.ref_id for k in model.knowledge if k.guard_protected} == core
    # the blur seam never ran (no blurred refs)
    assert blur.calls == []

    # the rescue is deterministic: same seed -> same rescued ref
    ep2 = make_episode(store)
    model2 = generate_founder_model(
        store, episode_id=ep2, target=TARGET, seed=3, config=config,
        blur_fn=blur, entailment_fn=entail,
    )
    states2 = {k.ref_id: k.state for k in model2.knowledge}
    assert states == states2


# --- REQUIRED: blur entailment lint (KTD3) -------------------------------------


def test_blur_entailment_lint_rejects_contradiction(tmp_path):
    """A blur the registry refuses to entail regenerates max_blur_attempts times
    then falls back to dropped (NULL blur); an entailed blur stays blurred."""
    store = make_store(tmp_path)
    seed_registry(store, tmp_path)
    config = gconfig(drop_rate=0.0, blur_rate=1.0)
    blur = FakeBlur()
    # the DEC is non-core, so the guard cannot rescue it back to intact when it
    # falls to dropped — its drop is observable.
    entail = FakeEntailment(contradict=("DEC-delete-soft",))

    ep = make_episode(store)
    model = generate_founder_model(
        store, episode_id=ep, target=TARGET, seed=11, config=config,
        blur_fn=blur, entailment_fn=entail, max_blur_attempts=2,
    )
    states = {k.ref_id: k.state for k in model.knowledge}
    blurs = {k.ref_id: k.blur_text for k in model.knowledge}

    # persistent contradiction -> fell back to dropped, no blur spoken
    assert states["DEC-delete-soft"] == "dropped"
    assert blurs["DEC-delete-soft"] is None
    # it was regenerated max_blur_attempts times before giving up
    assert blur.calls.count("DEC-delete-soft") == 2

    # an entailed (omitting) blur passes and stays blurred with prose
    assert states["DEC-auth-session-cookie"] == "blurred"
    assert blurs["DEC-auth-session-cookie"]

    # the dropped-decision is cached so it reproduces deterministically
    cached = store.conn.execute(
        "SELECT lint_verdict FROM founder_blur_cache WHERE ref_id = 'DEC-delete-soft'"
    ).fetchone()
    assert cached["lint_verdict"] == "drop"


# --- REQUIRED: cache keyed by content digest (KTD3) ----------------------------


def test_blur_cache_keyed_by_digest(tmp_path):
    """A registry re-extraction that changes a DEC's description changes its
    content digest, so the blur cache misses and the blur seam re-runs."""
    store = make_store(tmp_path)
    mint_decs(
        store, tmp_path,
        [dec("delete-soft", category="core-crud",
             description="removed links stay recoverable for a grace window")],
    )
    # one FEAT so the registry is non-empty and the guard has a core item
    mint_feats(
        store, tmp_path,
        [feat("bookmark-create", tier="must",
              outcome="a freshly saved link appears in the collection")],
    )
    config = gconfig(drop_rate=0.0, blur_rate=1.0)
    blur = FakeBlur()
    entail = FakeEntailment()

    ep1 = make_episode(store)
    generate_founder_model(
        store, episode_id=ep1, target=TARGET, seed=5, config=config,
        blur_fn=blur, entailment_fn=entail,
    )
    first = blur.calls.count("DEC-delete-soft")
    assert first == 1

    # re-extract the SAME DEC with a different description -> new content digest
    mint_decs(
        store, tmp_path,
        [dec("delete-soft", category="core-crud",
             description="trashed links can be restored within a short retention")],
    )

    ep2 = make_episode(store)
    generate_founder_model(
        store, episode_id=ep2, target=TARGET, seed=5, config=config,
        blur_fn=blur, entailment_fn=entail,
    )
    # cache MISS on the changed entry -> the blur seam re-ran for it
    assert blur.calls.count("DEC-delete-soft") == first + 1
    # and a second cache row exists under the new digest
    rows = store.conn.execute(
        "SELECT COUNT(*) AS n FROM founder_blur_cache WHERE ref_id = 'DEC-delete-soft'"
    ).fetchone()
    assert rows["n"] == 2


# --- unit scenarios ------------------------------------------------------------


def test_zero_rates_all_intact(tmp_path):
    """drop_rate=0 and blur_rate=0 -> every ref is intact; the blur seam is never
    touched."""
    store = make_store(tmp_path)
    seed_registry(store, tmp_path)
    config = gconfig(drop_rate=0.0, blur_rate=0.0)
    blur = FakeBlur()

    ep = make_episode(store)
    model = generate_founder_model(
        store, episode_id=ep, target=TARGET, seed=1, config=config,
        blur_fn=blur, entailment_fn=_entails_all,
    )
    assert {k.state for k in model.knowledge} == {"intact"}
    assert all(k.blur_text is None for k in model.knowledge)
    assert blur.calls == []


def test_blur_vocab_leak_rejects_distinctive_token():
    """The deterministic lint arm: a blur echoing a registry-distinctive token
    leaks; a generic JTBD-level paraphrase does not."""
    entry = RegistryEntry(
        ref_kind="dec", ref_id="DEC-delete-soft",
        description="removed links stay recoverable for a grace window",
        entry_digest="x", tier="", is_core=False,
    )
    # "recoverable" is distinctive (>=4 chars, not generic) -> leak
    assert blur_vocab_leak(entry, "stuff stays recoverable somehow") == "recoverable"
    # a generic paraphrase shares no distinctive token -> no leak
    assert blur_vocab_leak(entry, "i vaguely recall something about that") is None


def test_missing_registry_fails_loudly(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(FounderError, match="no confirmed registry entries"):
        read_registry_entries(store, TARGET, core_loop_n=5)


def test_generate_rejects_duplicate_episode(tmp_path):
    store = make_store(tmp_path)
    seed_registry(store, tmp_path)
    config = gconfig(drop_rate=0.0, blur_rate=0.0)
    ep = make_episode(store)
    generate_founder_model(
        store, episode_id=ep, target=TARGET, seed=1, config=config,
        blur_fn=FakeBlur(), entailment_fn=_entails_all,
    )
    with pytest.raises(FounderError, match="already exists"):
        generate_founder_model(
            store, episode_id=ep, target=TARGET, seed=1, config=config,
            blur_fn=FakeBlur(), entailment_fn=_entails_all,
        )


def test_goal_statement_derived_from_core_loop(tmp_path):
    """The goal statement is always intact and derives from the core-loop FEATs'
    behaviors, regardless of their degradation state."""
    store = make_store(tmp_path)
    seed_registry(store, tmp_path)
    # even with everything dropped, the goal is intact and mentions a core behavior
    config = gconfig(drop_rate=1.0, blur_rate=0.0, core_loop_n=2)
    ep = make_episode(store)
    model = generate_founder_model(
        store, episode_id=ep, target=TARGET, seed=9, config=config,
        blur_fn=FakeBlur(), entailment_fn=_entails_all,
    )
    assert model.goal_statement.startswith(f"I want a {TARGET} that lets me:")
    assert "saved link appears" in model.goal_statement  # a core FEAT behavior

    # the persisted founder_models row carries it
    row = store.conn.execute(
        "SELECT goal_statement, seed, params_hash FROM founder_models"
        " WHERE episode_id = ?",
        (ep,),
    ).fetchone()
    assert row["goal_statement"] == model.goal_statement
    assert row["seed"] == 9
    assert row["params_hash"] == degradation_params_hash(config)


def test_goal_statement_for_empty_core():
    assert "does its core job" in goal_statement_for("acme", [])


def test_training_seed_is_episode_id():
    assert training_seed(42) == 42
    with pytest.raises(FounderError):
        training_seed(-1)


def test_prompt_builders():
    entry = RegistryEntry(
        ref_kind="dec", ref_id="DEC-delete-soft",
        description="removed links stay recoverable for a grace window",
        entry_digest="x", tier="", is_core=False,
    )
    blur_prompt = build_blur_prompt(
        BlurRequest(entry=entry, attempt=0, goal_statement="I want a notes app")
    )
    assert "JTBD" in blur_prompt
    assert "removed links stay recoverable" in blur_prompt
    assert "I want a notes app" in blur_prompt
    # a retry attempt mentions the prior failures
    retry_prompt = build_blur_prompt(
        BlurRequest(entry=entry, attempt=2, goal_statement="g")
    )
    assert "failed the lint" in retry_prompt

    entail_prompt = build_entailment_prompt(entry, "stuff gets cleaned up eventually")
    assert "entails" in entail_prompt.lower()
    assert "removed links stay recoverable" in entail_prompt
    assert "stuff gets cleaned up eventually" in entail_prompt
