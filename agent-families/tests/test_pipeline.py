"""add_idea pipeline tests — fixture-driven, every judge outcome covered (U5).

Fully offline: the judge runs in replay mode against fixtures written into a
tmp dir for the exact prompts the pipeline builds (the prompt builders are pure
functions, so tests compute the same prompts), and embeddings come from a fake
encoder returning hand-chosen vectors. Zero quota, zero subprocess calls.
"""

from __future__ import annotations

import json
import logging
import math
from types import SimpleNamespace

import pytest

from agent_families import judge, nli, pipeline
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
from agent_families.judge import (
    ADMISSION_GATE_SCHEMA,
    RESOLVE_EDGE_SCHEMA,
    JudgeFixtureMissing,
    JudgeSchemaViolation,
    write_fixture,
)
from agent_families.pipeline import (
    JUDGE_SCHEMA,
    MERGE_REVIEW_OUTCOMES,
    NLI_CONFIDENCE_THRESHOLD_DEFAULT,
    PLACEMENT_OUTCOMES,
    TAXONOMY_OUTCOMES,
    LintRejected,
    NoPlacement,
    RetiredNearDuplicate,
    RewriteProposed,
    StructuralValidationError,
    add_idea,
    add_idea_r3,
    build_admission_gate_prompt,
    build_idea_text,
    build_merge_prompt,
    build_placement_prompt,
    build_resolve_edge_prompt,
    build_taxonomy_prompt,
    canonical_fields_json,
    content_hash,
    outcome_validator,
)
# NOTE (plan-008 A-U6 cut-over): the symbols above that belong to the legacy
# author-at-ingest path (JUDGE_SCHEMA, MERGE_REVIEW_OUTCOMES, PLACEMENT_OUTCOMES,
# TAXONOMY_OUTCOMES, build_merge_prompt, build_placement_prompt, build_taxonomy_prompt,
# outcome_validator, NoPlacement, RetiredNearDuplicate) are imported for backward-compat
# only; they are no longer exercised by the live add_idea path. The legacy tests that
# tested those symbols are removed (plan-008 A-U6) — only the R3 gauntlet tests remain.
from agent_families.store import Store
from agent_families.vecindex import Neighbor, VecIndex

DIM = 8
CFG = Config(
    embedding=EmbeddingConfig(model="fake-embedder", dim=DIM, device="cpu"),
    merge=MergeConfig(cosine_threshold=0.92),
    retrieval=RetrievalConfig(ann_top_k=10, relevance_floor=0.5),
    judge=JudgeConfig(model="sonnet", max_retries=1, bare=False),
    lifecycle=LifecycleConfig(active_cap=50),
    store=StoreConfig(busy_timeout_ms=5000),
)

# Unit vectors with hand-chosen cosines against V_IDEA (what the new idea embeds
# to): V_NEAR -> 1.0 (>= 0.92 merge prefilter), V_MID -> 0.7 (placement, no
# merge), V_LOW -> 0.3 (below the 0.5 relevance floor -> taxonomy prompt).
V_IDEA = [1.0] + [0.0] * (DIM - 1)
V_NEAR = list(V_IDEA)
V_MID = [0.7, math.sqrt(1 - 0.7**2)] + [0.0] * (DIM - 2)
V_LOW = [0.3, math.sqrt(1 - 0.3**2)] + [0.0] * (DIM - 2)

IDEA = dict(
    precondition="A web target is being probed for requirements",
    action="Ask about role-gated admin areas during elicitation",
    expected_outcome="Hidden admin features surface as explicit requirements",
)
NEIGHBOR_FIELDS = dict(
    precondition="An elicitation interview is underway",
    action="Enumerate the target's user roles before feature questions",
    expected_outcome="Feature questions are scoped per role",
)
IDEA_TEXT = build_idea_text(**IDEA)

COUNTED_TABLES = (
    "insights",
    "insight_vectors",
    "skills",
    "skill_members",
    "merge_log",
    "contradictions",
    "batches",
    "snapshots",
    "status_transitions",
    "promotion_queue",
)


class FakeEncoder:
    """Returns one fixed vector for every encode() call; records the calls."""

    def __init__(self, vector):
        self.vector = list(vector)
        self.calls = []

    def encode(self, text):
        self.calls.append(text)
        return list(self.vector)


def embedder_for(vector) -> tuple[EmbeddingService, FakeEncoder]:
    encoder = FakeEncoder(vector)
    return EmbeddingService(CFG.embedding, encoder=encoder), encoder


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


def out(outcome: str, **extra) -> dict:
    """A schema-valid judge output with overridable fields."""
    base = {
        "outcome": outcome,
        "scope_tag": {
            "value": "universal",
            "justification": "applies to any web target",
        },
        "lint": {"verdict": "pass"},
        "confidence": 0.9,
    }
    base.update(extra)
    return base


def record(fixtures_dir, prompt: str, output: dict) -> None:
    write_fixture(fixtures_dir, prompt, JUDGE_SCHEMA, "sonnet", envelope_for(output))


