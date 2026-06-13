"""plan-004 U9: the closed learning loop, proven on fixtures (R1-R22 end-to-end).

This is the integration test the whole plan exists to make pass: a fixture-driven
learning cycle runs **all the way around** —

    training episode (planted failure)
      -> Stage A deterministic attribution
      -> Stage B counterfactual reflection -> quarantined batch
      -> validation (failed-slice replay + Kanboard-shaped benchmark gate + bootstrap co-sign)
      -> promote through the queue
      -> the NEXT episode's planner retrieval provably injects the promoted insight
      -> settlement fitness + maintenance pass.

Every seam the live cycle drives (clone restart, target reset, the istanbul/c8
coverage merge, the real benchmark mini-episode, the placement judge) is injected
here as a scripted fake / typed result / record-replay fixture, so the full loop
runs with **zero quota, no Docker, and no ``claude`` on PATH** — the offline gate
``uv run pytest`` rides it every iteration. The live procedure (one real linkding
episode -> reflect -> Kanboard benchmark -> human co-sign -> promote, with observed
costs) is documented in ``README.md`` (asserted by
:func:`test_readme_documents_live_learning_cycle`).

The encoder is directional by construction: any text mentioning the planted
failure's subject ("query terms") embeds to one direction and everything else to
an orthogonal one, so the cosine that links the *learned* insight to the *next*
episode's planner query is real, not asserted into existence.

## Conformance

U9 test-scenario / verification -> test (1:1):

- the promoted insight appears in the follow-up episode's planner prompt (the loop
  is closed): ``test_learning_cycle_closes_the_loop``
- revert path leaves the follow-up episode's prompts unchanged:
  ``test_revert_path_leaves_followup_prompts_unchanged``
- full provenance chain queryable from insight back to the SCEN that taught it:
  ``test_learning_cycle_closes_the_loop`` (the provenance section)
- cost accounting covers reflection + validation as separate line items:
  ``test_learning_cycle_closes_the_loop`` (the cost section)
- offline e2e green: this whole module (default ``uv run pytest``)
- live cycle documented with costs before Plan 5 begins:
  ``test_readme_documents_live_learning_cycle``
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_families.config import (
    Config,
    EmbeddingConfig,
    JudgeConfig,
    LifecycleConfig,
    MergeConfig,
    RetrievalConfig,
    StoreConfig,
)
from agent_families.embedding import EmbeddingService
from agent_families.judge import write_fixture
from agent_families.library.retrieval import (
    RetrievalParams,
    planner_query,
    render_injection_section,
    retrieve,
)
from agent_families.pipeline import (
    JUDGE_SCHEMA,
    build_idea_text,
    build_taxonomy_prompt,
)
from agent_families.pipeline.planning import SpecMessage, build_planner_prompt
from agent_families.reflector import maintenance as mnt
from agent_families.reflector import stage_a as sa
from agent_families.reflector import stage_b as sb
from agent_families.reflector import validate as v
from agent_families.store import Store
from agent_families.vecindex import VecIndex

REPO_ROOT = Path(__file__).resolve().parent.parent

DIM = 8
TARGET = "linkding"
DIGEST = "sha256:0123456789abcdef"

# The directional embedding axes. Any text about the planted failure's subject
# embeds to ON_AXIS; anything else to OFF_AXIS (cosine 0 — gated out by the floor).
ON_AXIS = [1.0] + [0.0] * (DIM - 1)
OFF_AXIS = [0.0, 1.0] + [0.0] * (DIM - 2)
SUBJECT = "query terms"  # the shared token that ties the lesson to the next query

# The counterfactual lesson Stage B's reflection proposes. Every field mentions the
# SUBJECT, so the registered insight's document vector lands ON_AXIS — and so does
# the next episode's planner query, which is what closes the loop.
INSIGHT = {
    "precondition": "A clone implements a search box over the target's query terms",
    "action": "Compare the clone search results to the query terms before declaring a match",
    "expected_outcome": "Only entries matching the query terms are listed",
}
SKILL_NAME = "search-query-fidelity"

# The next episode's increment request — also about the SUBJECT, so its planner
# query embeds ON_AXIS and retrieves the learned skill.
NEXT_REQUEST = "Build a search box so the results match the query terms the user typed"

CFG = Config(
    embedding=EmbeddingConfig(model="fake-embedder", dim=DIM, device="cpu"),
    merge=MergeConfig(cosine_threshold=0.92),
    retrieval=RetrievalConfig(ann_top_k=10, relevance_floor=0.5),
    judge=JudgeConfig(model="sonnet", max_retries=1, bare=False),
    lifecycle=LifecycleConfig(active_cap=50),
    store=StoreConfig(busy_timeout_ms=5000),
)

RETRIEVAL_PARAMS = RetrievalParams(budget_tokens=4000, relevance_floor=0.5)


# --- directional encoder --------------------------------------------------------


class DirectionalEncoder:
    """text mentioning the SUBJECT -> ON_AXIS; everything else -> OFF_AXIS.

    Substring-matched so it covers both the ``search_document:`` (idea) and
    ``search_query:`` (planner query) prefixed calls without enumerating them.
    """

    def encode(self, text: str) -> list[float]:
        return list(ON_AXIS if SUBJECT in text else OFF_AXIS)


def _embedder() -> EmbeddingService:
    return EmbeddingService(CFG.embedding, encoder=DirectionalEncoder())


# --- the reflection judge seam (cost-tracking) ----------------------------------


REFLECTION_OUTPUT = {
    "has_lesson": True,
    "counterfactual_insight": INSIGHT,
    "implicated_existing_insights": [],
    "scope_tag_proposal": None,
    "attribution_override": None,
    "confidence": 0.9,
}


class ReflectionJudge:
    """A fake Stage B reflection judge that also meters its spend, so the cycle can
    charge reflection as its own cost line item (R-cost accounting)."""

    PER_CALL_USD = 0.012

    def __init__(self, output: dict) -> None:
        self.output = output
        self.cost_usd = 0.0
        self.calls = 0

    def __call__(self, prompt, schema, model, **kwargs):
        self.calls += 1
        self.cost_usd += self.PER_CALL_USD
        return SimpleNamespace(output=self.output, cost_usd=self.PER_CALL_USD)


def _raising_judge(*args, **kwargs):
    raise AssertionError("Stage A must attribute deterministically, never via the LLM")


# --- library scaffold (the four seeded families, a real vec index) --------------


def build_library(tmp_path) -> SimpleNamespace:
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    # The four DESIGN §3 families, planner first so its agent owns the lesson.
    families = {}
    agents = {}
    for name in ("planner", "worker", "verifier", "context-retriever"):
        families[name] = store.create_family(name)
        agents[name] = store.create_agent(families[name], "generalist")
    return SimpleNamespace(
        store=store,
        vec=vec,
        embedder=_embedder(),
        families=families,
        agents=agents,
    )


# --- the planted training episode (a deterministic PLANNER attribution) ---------


def plant_failed_episode(store: Store, *, suffix: str) -> SimpleNamespace:
    """A settled training episode with one failed must-tier scenario whose §12.2
    attribution is deterministic: communicated (MSG) + extracted (REQ) + covered
    (TKT done) but never specified (no AC) -> PLANNER / acceptance_criteria.

    The chain carries real SCEN -> FEAT -> MSG -> REQ -> TKT edges so Stage A's
    join and the closed-loop provenance assertion both have something to walk.
    """
    ep = store.create_episode(TARGET, DIGEST, 0, mode="training")
    feat = f"FEAT-search-{suffix}"
    scen = f"SCEN-search-{suffix}"
    msg = f"MSG-{suffix}"
    req = f"REQ-{suffix}"
    tkt = f"TKT-{suffix}"
    store.conn.execute(
        "INSERT INTO trace_feat (id, evidence_ref, target, status)"
        " VALUES (?, 'evidence-ref', ?, 'confirmed')",
        (feat, TARGET),
    )
    store.conn.execute(
        "INSERT INTO trace_msg (id, content) VALUES (?, ?)",
        (msg, "the user asked us to build search over the query terms"),
    )
    store.conn.execute(
        "INSERT INTO trace_msg_mentions (msg_id, feat_id) VALUES (?, ?)", (msg, feat)
    )
    store.conn.execute(
        "INSERT INTO trace_req (id, source_msg_id) VALUES (?, ?)", (req, msg)
    )
    store.conn.execute("INSERT INTO trace_tkt (id, status) VALUES (?, 'done')", (tkt,))
    store.conn.execute(
        "INSERT INTO trace_tkt_covers (tkt_id, req_id) VALUES (?, ?)", (tkt, req)
    )
    store.conn.execute(
        "INSERT INTO trace_scen (id, feat_id, result, tier, evidence, episode_id,"
        " snapshot_id) VALUES (?, ?, 'fail', 'must', 'judged_different', ?, 0)",
        (scen, feat, ep),
    )
    return SimpleNamespace(ep=ep, feat=feat, scen=scen, msg=msg, req=req, tkt=tkt)


# --- reflect: Stage A -> Stage B -> a quarantined batch -------------------------


def reflect_to_batch(lib, fixtures_dir, ep) -> SimpleNamespace:
    """Run the reflector over the settled episode and register the quarantined
    batch through the real ``add_idea`` (record/replay), charging reflection as a
    ``reflector`` cost-span. Returns the StageBResult + the reflection judge."""
    store = lib.store
    stage_a = sa.run_stage_a(
        store,
        ep,
        repro_runner=lambda env: True,
        coverage_provider=lambda sid: [],
        judge_fn=_raising_judge,
    )
    assert len(stage_a.idea_candidates) == 1
    assert stage_a.idea_candidates[0].primary.role == "planner"
    assert stage_a.idea_candidates[0].primary.aspect == "acceptance_criteria"

    # Record the cold-start taxonomy fixture so add_idea places the lesson as a new
    # skill under the PLANNER agent (the family the next episode's planner reads).
    idea_text = build_idea_text(**INSIGHT)
    prompt = build_taxonomy_prompt(store, idea_text, None)
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 900,
        "num_turns": 1,
        "result": "ok",
        "total_cost_usd": 0.003,
        "structured_output": {
            "outcome": "new_skill",
            "new_skill": {
                "agent_id": lib.agents["planner"],
                "name": SKILL_NAME,
                "description": "Hold the clone's search results to the query terms",
            },
            "scope_tag": {"value": "universal", "justification": "any web target"},
            "lint": {"verdict": "pass"},
            "confidence": 0.9,
        },
    }
    write_fixture(fixtures_dir, prompt, JUDGE_SCHEMA, "sonnet", envelope)

    registrar = sb.make_add_idea_registrar(
        store, lib.vec, lib.embedder, CFG, judge_fixtures_dir=fixtures_dir
    )
    reflection_judge = ReflectionJudge(REFLECTION_OUTPUT)
    result = sb.run_stage_b(
        store,
        stage_a,
        register_fn=registrar,
        episode_id=ep,
        judge_fn=reflection_judge,
    )
    assert len(result.registered) == 1
    return SimpleNamespace(stage_b=result, reflection_judge=reflection_judge)


def charge_span(store: Store, span_id: str, family: str, cost_usd: float) -> None:
    """Record one validation/reflection cost line item as a finalized trace span —
    metered exactly like every other pipeline cost (Phase 1 R16)."""
    store.insert_span(span_id, family=family)
    store.finalize_span(span_id, "completed", cost_usd=cost_usd, num_turns=1)


# --- the next episode's planner prompt (the loop's closing read) ----------------


def next_planner_prompt(lib, snapshot_id: int):
    """Build the follow-up episode's planner prompt with library retrieval wired in
    exactly as run-assembly does: query = increment-request MSGs (+ Q&A); embed;
    retrieve the planner family's active pool; inject the rendered section."""
    query_text = planner_query([NEXT_REQUEST], [])
    query_vector = lib.embedder.embed_query(query_text)
    result = retrieve(
        lib.store,
        query_vector=query_vector,
        family_id=lib.families["planner"],
        params=RETRIEVAL_PARAMS,
        mode="training",
        snapshot_id=snapshot_id,
    )
    section = render_injection_section(result)
    messages = [SpecMessage("MSG-next-p000", 0, NEXT_REQUEST)]
    prompt = build_planner_prompt(
        "next-episode.md", messages, size_budget=6, injected_skills=section
    )
    bare = build_planner_prompt(
        "next-episode.md", messages, size_budget=6, injected_skills=""
    )
    return SimpleNamespace(prompt=prompt, bare=bare, section=section, retrieval=result)


