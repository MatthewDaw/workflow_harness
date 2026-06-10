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

from agent_families import judge
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
    JudgeFixtureMissing,
    JudgeSchemaViolation,
    write_fixture,
)
from agent_families.pipeline import (
    JUDGE_SCHEMA,
    MERGE_REVIEW_OUTCOMES,
    PLACEMENT_OUTCOMES,
    TAXONOMY_OUTCOMES,
    LintRejected,
    NoPlacement,
    RetiredNearDuplicate,
    RewriteProposed,
    StructuralValidationError,
    add_idea,
    build_idea_text,
    build_merge_prompt,
    build_placement_prompt,
    build_taxonomy_prompt,
    canonical_fields_json,
    content_hash,
    outcome_validator,
)
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


# --- happy paths: placement judge -----------------------------------------------


def test_happy_path_append_to_existing_skill(env, caplog):
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


def test_new_skill_created_under_the_right_agent(env):
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


def test_exact_duplicate_exits_before_embedding(env):
    existing = seed(env, IDEA, V_IDEA)
    result, encoder = run(env)
    assert result.code == "exact_duplicate"
    assert result.insight_id == existing
    assert encoder.calls == []  # exits before any encoder call (R5)


def test_near_duplicate_merge_discard_writes_merge_log(env):
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


def test_resubmitting_merged_idea_exits_via_merge_log_fast_path(env):
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


def test_merge_rejection_falls_through_to_placement(env):
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


def test_contradiction_supersede_writes_link_and_flag_leaves_incumbent_active(env):
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


def test_contradiction_flag_opens_flag_without_supersedes_link(env):
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


def test_lint_reject_raises_with_reason_and_writes_nothing(env):
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


def test_rewrite_proposed_then_accept_rewrite_reenters_at_content_hash_check(env):
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


def test_taxonomy_out_of_subset_outcome_retries_then_fails_with_zero_writes(env):
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


def test_judge_failure_after_retries_leaves_zero_rows(env):
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


def test_judge_unavailable_leaves_zero_rows(env):
    # Transport-level judge failure (here: replay with no fixture recorded).
    seed(env, NEIGHBOR_FIELDS, V_MID)
    before = table_counts(env.store)
    with pytest.raises(JudgeFixtureMissing):
        run(env)
    assert table_counts(env.store) == before


def test_no_placement_on_placement_call_raises_with_zero_writes(env):
    n1 = seed(env, NEIGHBOR_FIELDS, V_MID)
    before = table_counts(env.store)
    prompt = build_placement_prompt(env.store, IDEA_TEXT, [neighbor(n1)], None)
    record(env.fixtures, prompt, out("no_placement"))
    with pytest.raises(NoPlacement):
        run(env)
    assert table_counts(env.store) == before


# --- cold start and the relevance floor ------------------------------------------------


def test_cold_start_taxonomy_prompt_on_empty_library(env):
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


def test_relevance_floor_routes_low_cosine_candidates_to_taxonomy_prompt(env):
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


def test_retired_near_duplicate_triggers_revive_or_override_exit(env):
    dup = seed(env, NEIGHBOR_FIELDS, V_NEAR, status="retired")
    before = table_counts(env.store)
    prompt = build_merge_prompt(env.store, IDEA_TEXT, [neighbor(dup, "retired")])
    record(env.fixtures, prompt, out("merge_discard", duplicate_of=dup))

    with pytest.raises(RetiredNearDuplicate, match="revive") as excinfo:
        run(env)
    assert excinfo.value.insight_id == dup
    assert table_counts(env.store) == before  # no merge-log row, nothing written


def test_override_retired_admits_the_idea_fresh_via_placement(env):
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


def test_judge_scope_tag_override_is_logged_and_stored(env, caplog):
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


def test_outcome_validator_reference_integrity(env):
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