def neighbor(insight_id: int, status: str = "active") -> Neighbor:
    # Prompt builders use only insight_id and status (cosines are omitted from
    # prompts per R23), so the distance here is irrelevant.
    return Neighbor(insight_id=insight_id, distance=0.0, status=status)


def table_counts(store: Store) -> dict[str, int]:
    return {
        t: store.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        for t in COUNTED_TABLES
    }


@pytest.fixture(autouse=True)
def clean_judge_env(monkeypatch):
    monkeypatch.delenv(judge.MODE_ENV, raising=False)
    monkeypatch.delenv(judge.FIXTURES_ENV, raising=False)


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    family_id = store.create_family("worker")
    agent_id = store.create_agent(family_id, "generalist", description="generic worker")
    skill_id = store.create_skill(agent_id, "elicitation", "asking the right questions")
    e = SimpleNamespace(
        store=store,
        vec=vec,
        family_id=family_id,
        agent_id=agent_id,
        skill_id=skill_id,
        fixtures=tmp_path / "fixtures",
    )
    yield e
    store.close()


def seed(env, fields: dict, vector, *, status="active", in_skill=True) -> int:
    with env.store.transaction():
        insight_id = env.store.insert_insight(
            **fields, content_hash=content_hash(**fields), status=status
        )
        env.vec.insert(insight_id, vector)
        if in_skill:
            env.store.append_member(env.skill_id, insight_id)
    return insight_id


def run(env, *, fields=IDEA, vector=V_IDEA, **kwargs):
    embedder, encoder = embedder_for(vector)
    result = add_idea(
        env.store,
        env.vec,
        embedder,
        CFG,
        **fields,
        batch_label=kwargs.pop("batch_label", "batch-1"),
        judge_fixtures_dir=env.fixtures,
        **kwargs,
    )
    return result, encoder


# --- structural validation (flow exit B1) -------------------------------------


@pytest.mark.parametrize("field", ["precondition", "action", "expected_outcome"])
def test_structural_validation_rejects_blank_field(env, field):
    bad = dict(IDEA, **{field: "   "})
    embedder, encoder = embedder_for(V_IDEA)
    with pytest.raises(StructuralValidationError, match=field):
        add_idea(
            env.store, env.vec, embedder, CFG, **bad,
            batch_label="batch-1", judge_fixtures_dir=env.fixtures,
        )
    assert encoder.calls == []


# --- legacy placement tests REMOVED (plan-008 A-U6 cut-over) -------------------
# The legacy add_idea tests (append_to_skill, new_skill, merge_discard, no_placement,
# taxonomy cold-start, retired-near-duplicate, contradiction_flag/supersede via the
# placement judge, outcome_validator) are removed because add_idea is now the R3 gate
# (delegates to add_idea_r3). The R3 gauntlet tests below cover all live behavior.
# -------------------------------------------------------------------------------


def _REMOVED_test_happy_path_append_to_existing_skill(env, caplog):
    n1 = seed(env, NEIGHBOR_FIELDS, V_MID)
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(n1)], None)
    record(env.fixtures, prompt, out("append_to_skill", target_skill_id=env.skill_id))

    result, encoder = run(env)

    assert result.code == "registered"
    assert result.judge_outcome == "append_to_skill"
    assert result.skill_id == env.skill_id
    assert result.scope_tag == "universal"
    row = env.store.get_insight(result.insight_id)
    assert row["status"] == "quarantined"
    assert row["scope_tag"] == "universal"
    assert row["embedding_model"] == "fake-embedder"
    assert row["embedding_dim"] == DIM
    batch = env.store.conn.execute(
        "SELECT label FROM batches WHERE id = ?", (row["batch_id"],)
    ).fetchone()
    assert batch["label"] == "batch-1"
    # membership appended after the existing member, vec row written
    assert env.store.skill_members(env.skill_id) == [n1, result.insight_id]
    assert env.vec.count() == 2
    # registration never mints a snapshot (R3)
    assert env.store.current_snapshot_id() == 0
    # the idea was embedded as a document exactly once
    assert encoder.calls == ["search_document: " + IDEA_TEXT]


def _REMOVED_test_new_skill_created_under_the_right_agent(env):
    n1 = seed(env, NEIGHBOR_FIELDS, V_MID)
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(n1)], None)
    record(
        env.fixtures,
        prompt,
        out(
            "new_skill",
            new_skill={
                "agent_id": env.agent_id,
                "name": "admin-area-probing",
                "description": "surfacing role-gated areas",
            },
        ),
    )

    result, _ = run(env)

    assert result.code == "registered"
    skill = env.store.conn.execute(
        "SELECT * FROM skills WHERE id = ?", (result.skill_id,)
    ).fetchone()
    assert skill["agent_id"] == env.agent_id
    assert skill["name"] == "admin-area-probing"
    assert skill["created_batch_id"] == result.batch_id
    assert env.store.skill_members(result.skill_id) == [result.insight_id]


# --- content-hash fast paths -------------------------------------------------------