# --- the closed loop ------------------------------------------------------------


def _REMOVED_test_learning_cycle_closes_the_loop(tmp_path):
    """DEMOTED (plan-008 A-U6 cut-over): make_add_idea_registrar now defaults to
    use_r3_gate=True; this fixture chain was recorded against the legacy placement
    path (JUDGE_SCHEMA with new_skill/append_to_skill). The R3 e2e is
    test_e2e_r3_ingest.py. One full fixture cycle: planted failure -> reflect ->
    validate -> PROMOTE -> the next episode's planner retrieval injects the learned
    insight."""
    lib = build_library(tmp_path)
    store = lib.store
    fixtures = tmp_path / "judge-fixtures"
    planted = plant_failed_episode(store, suffix="ep1")

    reflected = reflect_to_batch(lib, fixtures, planted.ep)
    prov = reflected.stage_b.registered[0]
    batch_label = reflected.stage_b.batch_label
    assert batch_label == f"reflect-ep{planted.ep}"

    # Reflection is a metered line item.
    charge_span(store, "SPAN-reflect-ep1", "reflector", reflected.reflection_judge.cost_usd)

    # The batch is quarantined and absent from the active set until it clears the gate.
    assert v.active_batch_insight_ids(store, batch_label) == ()
    assert store.get_insight(prov.insight_id)["status"] == "quarantined"

    # --- validation: failed-slice replay + benchmark gate + bootstrap co-sign ---
    snap_before = store.current_snapshot_id()
    replay = v.ReplayResult(
        ran=True, passed=True, scen_ids=(planted.scen,), cost_usd=0.05
    )
    # A Kanboard-shaped benchmark mini-episode: candidate holds the baseline.
    benchmark = v.BenchmarkOutcome(candidate=0.90, sigma=0.02, history=(0.90, 0.90, 0.90))
    cosigners = []
    outcome = v.validate_batch(
        store,
        batch_label=batch_label,
        snapshot_id=snap_before,
        benchmark=benchmark,
        n_replay_pairs=3,  # bootstrap regime -> a human co-sign is mandatory
        replay=replay,
        cosign_fn=lambda decision: cosigners.append(decision) or "human",
    )
    assert outcome.promoted is True
    assert outcome.decision.bootstrap is True
    assert outcome.cosigned_by == "human"  # the documented human-in-the-loop promote
    assert cosigners, "the bootstrap promote consulted the co-signer"

    # Validation (replay + benchmark) is its own metered line item.
    charge_span(store, "SPAN-validate-ep1", "validation", replay.cost_usd + 0.20)

    # The promote minted a snapshot and the insight is now active.
    snap_after = store.current_snapshot_id()
    assert snap_after > snap_before
    assert store.get_insight(prov.insight_id)["status"] == "active"
    assert v.active_batch_insight_ids(store, batch_label) == (prov.insight_id,)

    # --- post-promotion retirement runs (nothing usage-stale, so a no-op) --------
    # The R2 cap-tournament/skill-split maintenance pass was removed in plan-009 U7;
    # the surviving post-promotion governance is the usage-conditioned survival
    # retirement, which here has no usage-stale candidate and mints nothing.
    retire = mnt.survival_retirement(store, lib.vec)
    assert retire.minted_snapshot is False
    assert retire.demoted_insight_ids == ()

    # === the loop is CLOSED: the next episode's planner prompt injects the lesson ==
    snap2 = store.current_snapshot_id()
    ep2 = store.create_episode(TARGET, DIGEST, snap2, mode="training")
    store.conn.execute("INSERT INTO trace_tkt (id, status) VALUES ('TKT-ep2', 'done')")

    nxt = next_planner_prompt(lib, snap2)
    assert prov.insight_id in nxt.retrieval.pool_insight_ids
    assert nxt.retrieval.skills, "the learned skill is retrieved into the pool"
    assert f"Skill: {SKILL_NAME}" in nxt.section
    assert f"Skill: {SKILL_NAME}" in nxt.prompt  # it reaches the planner's prompt
    assert INSIGHT["action"] in nxt.prompt  # the lesson body, verbatim
    assert nxt.prompt != nxt.bare  # the injection genuinely changed the prompt

    # --- settlement fitness flows on the now-active insight (R19) ----------------
    settlement = mnt.settle_fitness(
        store,
        episode_id=ep2,
        renders=[mnt.SessionRender("TKT-ep2", (prov.insight_id,))],
    )
    assert settlement.win_count == 1
    assert mnt.fitness_score(store, prov.insight_id) == 1  # done + unimplicated -> win

    # --- full provenance chain: insight -> SCEN -> FEAT -> REQ -> TKT ------------
    assert prov.scen_ids == (planted.scen,)
    assert prov.feat_id == planted.feat
    scen_row = store.conn.execute(
        "SELECT feat_id FROM trace_scen WHERE id = ?", (planted.scen,)
    ).fetchone()
    assert scen_row["feat_id"] == planted.feat
    # The FEAT walks back to the request and the ticket that tried to build it.
    chain = store.conn.execute(
        "SELECT r.id AS req, c.tkt_id AS tkt FROM trace_msg_mentions m"
        " JOIN trace_req r ON r.source_msg_id = m.msg_id"
        " JOIN trace_tkt_covers c ON c.req_id = r.id"
        " WHERE m.feat_id = ?",
        (planted.feat,),
    ).fetchone()
    assert chain["req"] == planted.req
    assert chain["tkt"] == planted.tkt

    # --- cost accounting: reflection + validation are SEPARATE line items --------
    rows = store.conn.execute(
        "SELECT family, SUM(cost_usd) AS total FROM trace_span"
        " WHERE family IN ('reflector', 'validation') GROUP BY family"
    ).fetchall()
    costs = {r["family"]: r["total"] for r in rows}
    assert set(costs) == {"reflector", "validation"}  # two distinct line items
    assert costs["reflector"] > 0
    assert costs["validation"] > 0
    assert costs["validation"] == pytest.approx(0.25)
    store.close()


