"""Family router + routing-decision log tests (plan-005 U4, R12).

Fully offline: the routing LLM call goes through an injected ``judge_fn`` fake, the
store is exercised directly, and no embedder / subprocess / quota is touched.

## Conformance

plan-005 U4 router test scenario -> test:

- routing decisions logged with full candidates:
  ``test_routing_decision_is_logged_with_candidates``
- the chosen agent's routing_decisions counter increments (feeds the split gate):
  ``test_route_increments_chosen_agent_counter``
- a lone generic agent routes-to-self and still accrues decisions:
  ``test_single_candidate_routes_to_self_and_logs``
- low confidence flags the decision ambiguous (the R14c boundary trigger):
  ``test_low_confidence_decision_is_ambiguous``
- replay re-routes a logged request over a new candidate set, deterministically:
  ``test_route_request_over_candidates_is_deterministic_seam``
- a retired-by-split parent drops out of the candidate set:
  ``test_retired_and_pending_parents_excluded_from_candidates``
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_families.library import router
from agent_families.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


class _FakeResult:
    def __init__(self, output: dict) -> None:
        self.output = output


def make_judge(choose):
    """A judge seam fake: ``choose(prompt) -> (chosen_name, confidence)``."""

    calls = []

    def judge(prompt, schema, model, *, max_retries, mode=None, fixtures_dir=None):
        calls.append(prompt)
        name, conf = choose(prompt)
        return _FakeResult({"chosen_agent": name, "confidence": conf})

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def _two_specialists(store: Store):
    fam = store.create_family("worker", charter="builds the app")
    tagger = store.create_agent(
        fam, "tagger", description="organizes bookmarks with tags"
    )
    searcher = store.create_agent(
        fam, "searcher", description="full-text search over bookmarks"
    )
    return fam, tagger, searcher


# --- R12: decisions logged with full candidates --------------------------------


def test_routing_decision_is_logged_with_candidates(store):
    fam, tagger, searcher = _two_specialists(store)
    judge = make_judge(lambda p: ("tagger", 0.9))

    decision = router.route(
        store, family_id=fam, request_text="add a tag to this bookmark",
        judge_fn=judge,
    )

    assert decision.chosen_agent_id == tagger
    assert decision.decision_id is not None
    rows = router.logged_decisions(store, family_id=fam)
    assert len(rows) == 1
    row = rows[0]
    assert row["chosen_agent_id"] == tagger
    assert row["request_text"] == "add a tag to this bookmark"
    # The full candidate set is logged (both specialists, with ids + names).
    candidates = json.loads(row["candidates_json"])
    assert {c["name"] for c in candidates} == {"tagger", "searcher"}
    assert {c["agent_id"] for c in candidates} == {tagger, searcher}
    assert row["confidence"] == pytest.approx(0.9)


def test_route_increments_chosen_agent_counter(store):
    fam, tagger, searcher = _two_specialists(store)
    judge = make_judge(lambda p: ("searcher", 0.95))

    assert router.routing_decision_count(store, searcher) == 0
    for _ in range(3):
        router.route(
            store, family_id=fam, request_text="search bookmarks for python",
            judge_fn=judge,
        )
    # Only the chosen agent's counter moves — the split gate's input.
    assert router.routing_decision_count(store, searcher) == 3
    assert router.routing_decision_count(store, tagger) == 0


# --- R12: the lone generic agent routes-to-self --------------------------------


def test_single_candidate_routes_to_self_and_logs(store):
    fam = store.create_family("solo")
    generic = store.create_agent(fam, "generalist", description="does everything")
    judge = make_judge(lambda p: (_ for _ in ()).throw(AssertionError("no judge!")))

    decision = router.route(
        store, family_id=fam, request_text="do the thing", judge_fn=judge,
    )

    # One candidate: chosen trivially, confidence 1.0, NO judge call...
    assert decision.chosen_agent_id == generic
    assert decision.confidence == 1.0
    assert decision.ambiguous is False
    assert judge.calls == []  # type: ignore[attr-defined]
    # ...but it still logs and increments, so the lone agent accrues decisions
    # toward its split gate.
    assert len(router.logged_decisions(store, family_id=fam)) == 1
    assert router.routing_decision_count(store, generic) == 1


# --- R12 / R14c: low confidence is ambiguous -----------------------------------


def test_low_confidence_decision_is_ambiguous(store):
    fam, tagger, searcher = _two_specialists(store)
    # A confident route is not ambiguous...
    confident = router.route(
        store, family_id=fam, request_text="tag it", judge_fn=make_judge(
            lambda p: ("tagger", 0.92)
        ),
        ambiguity_confidence_threshold=0.6,
    )
    assert confident.ambiguous is False
    # ...a low-confidence route is (a boundary-ticket trigger, R14c).
    ambiguous = router.route(
        store, family_id=fam, request_text="tag and search it", judge_fn=make_judge(
            lambda p: ("tagger", 0.4)
        ),
        ambiguity_confidence_threshold=0.6,
    )
    assert ambiguous.ambiguous is True
    rows = router.logged_decisions(store, family_id=fam)
    assert rows[-1]["ambiguous"] == 1


# --- R13 substrate: the replay routing primitive -------------------------------


def test_route_request_over_candidates_is_deterministic_seam(store):
    fam, tagger, searcher = _two_specialists(store)
    cands = router.family_candidates(store, fam)

    def choose(prompt):
        return ("searcher", 0.99) if "search" in prompt else ("tagger", 0.99)

    judge = make_judge(choose)
    # The log-free primitive routing replay drives: same request -> same choice,
    # no log row, no counter move.
    chosen = router.route_request_over_candidates(
        "please search the bookmarks", cands, judge_fn=judge,
    )
    assert chosen == searcher
    assert router.logged_decisions(store, family_id=fam) == []
    assert router.routing_decision_count(store, searcher) == 0


def test_retired_and_pending_parents_excluded_from_candidates(store):
    fam, tagger, searcher = _two_specialists(store)
    # A split parent is unroutable both while pending and once retired.
    store.conn.execute(
        "UPDATE agents SET lineage_status = 'split_pending' WHERE id = ?", (tagger,)
    )
    names = {c.name for c in router.family_candidates(store, fam)}
    assert names == {"searcher"}
    store.conn.execute(
        "UPDATE agents SET lineage_status = 'retired' WHERE id = ?", (tagger,)
    )
    names = {c.name for c in router.family_candidates(store, fam)}
    assert names == {"searcher"}


def test_route_with_no_candidates_raises(store):
    fam = store.create_family("empty")
    with pytest.raises(router.RouterError, match="no routable agents"):
        router.route(store, family_id=fam, request_text="x", judge_fn=make_judge(
            lambda p: ("none", 1.0)
        ))


def test_request_hash_is_stable_and_candidate_order_independent():
    h1 = router.request_hash("do X", ["b", "a"])
    h2 = router.request_hash("do X", ["a", "b"])
    assert h1 == h2  # candidate order does not change the request identity
    assert router.request_hash("do Y", ["a", "b"]) != h1