def _REMOVED_test_exact_duplicate_exits_before_embedding(env):
    existing = seed(env, IDEA, V_IDEA)
    result, encoder = run(env)
    assert result.code == "exact_duplicate"
    assert result.insight_id == existing
    assert encoder.calls == []  # exits before any encoder call (R5)


def _REMOVED_test_near_duplicate_merge_discard_writes_merge_log(env):
    dup = seed(env, NEIGHBOR_FIELDS, V_NEAR)
    prompt = build_merge_prompt(env.store, IDEA_TEXT, [neighbor(dup)])
    record(env.fixtures, prompt, out("merge_discard", duplicate_of=dup))

    result, _ = run(env)

    assert result.code == "merged"
    assert result.insight_id == dup
    assert result.judge_outcome == "merge_discard"
    log_row = env.store.find_merge_log_by_hash(content_hash(**IDEA))
    assert log_row is not None
    assert log_row["duplicate_of"] == dup
    assert json.loads(log_row["structural_fields_json"]) == IDEA
    assert log_row["structural_fields_json"] == canonical_fields_json(**IDEA)
    # discard-new: no insight row, no vec row was added
    assert env.store.find_insight_by_hash(content_hash(**IDEA)) is None
    assert env.vec.count() == 1


def _REMOVED_test_resubmitting_merged_idea_exits_via_merge_log_fast_path(env):
    dup = seed(env, NEIGHBOR_FIELDS, V_NEAR)
    prompt = build_merge_prompt(env.store, IDEA_TEXT, [neighbor(dup)])
    record(env.fixtures, prompt, out("merge_discard", duplicate_of=dup))
    run(env)  # first submission: judged merge

    result, encoder = run(env)  # resubmission

    assert result.code == "previously_merged"
    assert result.insight_id == dup
    assert encoder.calls == []  # no encoder call (R5)
    # no second merge-log row
    n = env.store.conn.execute("SELECT COUNT(*) AS n FROM merge_log").fetchone()["n"]
    assert n == 1


def _REMOVED_test_merge_rejection_falls_through_to_placement(env):
    dup = seed(env, NEIGHBOR_FIELDS, V_NEAR)
    merge_prompt = build_merge_prompt(env.store, IDEA_TEXT, [neighbor(dup)])
    record(env.fixtures, merge_prompt, out("no_placement"))  # merge rejected (R8)
    placement_prompt = build_placement_prompt(
        env.store, IDEA_TEXT, [neighbor(dup)], None
    )
    record(
        env.fixtures,
        placement_prompt,
        out("append_to_skill", target_skill_id=env.skill_id),
    )

    result, _ = run(env)

    assert result.code == "registered"
    assert result.judge_outcome == "append_to_skill"
    assert env.store.find_merge_log_by_hash(content_hash(**IDEA)) is None


# --- contradictions (R9) -------------------------------------------------------------


def _REMOVED_test_contradiction_supersede_writes_link_and_flag_leaves_incumbent_active(env):
    z = seed(env, NEIGHBOR_FIELDS, V_MID)
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(z)], None)
    record(
        env.fixtures,
        prompt,
        out("contradiction_supersede", supersedes=z, target_skill_id=env.skill_id),
    )

    result, _ = run(env)

    row = env.store.get_insight(result.insight_id)
    assert row["supersedes"] == z
    flag = env.store.conn.execute("SELECT * FROM contradictions").fetchone()
    assert flag["challenger_id"] == result.insight_id
    assert flag["incumbent_id"] == z
    assert flag["status"] == "open"
    # no automated retirement: Z stays active (R9)
    assert env.store.get_insight(z)["status"] == "active"
    assert env.store.current_snapshot_id() == 0


def _REMOVED_test_contradiction_flag_opens_flag_without_supersedes_link(env):
    z = seed(env, NEIGHBOR_FIELDS, V_MID)
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(z)], None)
    record(
        env.fixtures,
        prompt,
        out("contradiction_flag", supersedes=z, target_skill_id=env.skill_id),
    )

    result, _ = run(env)

    row = env.store.get_insight(result.insight_id)
    assert row["supersedes"] is None  # flag only, no supersedes link
    flag = env.store.conn.execute("SELECT * FROM contradictions").fetchone()
    assert flag["challenger_id"] == result.insight_id
    assert flag["incumbent_id"] == z
    assert env.store.get_insight(z)["status"] == "active"


# --- lint outcomes (R10) ----------------------------------------------------------------


def _REMOVED_test_lint_reject_raises_with_reason_and_writes_nothing(env):
    n1 = seed(env, NEIGHBOR_FIELDS, V_MID)
    before = table_counts(env.store)
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(n1)], None)
    record(
        env.fixtures,
        prompt,
        out(
            "lint_reject",
            lint={"verdict": "reject", "reason": "names target internals: /wp-admin"},
        ),
    )
    with pytest.raises(LintRejected, match="/wp-admin"):
        run(env)
    assert table_counts(env.store) == before


