"""U7: byte-stable concat rendering and delta-patch compile (R15/R16).

Fully offline: the compile path runs the judge seam in replay mode against
fixtures written by the test itself — zero subprocess calls, zero quota.
"""

from __future__ import annotations

import pytest

from agent_families.judge import JudgeSchemaViolation, write_fixture
from agent_families.rendering import (
    COMPILE_SCHEMA,
    Renderer,
    RenderingError,
    extract_provenance,
)
from agent_families.store import Store

MODEL = "sonnet"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def skill_id(store):
    family_id = store.create_family("engineering")
    agent_id = store.create_agent(family_id, "builder")
    return store.create_skill(
        agent_id, "windows-subprocess", "Subprocess discipline on Windows."
    )


def add_member(store, skill_id, key, status="quarantined"):
    insight_id = store.insert_insight(
        precondition=f"pre-{key}",
        action=f"act-{key}",
        expected_outcome=f"out-{key}",
        content_hash=f"hash-{key}",
        status=status,
    )
    store.append_member(skill_id, insight_id)
    return insight_id


def promote(store, insight_ids):
    with store.queue_operation("promote", "test batch") as snapshot_id:
        for insight_id in insight_ids:
            store.set_status(insight_id, "active", snapshot_id)
        return snapshot_id


def good_compile_envelope(sections):
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 2100,
        "num_turns": 1,
        "result": "ok",
        "total_cost_usd": 0.003,
        "structured_output": {"sections": sections},
    }


# --- concatenation: byte stability (R15) --------------------------------------


def test_same_inputs_render_byte_identical_across_runs(store, skill_id, tmp_path):
    ids = [add_member(store, skill_id, k) for k in ("a", "b", "c")]
    promote(store, ids)
    first = Renderer(store).render_concat(skill_id)
    # A separate connection and a separate Renderer: same bytes, not just same str.
    with Store(tmp_path / "library.db") as reopened:
        second = Renderer(reopened).render_concat(skill_id)
    assert isinstance(first.content, bytes)
    assert first.content == second.content
    assert b"\r" not in first.content  # newline discipline pinned to \n
    assert first.content.endswith(b"\n")


def test_adding_insight_changes_only_the_appended_region(store, skill_id):
    promote(store, [add_member(store, skill_id, k) for k in ("a", "b")])
    old = Renderer(store).render_concat(skill_id)
    promote(store, [add_member(store, skill_id, "c")])
    new = Renderer(store).render_concat(skill_id)
    assert new.content[: len(old.content)] == old.content
    tail = new.content[len(old.content) :]
    assert tail.startswith(b"\n## Insight")
    assert tail.count(b"## Insight") == 1
    assert b"act-c" in tail


def test_render_at_old_snapshot_reproduces_old_bytes(store, skill_id):
    snap1 = promote(store, [add_member(store, skill_id, "a")])
    old = Renderer(store).render_concat(skill_id)
    assert old.snapshot_id == snap1
    promote(store, [add_member(store, skill_id, "b")])
    renderer = Renderer(store)  # fresh cache: replay must recompute, not remember
    current = renderer.render_concat(skill_id)
    assert current.content != old.content
    replayed = renderer.render_concat(skill_id, snapshot_id=snap1)
    assert replayed.content == old.content
    assert replayed.from_cache is False


# --- concatenation: visibility (R13) -------------------------------------------


def test_quarantined_excluded_by_default_included_with_flag(store, skill_id):
    active_id = add_member(store, skill_id, "a")
    promote(store, [active_id])
    quarantined_id = add_member(store, skill_id, "q")
    renderer = Renderer(store)
    default = renderer.render_concat(skill_id)
    assert f"## Insight {active_id}".encode() in default.content
    assert f"## Insight {quarantined_id}".encode() not in default.content
    flagged = renderer.render_concat(skill_id, include_quarantined=True)
    assert f"## Insight {quarantined_id}".encode() in flagged.content
    assert flagged.content[: len(default.content)] == default.content