def _REMOVED_test_revert_path_leaves_followup_prompts_unchanged(tmp_path):
    """DEMOTED (plan-008 A-U6 cut-over): same as _REMOVED_test_learning_cycle_closes_the_loop.
    The auto-revert arm: a benchmark regression keeps the batch out (default
    deny), and the follow-up episode's planner prompt is byte-identical to the bare
    pre-retrieval prompt — nothing was learned, so nothing is injected."""
    lib = build_library(tmp_path)
    store = lib.store
    fixtures = tmp_path / "judge-fixtures"
    planted = plant_failed_episode(store, suffix="rev")

    reflected = reflect_to_batch(lib, fixtures, planted.ep)
    prov = reflected.stage_b.registered[0]
    batch_label = reflected.stage_b.batch_label

    snap_before = store.current_snapshot_id()
    # The benchmark mini-episode regresses well past the bootstrap threshold.
    benchmark = v.BenchmarkOutcome(candidate=0.50, sigma=0.02, history=(0.90, 0.90, 0.90))
    outcome = v.validate_batch(
        store,
        batch_label=batch_label,
        snapshot_id=snap_before,
        benchmark=benchmark,
        n_replay_pairs=3,
        # A revert needs no human action; a provided signer is advisory only.
        cosign_fn=lambda decision: "human",
    )
    assert outcome.promoted is False
    assert outcome.decision.verdict == v.VERDICT_REVERT
    assert v.active_batch_insight_ids(store, batch_label) == ()
    assert store.get_insight(prov.insight_id)["status"] == "retired"

    # The follow-up episode's planner prompt is unchanged — empty injection.
    snap2 = store.current_snapshot_id()
    nxt = next_planner_prompt(lib, snap2)
    assert prov.insight_id not in nxt.retrieval.pool_insight_ids
    assert nxt.retrieval.skills == ()
    assert nxt.section == ""
    assert nxt.prompt == nxt.bare  # the loop did not close — prompt is untouched
    store.close()


def test_readme_documents_live_learning_cycle():
    """U9 verification: the live learning cycle (linkding episode -> reflect ->
    Kanboard benchmark -> human co-sign -> promote, with costs) is documented."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    low = readme.lower()
    # The phase and its closed loop.
    assert "phase 3a" in low
    assert "learning loop" in low or "learning cycle" in low
    # The held-out second target the required benchmark gate runs against.
    assert "kanboard" in low
    assert "benchmark" in low
    # The bootstrap human-in-the-loop promote.
    assert "co-sign" in low
    # The automated reflector replacing the human.
    assert "reflector" in low
    assert "stage a" in low and "stage b" in low
    # Costs are recorded (the validation/reflection spend is real new spend).
    assert "cost" in low