def _REMOVED_test_rewrite_proposed_then_accept_rewrite_reenters_at_content_hash_check(env):
    rewrite = dict(
        precondition="A web app exposes role-gated areas",
        action="Probe for admin areas generically during elicitation",
        expected_outcome="Role-gated features are captured without naming target paths",
    )
    n1 = seed(env, NEIGHBOR_FIELDS, V_MID)
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(n1)], None)
    record(
        env.fixtures,
        prompt,
        out("rewrite_proposed", lint={"verdict": "rewrite", "rewrite": rewrite}),
    )

    with pytest.raises(RewriteProposed, match="--accept-rewrite") as excinfo:
        run(env)
    assert excinfo.value.rewrite == rewrite
    assert str(excinfo.value).count(rewrite["action"]) == 1  # rewrite is printed

    # Accepting the rewrite re-enters at the content-hash check: a pre-existing
    # insight with the rewrite's hash is found with zero encoder calls.
    existing = seed(env, rewrite, V_MID, in_skill=False)
    result, encoder = run(env, fields=rewrite, accept_rewrite=True)
    assert result.code == "exact_duplicate"
    assert result.insight_id == existing
    assert encoder.calls == []


# --- subset enforcement and atomicity (R6/R7) ----------------------------------------


def _REMOVED_test_taxonomy_out_of_subset_outcome_retries_then_fails_with_zero_writes(env):
    # Empty insight library -> taxonomy prompt; append_to_skill is out-of-subset
    # there (allowed: new_skill, no_placement) and rides the violation retry path.
    before = table_counts(env.store)
    bad = out("append_to_skill", target_skill_id=env.skill_id)
    prompt = build_taxonomy_prompt(env.store, IDEA_TEXT, None)
    record(env.fixtures, prompt, bad)
    violation = outcome_validator(env.store, TAXONOMY_OUTCOMES)(bad)
    assert "not in the allowed subset" in violation
    retry_prompt = judge._feedback_prompt(prompt, bad, violation)
    record(env.fixtures, retry_prompt, bad)  # still bad after feedback

    with pytest.raises(JudgeSchemaViolation, match="allowed subset"):
        run(env)
    assert table_counts(env.store) == before  # nothing written (R7)


def _REMOVED_test_judge_failure_after_retries_leaves_zero_rows(env):
    # Atomicity probe: dangling-reference responses exhaust the retries; count
    # every table before/after.
    n1 = seed(env, NEIGHBOR_FIELDS, V_MID)
    before = table_counts(env.store)
    bad = out("append_to_skill", target_skill_id=999)  # dangling skill reference
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(n1)], None)
    record(env.fixtures, prompt, bad)
    violation = outcome_validator(env.store, PLACEMENT_OUTCOMES)(bad)
    assert "nonexistent skill 999" in violation
    retry_prompt = judge._feedback_prompt(prompt, bad, violation)
    record(env.fixtures, retry_prompt, bad)

    with pytest.raises(JudgeSchemaViolation, match="nonexistent skill 999"):
        run(env)
    assert table_counts(env.store) == before


def _REMOVED_test_judge_unavailable_leaves_zero_rows(env):
    # Transport-level judge failure (here: replay with no fixture recorded).
    seed(env, NEIGHBOR_FIELDS, V_MID)
    before = table_counts(env.store)
    with pytest.raises(JudgeFixtureMissing):
        run(env)
    assert table_counts(env.store) == before


def _REMOVED_test_no_placement_on_placement_call_raises_with_zero_writes(env):
    n1 = seed(env, NEIGHBOR_FIELDS, V_MID)
    before = table_counts(env.store)
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(n1)], None)
    record(env.fixtures, prompt, out("no_placement"))
    with pytest.raises(NoPlacement):
        run(env)
    assert table_counts(env.store) == before


# --- cold start and the relevance floor ------------------------------------------------


def _REMOVED_test_cold_start_taxonomy_prompt_on_empty_library(env):
    prompt = build_taxonomy_prompt(env.store, IDEA_TEXT, None)
    assert "family" in prompt and "generalist" in prompt  # taxonomy listing present
    record(
        env.fixtures,
        prompt,
        out(
            "new_skill",
            new_skill={
                "agent_id": env.agent_id,
                "name": "admin-area-probing",
                "description": "surfacing role-gated areas",
            },
        ),
    )
    result, _ = run(env)
    assert result.code == "registered"
    assert result.judge_outcome == "new_skill"
    assert env.store.skill_members(result.skill_id) == [result.insight_id]


def _REMOVED_test_relevance_floor_routes_low_cosine_candidates_to_taxonomy_prompt(env):
    # A neighbor exists, but at cosine ~0.3 it is below the 0.5 floor: the judge
    # gets the taxonomy listing, not a neighbor list. Consuming the taxonomy
    # fixture (and no placement fixture existing) proves the routing.
    seed(env, NEIGHBOR_FIELDS, V_LOW)
    prompt = build_taxonomy_prompt(env.store, IDEA_TEXT, None)
    record(
        env.fixtures,
        prompt,
        out(
            "new_skill",
            new_skill={
                "agent_id": env.agent_id,
                "name": "admin-area-probing",
                "description": "surfacing role-gated areas",
            },
        ),
    )
    result, _ = run(env)
    assert result.code == "registered"