def test_retired_member_never_renders(store, skill_id):
    insight_id = add_member(store, skill_id, "a")
    promote(store, [insight_id])
    with store.queue_operation("retire", "test") as snapshot_id:
        store.set_status(insight_id, "retired", snapshot_id)
    renderer = Renderer(store)
    assert renderer.render_concat(skill_id).empty
    assert renderer.render_concat(skill_id, include_quarantined=True).empty


# --- concatenation: cache (R15) -------------------------------------------------


def test_cached_active_render_stable_while_quarantine_view_is_fresh(store, skill_id):
    promote(store, [add_member(store, skill_id, "a")])
    renderer = Renderer(store)
    first = renderer.render_concat(skill_id)
    assert first.from_cache is False
    # Registration appends a quarantined member and mints no snapshot: the
    # active-only render is served from cache, byte-identical.
    new_id = add_member(store, skill_id, "q")
    again = renderer.render_concat(skill_id)
    assert again.from_cache is True
    assert again.content == first.content
    # ...while the quarantine-inclusive view computes fresh and sees it at once.
    flagged = renderer.render_concat(skill_id, include_quarantined=True)
    assert flagged.from_cache is False
    assert f"## Insight {new_id}".encode() in flagged.content
    later_id = add_member(store, skill_id, "q2")
    flagged2 = renderer.render_concat(skill_id, include_quarantined=True)
    assert flagged2.from_cache is False
    assert f"## Insight {later_id}".encode() in flagged2.content


# --- concatenation: empty skill ---------------------------------------------------


def test_empty_skill_renders_empty_body_and_is_flagged(store, skill_id):
    rendering = Renderer(store).render_concat(skill_id)
    assert rendering.empty is True
    expected_header = (
        "# Skill: windows-subprocess\n\nSubprocess discipline on Windows.\n\n---\n"
    )
    assert rendering.content == expected_header.encode("utf-8")
    assert b"## Insight" not in rendering.content


def test_unknown_skill_raises(store):
    with pytest.raises(RenderingError, match="skill 999"):
        Renderer(store).render_concat(999)


# --- derived membership: stable order + NULL-safe names (U8, R17) -----------------


def test_render_order_is_derive_stable_not_position(store, skill_id):
    """R17: members render in insight_id order, never skill_members.position.

    A derive pass rebalances membership wholesale, so append-position is
    meaningless. A fixture that scrambles position while preserving membership
    must render byte-identical output — a position-ordered renderer fails here.
    """
    ids = [add_member(store, skill_id, k) for k in ("a", "b", "c")]
    promote(store, ids)  # mints the snapshot membership renders at
    before = Renderer(store).render_concat(skill_id).content
    # Bytes are in ascending insight_id order: act-a precedes act-b precedes act-c.
    assert before.index(b"act-a") < before.index(b"act-b") < before.index(b"act-c")

    # Scramble position to the exact reverse of insight_id order, membership intact.
    # Shift out of the live range first so the per-row UNIQUE(skill_id, position)
    # constraint never trips mid-reassignment.
    store.conn.execute(
        "UPDATE skill_members SET position = position + 1000 WHERE skill_id = ?",
        (skill_id,),
    )
    for pos, insight_id in enumerate(reversed(ids)):
        store.conn.execute(
            "UPDATE skill_members SET position = ? WHERE skill_id = ? AND insight_id = ?",
            (pos, skill_id, insight_id),
        )
    store.conn.commit()
    assert store.skill_members(skill_id) == list(reversed(ids))  # position truly reversed

    after = Renderer(store).render_concat(skill_id).content  # fresh renderer: recomputes
    assert after == before  # derive-stable order: position scramble changes nothing


