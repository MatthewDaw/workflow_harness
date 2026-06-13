"""plan-008 U9: config wiring + R3 ingest-gauntlet end-to-end acceptance (R20, R21).

The whole gauntlet driven offline over a real migrated store: the curated set
(R21) exercises every R3 move exactly once —

    exact duplicate  -> corroborate (content-hash hit, R15)
    near duplicate    -> corroborate (NLI entailment + corroborates edge + vote)
    nuance            -> refine      (NLI neutral + refines edge, both kept)
    contradiction     -> contradicts edge ONLY at ingest, then `invalid_at`
                          stamped + incumbent retired at PROMOTION (deferred, R16)
    target trivia     -> lint_reject (the gate exits before any embed/insert)

and asserts the load-bearing R21 invariants: no skill authored at ingest,
contradictions are NEVER merged at any cosine, corroboration counts increment on
the right incumbents, and `fitness_events` accumulate the snapshot-keyed usage
traces plan 009's objective consumes.

Fully offline / zero quota (R21): the admission gate + resolve_edge fallback
replay against judge fixtures, NLI replays against nli fixtures (deterministic
CPU), embeddings come from a fake encoder. No ``claude`` on PATH, no model
download. The config keys this unit ships (`[merge].candidate_floor`,
`[nli].confidence_threshold`) are read from a loaded :class:`Config` and threaded
into ``add_idea_r3`` / ``promote_batch`` — the wiring is exercised, not hardcoded.

## Conformance (008 U9)

Each R20/R21 invariant maps to the behavioral test that enforces it:

- the curated set drives each move exactly once and asserts the resulting
  edges/statuses; the contradiction is NEVER merged at any cosine
    -> test_e2e_curated_chain_every_move
- after the full curated chain the `skills` table has ZERO rows authored at
  ingest (grouping deferred to plan 009)
    -> test_e2e_no_skill_authored_at_ingest
- corroboration counts increment on the right incumbents and `fitness_events`
  accumulate non-zero, snapshot-keyed rows (the substrate plan 009 consumes)
    -> test_e2e_corroboration_and_fitness_populated
- re-running the curated chain produces no duplicate insights/edges and no
  spurious status changes (idempotent re-run)
    -> test_e2e_second_run_idempotent
- the e2e completes with `claude` absent from PATH and NLI/judge in replay mode
  (zero quota, no model download)
    -> test_e2e_runs_offline_zero_quota
- R20 config wiring: the shipped thresholds.toml carries `[merge].candidate_floor`
  and `[nli]`, and those values flow into the gauntlet
    -> test_shipped_config_carries_r3_keys (and the curated chain, which threads
       config.merge.candidate_floor / config.nli.confidence_threshold)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_families import judge, nli, pipeline
from agent_families.config import (
    Config,
    EmbeddingConfig,
    GraphConfig,
    JudgeConfig,
    LifecycleConfig,
    MergeConfig,
    NliConfig,
    RetrievalConfig,
    StoreConfig,
    load_config,
)
from agent_families.embedding import EmbeddingService
from agent_families.judge import (
    ADMISSION_GATE_SCHEMA,
    RESOLVE_EDGE_SCHEMA,
    write_fixture,
)
from agent_families.lifecycle import promote_batch
from agent_families.pipeline import (
    LintRejected,
    add_idea_r3,
    build_idea_text,
    content_hash,
)
from agent_families.store import Store
from agent_families.vecindex import VecIndex

REPO_THRESHOLDS = Path(__file__).resolve().parent.parent / "thresholds.toml"

DIM = 8

# A Config carrying the R3 keys this unit wires. The embedding dim matches the
# fake encoder; merge.candidate_floor / nli.confidence_threshold are exactly the
# shipped defaults, threaded into add_idea_r3 / promote_batch below (R20).
CFG = Config(
    embedding=EmbeddingConfig(model="fake-embedder", dim=DIM, device="cpu"),
    merge=MergeConfig(cosine_threshold=0.92, candidate_floor=0.80),
    retrieval=RetrievalConfig(ann_top_k=10, relevance_floor=0.5),
    judge=JudgeConfig(model="sonnet", max_retries=1, bare=False),
    lifecycle=LifecycleConfig(active_cap=50),
    store=StoreConfig(busy_timeout_ms=5000),
    nli=NliConfig(model=nli.DEFAULT_MODEL, confidence_threshold=0.65),
    graph=GraphConfig(knn_k=15),
)


# --- fakes / fixture helpers (mirror test_pipeline.py's R3 conventions) -------


class FakeEncoder:
    """Returns one fixed vector for every encode() call (offline, deterministic)."""

    def __init__(self, vector):
        self.vector = list(vector)
        self.calls: list[str] = []

    def encode(self, text):
        self.calls.append(text)
        return list(self.vector)


def embedder_for(vector) -> EmbeddingService:
    return EmbeddingService(CFG.embedding, encoder=FakeEncoder(vector))


def evec(i: int) -> list[float]:
    """The i-th canonical unit basis vector (orthogonal across scenarios)."""
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def envelope_for(output: dict) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 950,
        "num_turns": 1,
        "result": "ok",
        "total_cost_usd": 0.003,
        "structured_output": output,
    }


def fields_n(tag: str) -> dict:
    """A distinct structural atom keyed by ``tag`` (unique content hash + text)."""
    return dict(
        precondition=f"A web target tagged {tag} is being probed for requirements",
        action=f"Ask about {tag}-gated areas during the elicitation interview",
        expected_outcome=f"Hidden {tag} features surface as explicit requirements",
    )


def atom_of(fields: dict) -> dict:
    a = dict(fields)
    a["negative_scope"] = "do not apply outside the stated precondition"
    return a


def record_gate_admit(fixtures_dir, fields: dict, *, scope_value="universal") -> None:
    prompt = pipeline.build_admission_gate_prompt(
        fields["precondition"], fields["action"], fields["expected_outcome"], None
    )
    output = {
        "outcome": "admit",
        "atom": atom_of(fields),
        "scope_tag": {"value": scope_value, "justification": "the rule generalizes"},
    }
    write_fixture(fixtures_dir, prompt, ADMISSION_GATE_SCHEMA, "sonnet", envelope_for(output))


def record_gate_lint_reject(fixtures_dir, fields: dict, reason: str) -> None:
    prompt = pipeline.build_admission_gate_prompt(
        fields["precondition"], fields["action"], fields["expected_outcome"], None
    )
    output = {
        "outcome": "lint_reject",
        "reason": reason,
        "scope_tag": {"value": "", "justification": "rejected"},
    }
    write_fixture(fixtures_dir, prompt, ADMISSION_GATE_SCHEMA, "sonnet", envelope_for(output))


def record_nli(fixtures_dir, premise_fields: dict, hyp_fields: dict, label, conf) -> None:
    nli.write_fixture(
        fixtures_dir,
        nli.DEFAULT_MODEL,
        build_idea_text(**premise_fields),
        build_idea_text(**hyp_fields),
        {"label": label, "confidence": conf, "logits": [0.0, 0.0, 0.0]},
    )


def insight_nli_text(fields: dict) -> str:
    """The whole-atom text the promotion-time re-check feeds NLI (lifecycle form)."""
    parts = [fields["precondition"], fields["action"], fields["expected_outcome"]]
    return " ".join(p.strip() for p in parts if p.strip())


def record_recheck_nli(fixtures_dir, incumbent: dict, challenger: dict, label, conf) -> None:
    """The promotion-time supersede re-check fixture (premise=incumbent atom text)."""
    nli.write_fixture(
        fixtures_dir,
        nli.DEFAULT_MODEL,
        insight_nli_text(incumbent),
        insight_nli_text(challenger),
        {"label": label, "confidence": conf, "logits": [0.0, 0.0, 0.0]},
    )


# --- env + store helpers ------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_judge_env(monkeypatch):
    monkeypatch.delenv(judge.MODE_ENV, raising=False)
    monkeypatch.delenv(judge.FIXTURES_ENV, raising=False)
    monkeypatch.delenv(nli.MODE_ENV, raising=False)
    monkeypatch.delenv(nli.FIXTURES_ENV, raising=False)


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    e = SimpleNamespace(store=store, vec=vec, fixtures=tmp_path / "fixtures")
    yield e
    store.close()


def seed(env, fields: dict, vector, *, provenance="manual") -> int:
    with env.store.transaction():
        insight_id = env.store.insert_insight(
            **fields,
            content_hash=content_hash(**fields),
            status="active",
            provenance=provenance,
        )
        env.vec.insert(insight_id, vector)
    return insight_id


def register(env, fields, vector, *, batch_label):
    """Drive add_idea_r3 with the config-wired floor + NLI threshold (R20)."""
    return add_idea_r3(
        env.store,
        env.vec,
        embedder_for(vector),
        CFG,
        **fields,
        batch_label=batch_label,
        judge_fixtures_dir=env.fixtures,
        nli_fixtures_dir=env.fixtures,
        nli_mode="replay",
        candidate_floor=CFG.merge.candidate_floor,
        nli_confidence_threshold=CFG.nli.confidence_threshold,
    )


def edges_of(env, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM insight_edges"
    params: tuple = ()
    if kind is not None:
        sql += " WHERE kind = ?"
        params = (kind,)
    return [dict(r) for r in env.store.conn.execute(sql, params).fetchall()]


def corroborate_count(env, insight_id: int) -> int:
    return env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM fitness_events WHERE insight_id = ? AND kind = 'corroborate'",
        (insight_id,),
    ).fetchone()["n"]


def count(env, table: str) -> int:
    return env.store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# --- the curated chain (R21) — authored once, asserted by each test ------------


@dataclass
class ChainResult:
    dup_inc: int          # exact-dup incumbent (content-hash hit corroborates it)
    corr_inc: int         # near-dup incumbent (NLI entailment)
    corr_new: int         # the kept, distinct near-dup insight
    ref_inc: int          # nuance incumbent (NLI neutral)
    ref_new: int          # the kept refine insight
    con_inc: int          # contradiction incumbent (retired at promotion)
    con_new: int          # the contradiction challenger
    promote_snapshot: int  # the snapshot the supersede rode


# The five curated scenarios (orthogonal key vectors -> disjoint candidate sets).
DUP = fields_n("dup")
CORR_INC, CORR_NEW = fields_n("corr-inc"), fields_n("corr-new")
REF_INC, REF_NEW = fields_n("ref-inc"), fields_n("ref-new")
CON_INC, CON_NEW = fields_n("con-inc"), fields_n("con-new")
TRIVIA = fields_n("trivia")


def record_all_fixtures(env) -> None:
    """Idempotent fixture recording for the whole curated chain (write overwrites)."""
    # exact dup -> the SAME fields re-submitted; only the gate runs (content-hash hit).
    record_gate_admit(env.fixtures, DUP)
    # near dup -> entailment (corroborate, distinct + edge + vote).
    record_gate_admit(env.fixtures, CORR_NEW)
    record_nli(env.fixtures, CORR_INC, CORR_NEW, "entailment", 0.95)
    # nuance -> neutral (refine, both kept).
    record_gate_admit(env.fixtures, REF_NEW)
    record_nli(env.fixtures, REF_INC, REF_NEW, "neutral", 0.92)
    # contradiction -> contradicts edge only at ingest, retired at promotion.
    record_gate_admit(env.fixtures, CON_NEW)
    record_nli(env.fixtures, CON_INC, CON_NEW, "contradiction", 0.95)
    record_recheck_nli(env.fixtures, CON_INC, CON_NEW, "contradiction", 0.95)
    # target trivia -> the gate rejects before any embed/insert.
    record_gate_lint_reject(env.fixtures, TRIVIA, "target-trivia: a single instance, not a rule")


def seed_incumbents(env) -> dict[str, int]:
    """Seed the four incumbents; the contradiction incumbent has LOWER authority
    (reflector) so the manual challenger wins the promotion-time tournament (R16)."""
    return {
        "dup": seed(env, DUP, evec(0)),
        "corr": seed(env, CORR_INC, evec(1)),
        "ref": seed(env, REF_INC, evec(2)),
        "con": seed(env, CON_INC, evec(3), provenance="reflector"),
    }


def run_curated_chain(env, *, promote: bool = True) -> ChainResult:
    """Drive the curated set through the R3 gauntlet (+ the promote-time supersede)."""
    record_all_fixtures(env)
    inc = seed_incumbents(env)

    # 1. exact duplicate -> corroborate (content-hash hit).
    res_dup = register(env, DUP, evec(0), batch_label="b-dup")
    assert res_dup.code == "corroborated" and res_dup.insight_id == inc["dup"]

    # 2. near duplicate -> corroborate (entailment).
    res_corr = register(env, CORR_NEW, evec(1), batch_label="b-corr")
    assert res_corr.code == "registered"

    # 3. nuance -> refine (neutral).
    res_ref = register(env, REF_NEW, evec(2), batch_label="b-ref")
    assert res_ref.code == "registered"

    # 4. contradiction -> contradicts edge ONLY at ingest.
    res_con = register(env, CON_NEW, evec(3), batch_label="b-con")
    assert res_con.code == "registered"

    # 5. target trivia -> lint_reject (the gate exits before embed/insert).
    with pytest.raises(LintRejected):
        register(env, TRIVIA, evec(4), batch_label="b-trivia")

    promote_snapshot = env.store.current_snapshot_id()  # 0 at ingest
    if promote:
        # Deferred-supersede rides promotion: the validated manual challenger
        # retires the lower-authority incumbent under the minted snapshot (R16).
        result = promote_batch(
            env.store,
            "b-con",
            nli_mode="replay",
            nli_fixtures_dir=env.fixtures,
            nli_confidence_threshold=CFG.nli.confidence_threshold,
        )
        assert result.retired_incumbent_ids == (inc["con"],)
        promote_snapshot = result.snapshot_id

    return ChainResult(
        dup_inc=inc["dup"],
        corr_inc=inc["corr"],
        corr_new=res_corr.insight_id,
        ref_inc=inc["ref"],
        ref_new=res_ref.insight_id,
        con_inc=inc["con"],
        con_new=res_con.insight_id,
        promote_snapshot=promote_snapshot,
    )


# --- R20: config wiring -------------------------------------------------------


def test_shipped_config_carries_r3_keys():
    """The shipped thresholds.toml carries the R3 keys this unit wires (R20)."""
    cfg = load_config(REPO_THRESHOLDS)
    # candidate_floor is the FILTER (not the demoted cosine verdict).
    assert cfg.merge.candidate_floor == pytest.approx(0.80)
    assert cfg.merge.cosine_threshold == pytest.approx(0.92)  # demoted, retained
    # [nli] renders the duplicate-vs-contradiction verdict.
    assert cfg.nli.model == "cross-encoder/nli-deberta-v3-base"
    assert 0.0 <= cfg.nli.confidence_threshold <= 1.0
    # [embedding].matryoshka_dim is OPTIONAL (commented in the template) -> None.
    assert cfg.embedding.matryoshka_dim is None
    # [graph] is reserved for plan 009.
    assert cfg.graph.knn_k == 15


# --- R21: the curated chain, every move exactly once --------------------------


def test_e2e_curated_chain_every_move(env):
    """Each R3 move fires once; the contradiction is NEVER merged at any cosine."""
    chain = run_curated_chain(env)

    # exact dup: corroborated the incumbent, no new insight authored.
    # near dup: a DISTINCT new insight + a corroborates edge new -> incumbent.
    assert chain.corr_new != chain.corr_inc
    corr_edges = edges_of(env, "corroborates")
    assert len(corr_edges) == 1
    assert corr_edges[0]["src"] == chain.corr_new and corr_edges[0]["dst"] == chain.corr_inc

    # nuance: both kept + a refines edge.
    assert chain.ref_new != chain.ref_inc
    ref_edges = edges_of(env, "refines")
    assert len(ref_edges) == 1
    assert ref_edges[0]["src"] == chain.ref_new and ref_edges[0]["dst"] == chain.ref_inc

    # contradiction: a contradicts edge was written at ingest, NEVER a merge.
    con_edges = edges_of(env, "contradicts")
    assert len(con_edges) == 1
    assert con_edges[0]["src"] == chain.con_new and con_edges[0]["dst"] == chain.con_inc
    assert env.store.find_merge_log_by_hash(content_hash(**CON_NEW)) is None

    # The deferred-supersede fired at PROMOTION (not ingest): the incumbent is
    # now retired with invalid_at stamped under the promotion snapshot.
    inc_row = env.store.get_insight(chain.con_inc)
    assert inc_row["status"] == "retired"
    assert inc_row["invalid_at"] == f"snapshot:{chain.promote_snapshot}"
    # The challenger survived promotion as active.
    assert env.store.get_insight(chain.con_new)["status"] == "active"

    # No `similarity` edge is ever written at v1 (recomputed each derive pass).
    assert edges_of(env, "similarity") == []


def test_e2e_no_skill_authored_at_ingest(env):
    """After the full curated chain the `skills` table has ZERO rows (R13/R21)."""
    assert count(env, "skills") == 0  # nothing pre-seeded
    run_curated_chain(env)
    assert count(env, "skills") == 0
    assert count(env, "skill_members") == 0


def test_e2e_corroboration_and_fitness_populated(env):
    """Corroboration counts land on the right incumbents; fitness_events accrue (R21)."""
    chain = run_curated_chain(env)

    # The exact-dup incumbent got a content-hash corroborate vote; the near-dup
    # incumbent got an entailment corroborate vote. Neither the refine incumbent
    # nor either challenger is voted on.
    assert corroborate_count(env, chain.dup_inc) == 1
    assert corroborate_count(env, chain.corr_inc) == 1
    assert corroborate_count(env, chain.ref_inc) == 0
    assert corroborate_count(env, chain.corr_new) == 0

    # fitness_events accumulate non-zero, snapshot-keyed rows (the substrate plan
    # 009's objective consumes). The votes were stamped at ingest -> snapshot 0.
    rows = env.store.conn.execute(
        "SELECT insight_id, kind, snapshot_id FROM fitness_events WHERE kind = 'corroborate'"
    ).fetchall()
    assert len(rows) == 2
    assert {r["insight_id"] for r in rows} == {chain.dup_inc, chain.corr_inc}
    assert all(r["snapshot_id"] == 0 for r in rows)


def test_e2e_second_run_idempotent(env):
    """Re-running the ingest chain authors no duplicate insights/edges (R21)."""
    chain = run_curated_chain(env, promote=True)

    insight_ids_before = {
        r["id"] for r in env.store.conn.execute("SELECT id FROM insights").fetchall()
    }
    edges_before = {
        (r["src"], r["dst"], r["kind"]) for r in edges_of(env)
    }
    statuses_before = {
        r["id"]: r["status"]
        for r in env.store.conn.execute("SELECT id, status FROM insights").fetchall()
    }

    # Re-submit every challenger: each generalized atom is now an exact-content
    # hit -> the corroborate path returns the existing insight, no new row/edge.
    record_all_fixtures(env)
    for fields, vector, batch in (
        (DUP, evec(0), "b-dup"),
        (CORR_NEW, evec(1), "b-corr"),
        (REF_NEW, evec(2), "b-ref"),
        (CON_NEW, evec(3), "b-con"),
    ):
        res = register(env, fields, vector, batch_label=batch)
        assert res.code == "corroborated"
    with pytest.raises(LintRejected):
        register(env, TRIVIA, evec(4), batch_label="b-trivia")

    insight_ids_after = {
        r["id"] for r in env.store.conn.execute("SELECT id FROM insights").fetchall()
    }
    edges_after = {
        (r["src"], r["dst"], r["kind"]) for r in edges_of(env)
    }
    statuses_after = {
        r["id"]: r["status"]
        for r in env.store.conn.execute("SELECT id, status FROM insights").fetchall()
    }

    assert insight_ids_after == insight_ids_before  # no duplicate insights
    assert edges_after == edges_before              # no duplicate edges
    assert statuses_after == statuses_before        # no spurious status changes
    # The retired incumbent stayed retired; the chain promoted nothing new.
    assert statuses_after[chain.con_inc] == "retired"


def test_e2e_runs_offline_zero_quota(env, monkeypatch):
    """The whole chain completes with `claude` absent from PATH, replay-only (R21)."""
    # No `claude` resolvable, and any accidental judge/NLI live call would explode:
    # the subprocess invocation (`_invoke`) and the NLI model load (`_load_encoder`)
    # are both replaced with hard failures, so replay mode is proven, not assumed.
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        judge, "_invoke",
        lambda *a, **k: pytest.fail("a live `claude -p` call escaped replay mode"),
    )
    monkeypatch.setattr(
        nli, "_load_encoder",
        lambda *a, **k: pytest.fail("an NLI model load escaped replay mode (download/quota)"),
    )

    chain = run_curated_chain(env)

    # The gauntlet produced its full trace substrate with zero quota.
    assert chain.con_new != chain.con_inc
    assert edges_of(env, "contradicts")
    assert env.store.get_insight(chain.con_inc)["status"] == "retired"