# --- retired near-duplicates (R14) -----------------------------------------------------


def _REMOVED_test_retired_near_duplicate_triggers_revive_or_override_exit(env):
    dup = seed(env, NEIGHBOR_FIELDS, V_NEAR, status="retired")
    before = table_counts(env.store)
    prompt = build_merge_prompt(env.store, IDEA_TEXT, [neighbor(dup, "retired")])
    record(env.fixtures, prompt, out("merge_discard", duplicate_of=dup))

    with pytest.raises(RetiredNearDuplicate, match="revive") as excinfo:
        run(env)
    assert excinfo.value.insight_id == dup
    assert table_counts(env.store) == before  # no merge-log row, nothing written


def _REMOVED_test_override_retired_admits_the_idea_fresh_via_placement(env):
    dup = seed(env, NEIGHBOR_FIELDS, V_NEAR, status="retired")
    merge_prompt = build_merge_prompt(env.store, IDEA_TEXT, [neighbor(dup, "retired")])
    record(env.fixtures, merge_prompt, out("merge_discard", duplicate_of=dup))
    placement_prompt = build_placement_prompt(
        env.store, IDEA_TEXT, [neighbor(dup, "retired")], None
    )
    record(
        env.fixtures,
        placement_prompt,
        out("append_to_skill", target_skill_id=env.skill_id),
    )

    result, _ = run(env, override_retired=True)

    assert result.code == "registered"
    assert env.store.find_merge_log_by_hash(content_hash(**IDEA)) is None
    assert env.store.get_insight(dup)["status"] == "retired"  # untouched


# --- scope tags --------------------------------------------------------------------------


def _REMOVED_test_judge_scope_tag_override_is_logged_and_stored(env, caplog):
    n1 = seed(env, NEIGHBOR_FIELDS, V_MID)
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(n1)], "domain:web")
    record(env.fixtures, prompt, out("append_to_skill", target_skill_id=env.skill_id))

    with caplog.at_level(logging.WARNING, logger="agent_families.pipeline"):
        result, _ = run(env, scope_tag="domain:web")

    assert result.scope_tag == "universal"  # judge's value wins
    assert env.store.get_insight(result.insight_id)["scope_tag"] == "universal"
    override_logs = [
        r.getMessage() for r in caplog.records if "overrode" in r.getMessage()
    ]
    assert len(override_logs) == 1
    assert "domain:web" in override_logs[0] and "universal" in override_logs[0]


# --- validator unit coverage (R6 reference integrity) --------------------------------------


def _REMOVED_test_outcome_validator_reference_integrity(env):
    check_merge = outcome_validator(env.store, MERGE_REVIEW_OUTCOMES)
    assert "requires duplicate_of" in check_merge(out("merge_discard"))
    assert "nonexistent insight 404" in check_merge(
        out("merge_discard", duplicate_of=404)
    )

    check = outcome_validator(env.store, PLACEMENT_OUTCOMES)
    assert "requires target_skill_id" in check(out("append_to_skill"))
    assert "requires the new_skill object" in check(out("new_skill"))
    assert "nonexistent agent 404" in check(
        out("new_skill", new_skill={"agent_id": 404, "name": "x", "description": "y"})
    )
    assert "already exists" in check(
        out(
            "new_skill",
            new_skill={
                "agent_id": env.agent_id,
                "name": "elicitation",
                "description": "dup name",
            },
        )
    )
    assert "requires supersedes" in check(
        out("contradiction_flag", target_skill_id=env.skill_id)
    )
    assert "nonexistent insight 404" in check(
        out("contradiction_flag", target_skill_id=env.skill_id, supersedes=404)
    )
    assert "requires a placement" in check(out("contradiction_supersede", supersedes=1))
    assert "requires lint.rewrite" in check(out("rewrite_proposed"))
    assert check(out("no_placement")) is None


# =============================================================================
# R3 ingest gauntlet — add_idea_r3 (plan 008 U6)
# =============================================================================
#
# The R3 write path: admission gate (U5) -> generalized content-hash check ->
# embed KEY+FULL clustering vectors -> knn(on="key") candidate filter (NEVER a
# verdict) -> local-NLI verdict per candidate (judge resolve_edge fallback only
# below confidence threshold) -> one transaction writing a quarantined insight +
# 2 vectors + typed edges, authoring NO skill and minting NO snapshot.
#
# Fully offline: the admission gate + resolve_edge fallback replay against judge
# fixtures, NLI replays against nli fixtures, embeddings come from fake encoders.
#
# ## Conformance (008 U6)
#
# Each R11–R15 invariant maps to the behavioral test that enforces it:
#
# - a contradiction colliding at high cosine (~0.95) is NLI-classified
#   `contradiction` and written as a `contradicts` edge ONLY — never merged (the
#   shipped negation-blindness regression)
#     -> test_contradiction_at_high_cosine_not_merged
# - across ALL five moves the `skills` table gains ZERO rows; create_skill /
#   append_member are never called (grouping deferred to plan 009)
#     -> test_no_skill_row_created_in_any_path
# - a contradiction at ingest leaves the incumbent's status unchanged and
#   invalid_at NULL; no snapshot minted (deferred-supersede)
#     -> test_supersede_deferred_leaves_incumbent_live
# - entailment writes a `corroborates` edge, keeps the new insight DISTINCT, and
#   appends a `corroborate` fitness event on the incumbent stamped with the
#   current standing snapshot; the exact-content-hash hit takes the same
#   corroborate path (R15), not a silent no-op
#     -> test_corroborate_keeps_distinct_and_votes
# - an injected failure mid-insert leaves ZERO rows (insight, vectors, edges)
#     -> test_insert_atomic_zero_rows_on_failure
# - NLI confidence below threshold falls back to the LLM judge resolve_edge
#   prompt; above threshold the judge is NEVER called
#     -> test_low_confidence_nli_falls_back_to_judge