def test_unnamed_module_renders_placeholder_no_none(store):
    """R17: a NULL-named derived module renders a placeholder, never ``None``."""
    family_id = store.create_family("engineering")
    agent_id = store.create_agent(family_id, "builder")
    module_id = store.create_skill(agent_id, None, "")  # unnamed derived module
    insight_id = add_member(store, module_id, "a")
    promote(store, [insight_id])
    content = Renderer(store).render_concat(module_id).content
    assert b"None" not in content
    assert b"# Skill: None" not in content
    assert f"# Skill: module-{module_id}".encode() in content


def test_cache_invalidated_only_via_snapshot(store, skill_id):
    """R17: a membership rebalance mints a snapshot, so the cache cannot serve stale.

    The render cache is keyed (skill_id, snapshot_id). Because every rebalance
    mints a snapshot (R8), the rebalanced membership lands on a fresh key — a cache
    miss that reflects the change — while the prior snapshot's bytes stay pinned.
    """
    id_a = add_member(store, skill_id, "a")
    id_b = add_member(store, skill_id, "b")
    snap1 = promote(store, [id_a, id_b])
    renderer = Renderer(store)
    first = renderer.render_concat(skill_id)
    assert first.from_cache is False and first.snapshot_id == snap1
    assert f"## Insight {id_b}".encode() in first.content

    # Rebalance: drop b from membership inside a queue op, which mints a snapshot.
    with store.queue_operation("derive", "rebalance") as snap2:
        store.conn.execute(
            "DELETE FROM skill_members WHERE skill_id = ? AND insight_id = ?",
            (skill_id, id_b),
        )
    assert snap2 != snap1

    rebalanced = renderer.render_concat(skill_id)  # current snapshot is now snap2
    assert rebalanced.snapshot_id == snap2
    assert rebalanced.from_cache is False  # new snapshot key => cache miss, fresh bytes
    assert f"## Insight {id_b}".encode() not in rebalanced.content
    assert f"## Insight {id_a}".encode() in rebalanced.content
    # The prior snapshot's bytes remain pinned under their own key (distinct entries).
    pinned = renderer.render_concat(skill_id, snapshot_id=snap1)
    assert pinned.from_cache is True
    assert pinned.content == first.content


# --- compile (R16) -----------------------------------------------------------------


@pytest.fixture
def compiled_dir(tmp_path):
    return tmp_path / "compiled"


@pytest.fixture
def fixtures_dir(tmp_path):
    return tmp_path / "judge-fixtures"


def record_compile_fixture(renderer, skill_id, fixtures_dir, structured_output):
    prompt = renderer.compile_prompt(skill_id)
    envelope = good_compile_envelope([])
    envelope["structured_output"] = structured_output
    write_fixture(fixtures_dir, prompt, COMPILE_SCHEMA, MODEL, envelope)
    return prompt


def test_compile_success_persists_resolvable_provenance(
    store, skill_id, compiled_dir, fixtures_dir
):
    id_a = add_member(store, skill_id, "a")
    id_b = add_member(store, skill_id, "b")
    snap = promote(store, [id_a, id_b])
    renderer = Renderer(store, compiled_dir)
    rows_before = [tuple(store.get_insight(i)) for i in (id_a, id_b)]
    record_compile_fixture(
        renderer,
        skill_id,
        fixtures_dir,
        {
            "sections": [
                {"heading": "Encoding", "body": "Pin utf-8.", "insight_ids": [id_a]},
                {
                    "heading": "Entrypoints",
                    "body": "Resolve node entry scripts.",
                    "insight_ids": [id_a, id_b],
                },
            ]
        },
    )
    doc = renderer.compile_skill(
        skill_id, model=MODEL, max_retries=0, mode="replay", fixtures_dir=fixtures_dir
    )
    assert doc.snapshot_id == snap
    assert doc.path.read_bytes() == doc.content
    assert renderer.get_compiled(skill_id, snap) == doc.content
    # Per-section annotations parse back out and resolve to real member rows.
    provenance = extract_provenance(doc.content)
    assert provenance == [[id_a], [id_a, id_b]]
    assert doc.provenance == ((id_a,), (id_a, id_b))
    members = set(store.skill_members(skill_id))
    for section_ids in provenance:
        for insight_id in section_ids:
            assert store.get_insight(insight_id) is not None
            assert insight_id in members
    # Compilation never modifies insights.
    assert [tuple(store.get_insight(i)) for i in (id_a, id_b)] == rows_before