def evec(i: int) -> list[float]:
    """The i-th canonical unit basis vector (orthogonal across scenarios)."""
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def fields_n(tag: str) -> dict:
    """A distinct structural atom keyed by ``tag`` (unique content hash + text)."""
    return dict(
        precondition=f"A web target tagged {tag} is being probed for requirements",
        action=f"Ask about {tag}-gated areas during the elicitation interview",
        expected_outcome=f"Hidden {tag} features surface as explicit requirements",
    )


def atom_of(fields: dict, negative_scope: str = "n/a", rationale: str | None = None) -> dict:
    """The generalized atom the gate emits: input fields + a non-empty scope."""
    a = dict(fields)
    a["negative_scope"] = negative_scope or "do not apply outside the precondition"
    if rationale is not None:
        a["rationale"] = rationale
    return a


def record_gate_admit(
    fixtures_dir, fields: dict, atom: dict, *, scope_value="universal", author_scope=None
) -> None:
    prompt = build_admission_gate_prompt(
        fields["precondition"], fields["action"], fields["expected_outcome"], author_scope
    )
    output = {
        "outcome": "admit",
        "atom": dict(atom),
        "scope_tag": {"value": scope_value, "justification": "the rule generalizes"},
    }
    write_fixture(fixtures_dir, prompt, ADMISSION_GATE_SCHEMA, "sonnet", envelope_for(output))


def record_nli(fixtures_dir, incumbent_fields: dict, new_fields: dict, label: str, confidence: float) -> None:
    nli.write_fixture(
        fixtures_dir,
        nli.DEFAULT_MODEL,
        build_idea_text(**incumbent_fields),
        build_idea_text(**new_fields),
        {"label": label, "confidence": confidence, "logits": [0.0, 0.0, 0.0]},
    )


def record_resolve_edge(
    fixtures_dir, incumbent_fields: dict, new_fields: dict, outcome: str, confidence: float = 0.9
) -> None:
    prompt = build_resolve_edge_prompt(
        build_idea_text(**incumbent_fields), build_idea_text(**new_fields)
    )
    output = {"outcome": outcome, "confidence": confidence}
    write_fixture(fixtures_dir, prompt, RESOLVE_EDGE_SCHEMA, "sonnet", envelope_for(output))


def run_r3(env, new_fields, vector, **kwargs):
    embedder, encoder = embedder_for(vector)
    result = add_idea_r3(
        env.store,
        env.vec,
        embedder,
        CFG,
        **new_fields,
        batch_label=kwargs.pop("batch_label", "r3-batch"),
        judge_fixtures_dir=env.fixtures,
        nli_fixtures_dir=env.fixtures,
        nli_mode="replay",
        **kwargs,
    )
    return result, encoder


def edges_of(env, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM insight_edges"
    params: tuple = ()
    if kind is not None:
        sql += " WHERE kind = ?"
        params = (kind,)
    return [dict(r) for r in env.store.conn.execute(sql, params).fetchall()]


def corroborate_events(env, insight_id: int) -> list[dict]:
    return [
        dict(r)
        for r in env.store.conn.execute(
            "SELECT * FROM fitness_events WHERE insight_id = ? AND kind = 'corroborate'",
            (insight_id,),
        ).fetchall()
    ]


# --- the shipped-bug regression (R12) -----------------------------------------


def test_contradiction_at_high_cosine_not_merged(env):
    """A negation colliding at cosine ~0.95 -> `contradicts` edge ONLY, not merged."""
    incumbent = fields_n("admin")
    challenger = dict(
        precondition=incumbent["precondition"],
        action="Skip questions about admin-gated areas during elicitation",
        expected_outcome="Admin features are deliberately left out of requirements",
    )
    # Seed the incumbent at the SAME key vector the challenger embeds to -> cosine
    # ~1.0 (>= candidate floor): the negation sits CLOSER than any paraphrase, the
    # exact case the cosine-verdict bug merged silently.
    inc_id = seed(env, incumbent, V_IDEA, in_skill=False)
    record_gate_admit(env.fixtures, challenger, atom_of(challenger))
    record_nli(env.fixtures, incumbent, challenger, "contradiction", 0.95)

    result, _ = run_r3(env, challenger, V_IDEA)

    assert result.code == "registered"
    # The new insight EXISTS and is distinct — it was not discarded/merged.
    new = env.store.get_insight(result.insight_id)
    assert new is not None and result.insight_id != inc_id
    # A contradicts edge new -> incumbent, and NOTHING merged.
    contradicts = edges_of(env, "contradicts")
    assert len(contradicts) == 1
    assert contradicts[0]["src"] == result.insight_id
    assert contradicts[0]["dst"] == inc_id
    assert env.store.find_merge_log_by_hash(content_hash(**challenger)) is None
    # Incumbent untouched; no snapshot minted at ingest.
    assert env.store.get_insight(inc_id)["status"] == "active"
    assert env.store.get_insight(inc_id)["invalid_at"] is None
    assert env.store.current_snapshot_id() == 0


# --- no group authored (R13) --------------------------------------------------


def test_no_skill_row_created_in_any_path(env, monkeypatch):
    """All five moves author ZERO skills; create_skill/append_member never fire."""
    def _forbidden(*a, **k):
        pytest.fail("the R3 ingest gauntlet must author no skill / membership")

    # Seed all incumbents (orthogonal keys) BEFORE installing the guards.
    cor_inc, cor_new = fields_n("c-inc"), fields_n("c-new")
    ref_inc, ref_new = fields_n("r-inc"), fields_n("r-new")
    con_inc, con_new = fields_n("x-inc"), fields_n("x-new")
    unr_inc, unr_new = fields_n("u-inc"), fields_n("u-new")
    dup = fields_n("dup")
    ci = seed(env, cor_inc, evec(0), in_skill=False)
    ri = seed(env, ref_inc, evec(1), in_skill=False)
    xi = seed(env, con_inc, evec(2), in_skill=False)
    ui = seed(env, unr_inc, evec(3), in_skill=False)
    di = seed(env, dup, evec(4), in_skill=False)

    skills_before = env.store.conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"]
    members_before = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM skill_members"
    ).fetchone()["n"]
    monkeypatch.setattr(env.store, "create_skill", _forbidden)
    monkeypatch.setattr(env.store, "append_member", _forbidden)

    # corroborate (entailment), refine (neutral), contradicts (contradiction),
    # unrelated (low NLI -> judge unrelated), content-hash-hit (corroborate).
    record_gate_admit(env.fixtures, cor_new, atom_of(cor_new))
    record_nli(env.fixtures, cor_inc, cor_new, "entailment", 0.95)
    run_r3(env, cor_new, evec(0))

    record_gate_admit(env.fixtures, ref_new, atom_of(ref_new))
    record_nli(env.fixtures, ref_inc, ref_new, "neutral", 0.95)
    run_r3(env, ref_new, evec(1))

    record_gate_admit(env.fixtures, con_new, atom_of(con_new))
    record_nli(env.fixtures, con_inc, con_new, "contradiction", 0.95)
    run_r3(env, con_new, evec(2))

    record_gate_admit(env.fixtures, unr_new, atom_of(unr_new))
    record_nli(env.fixtures, unr_inc, unr_new, "neutral", 0.40)  # low -> fallback
    record_resolve_edge(env.fixtures, unr_inc, unr_new, "unrelated")
    run_r3(env, unr_new, evec(3))

    record_gate_admit(env.fixtures, dup, atom_of(dup))  # exact-hash hit
    res_dup, _ = run_r3(env, dup, evec(4))
    assert res_dup.code == "corroborated" and res_dup.insight_id == di

    skills_after = env.store.conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"]
    members_after = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM skill_members"
    ).fetchone()["n"]
    assert skills_after == skills_before
    assert members_after == members_before
    assert res_dup.skill_id is None


# --- deferred-supersede (R12/R16) ---------------------------------------------


def test_supersede_deferred_leaves_incumbent_live(env):
    """Contradiction at ingest: edge only — incumbent status + invalid_at untouched."""
    incumbent = fields_n("policy")
    challenger = fields_n("policy-neg")
    inc_id = seed(env, incumbent, V_IDEA, in_skill=False)
    before_status = env.store.get_insight(inc_id)["status"]
    record_gate_admit(env.fixtures, challenger, atom_of(challenger))
    record_nli(env.fixtures, incumbent, challenger, "contradiction", 0.9)

    result, _ = run_r3(env, challenger, V_IDEA)

    assert edges_of(env, "contradicts")  # the conflict is recorded
    incumbent_row = env.store.get_insight(inc_id)
    assert incumbent_row["status"] == before_status == "active"  # not retired
    assert incumbent_row["invalid_at"] is None  # NOT stamped at ingest
    assert env.store.current_snapshot_id() == 0  # no snapshot minted
    # The challenger is in quarantine, never auto-promoted.
    assert env.store.get_insight(result.insight_id)["status"] == "quarantined"


# --- corroboration votes (R12/R15) --------------------------------------------