def test_compile_failure_leaves_previous_compiled_doc_intact(
    store, skill_id, compiled_dir, fixtures_dir
):
    id_a = add_member(store, skill_id, "a")
    snap1 = promote(store, [id_a])
    renderer = Renderer(store, compiled_dir)
    record_compile_fixture(
        renderer,
        skill_id,
        fixtures_dir,
        {"sections": [{"heading": "One", "body": "Body.", "insight_ids": [id_a]}]},
    )
    good = renderer.compile_skill(
        skill_id, model=MODEL, max_retries=0, mode="replay", fixtures_dir=fixtures_dir
    )
    # Library grows; the next compile replays a malformed structured output.
    id_b = add_member(store, skill_id, "b")
    snap2 = promote(store, [id_b])
    record_compile_fixture(
        renderer, skill_id, fixtures_dir, {"sections": "not-a-list"}
    )
    with pytest.raises(JudgeSchemaViolation):
        renderer.compile_skill(
            skill_id,
            model=MODEL,
            max_retries=0,
            mode="replay",
            fixtures_dir=fixtures_dir,
        )
    assert renderer.compiled_path(skill_id, snap1).read_bytes() == good.content
    assert renderer.get_compiled(skill_id, snap2) is None
    assert list(compiled_dir.glob("*.tmp")) == []


def test_compile_rejects_dangling_provenance_and_writes_nothing(
    store, skill_id, compiled_dir, fixtures_dir
):
    id_a = add_member(store, skill_id, "a")
    promote(store, [id_a])
    renderer = Renderer(store, compiled_dir)
    record_compile_fixture(
        renderer,
        skill_id,
        fixtures_dir,
        {"sections": [{"heading": "One", "body": "Body.", "insight_ids": [999]}]},
    )
    with pytest.raises(JudgeSchemaViolation, match="999"):
        renderer.compile_skill(
            skill_id,
            model=MODEL,
            max_retries=0,
            mode="replay",
            fixtures_dir=fixtures_dir,
        )
    assert not compiled_dir.exists()


def test_compile_empty_skill_refused(store, skill_id, compiled_dir, fixtures_dir):
    with pytest.raises(RenderingError, match="nothing to compile"):
        Renderer(store, compiled_dir).compile_skill(
            skill_id, model=MODEL, max_retries=0, mode="replay",
            fixtures_dir=fixtures_dir,
        )


def test_compile_without_compiled_dir_fails_before_judge_call(
    store, skill_id, fixtures_dir
):
    promote(store, [add_member(store, skill_id, "a")])
    # No fixture exists: reaching the judge would raise JudgeFixtureMissing
    # instead, so RenderingError proves the config check precedes judge spend.
    with pytest.raises(RenderingError, match="compiled_dir"):
        Renderer(store).compile_skill(
            skill_id, model=MODEL, max_retries=0, mode="replay",
            fixtures_dir=fixtures_dir,
        )


# ## Conformance (plan-009 U8, R17)
#
# Each named R17 invariant maps to the behavioral test that enforces it:
#
# - "rendering orders members by a derive-stable key, not skill_members.position"
#       -> test_render_order_is_derive_stable_not_position
# - "an unnamed (NULL name/description) module renders a placeholder, never None"
#       -> test_unnamed_module_renders_placeholder_no_none
# - "the snapshot-keyed render cache cannot serve stale bytes for changed
#    membership, because every rebalance mints a snapshot (R8)"
#       -> test_cache_invalidated_only_via_snapshot