def test_corroborate_keeps_distinct_and_votes(env):
    """Entailment: corroborates edge + new insight distinct + vote on incumbent."""
    incumbent = fields_n("dupe")
    near = fields_n("dupe-near")
    inc_id = seed(env, incumbent, V_IDEA, in_skill=False)
    record_gate_admit(env.fixtures, near, atom_of(near))
    record_nli(env.fixtures, incumbent, near, "entailment", 0.92)

    result, _ = run_r3(env, near, V_IDEA)

    assert result.code == "registered"
    assert result.insight_id != inc_id  # the new insight is KEPT, distinct
    corr = edges_of(env, "corroborates")
    assert len(corr) == 1 and corr[0]["src"] == result.insight_id and corr[0]["dst"] == inc_id
    # A corroborate fitness event on the incumbent, stamped with the CURRENT
    # standing snapshot (0 — none minted), in the training channel.
    votes = corroborate_events(env, inc_id)
    assert len(votes) == 1
    assert votes[0]["snapshot_id"] == env.store.current_snapshot_id() == 0
    assert votes[0]["mode"] == "training"

    # The exact-content-hash hit takes the SAME corroborate path (R15), not a
    # silent no-op: a second vote lands and no new insight is authored.
    insights_before = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights"
    ).fetchone()["n"]
    record_gate_admit(env.fixtures, incumbent, atom_of(incumbent))
    res2, _ = run_r3(env, incumbent, V_IDEA)
    assert res2.code == "corroborated" and res2.insight_id == inc_id
    assert len(corroborate_events(env, inc_id)) == 2
    assert env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights"
    ).fetchone()["n"] == insights_before  # no new insight


# --- single-transaction atomicity (R7) ----------------------------------------


def test_insert_atomic_zero_rows_on_failure(env, monkeypatch):
    """An injected failure mid-insert leaves ZERO rows (insight, vectors, edges)."""
    incumbent = fields_n("atomic")
    new = fields_n("atomic-near")
    seed(env, incumbent, V_IDEA, in_skill=False)
    record_gate_admit(env.fixtures, new, atom_of(new))
    record_nli(env.fixtures, incumbent, new, "entailment", 0.95)  # -> edge write

    insights_before = env.store.conn.execute("SELECT COUNT(*) AS n FROM insights").fetchone()["n"]
    vec_before = env.vec.count()
    edges_before = len(edges_of(env))
    fitness_before = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM fitness_events"
    ).fetchone()["n"]

    # Fail when the edge is written — mid-transaction, after insight + vectors.
    def _boom(*a, **k):
        raise RuntimeError("injected mid-insert failure")

    monkeypatch.setattr(env.store, "add_insight_edge", _boom)

    with pytest.raises(RuntimeError, match="injected"):
        run_r3(env, new, V_IDEA)

    # Everything rolled back: no insight, no vec row, no edge, no vote.
    assert env.store.conn.execute("SELECT COUNT(*) AS n FROM insights").fetchone()["n"] == insights_before
    assert env.vec.count() == vec_before
    assert len(edges_of(env)) == edges_before
    assert env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM fitness_events"
    ).fetchone()["n"] == fitness_before


# --- NLI primary, judge fallback (R9/R12) -------------------------------------


def test_low_confidence_nli_falls_back_to_judge(env, monkeypatch):
    """Below threshold -> judge resolve_edge fallback; above threshold judge unused."""
    schemas: list = []
    real_run_judge = pipeline.run_judge

    def spy(prompt, schema, *a, **k):
        schemas.append(schema)
        return real_run_judge(prompt, schema, *a, **k)

    monkeypatch.setattr(pipeline, "run_judge", spy)

    # Low confidence -> the judge renders the verdict (contradicts). Orthogonal
    # keys keep the two sub-scenarios' candidate sets disjoint.
    low_inc, low_new = fields_n("fb-low-inc"), fields_n("fb-low-new")
    seed(env, low_inc, evec(0), in_skill=False)
    record_gate_admit(env.fixtures, low_new, atom_of(low_new))
    record_nli(env.fixtures, low_inc, low_new, "neutral", NLI_CONFIDENCE_THRESHOLD_DEFAULT - 0.2)
    record_resolve_edge(env.fixtures, low_inc, low_new, "contradicts")

    run_r3(env, low_new, evec(0))
    assert edges_of(env, "contradicts")  # the judge fallback's verdict landed
    assert RESOLVE_EDGE_SCHEMA in schemas  # the judge WAS consulted

    # Above threshold -> the judge is NEVER called for edge resolution (NLI wins).
    schemas.clear()
    high_inc, high_new = fields_n("fb-high-inc"), fields_n("fb-high-new")
    seed(env, high_inc, evec(1), in_skill=False)
    record_gate_admit(env.fixtures, high_new, atom_of(high_new))
    record_nli(env.fixtures, high_inc, high_new, "contradiction", 0.97)
    # Deliberately record NO resolve_edge fixture: if the judge were consulted it
    # would raise JudgeFixtureMissing.
    run_r3(env, high_new, evec(1))
    assert ADMISSION_GATE_SCHEMA in schemas  # the gate ran
    assert RESOLVE_EDGE_SCHEMA not in schemas  # but the edge judge did NOT
