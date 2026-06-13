"""test_u8_authored_ingestion.py — Acceptance tests for U8: authored ingestion.

Covers all 12 acceptance checklist items from MAT-146:

  1. test_user_directive_persists_to_mem_kv_and_bridges_to_graph
  2. test_directive_enters_active_without_pr
  3. test_directive_supersedes_colliding_inferred_idea
  4. test_inferred_idea_cannot_supersede_directive
  5. test_later_directive_supersedes_earlier
  6. test_directive_is_necessity_exempt
  7. test_memory_delete_unbridges_graph_projection
  8. test_pasted_text_decomposes_into_atomic_authored_nodes
  9. test_authored_text_enters_top_authority_necessity_exempt
 10. test_repaste_dedupes_by_content_hash
 11. test_authored_semantic_scan_supersedes_colliding_inferred_without_anchor
 12. test_agent_tool_and_rest_write_same_authored_memory

All tests are offline (no live AWS, no NLI model, no GitHub).
"""
from __future__ import annotations

import json
import time

import pytest

from learning_service.authored import (
    AUTHORITY_AUTHORED_IMPORT,
    AUTHORITY_USER_DIRECTIVE,
    MemKvStore,
    delete_authored_source,
    ingest_directive,
    ingest_pasted_text,
    remember_tool,
    sanitize_authored_text,
    decompose_text_into_paragraphs,
)
from learning_service.db.store import InMemoryLearningStore, OrgGuardError
from learning_service.necessity import triviality_dedup_filter, _AUTHORED_AUTHORITY_KINDS
from learning_service.schema.generated.py_types import IdeaRecord, IdeaSourceRecord

# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

ORG = "test-org"
PROJECT_ID = "proj-001"
USER_ID = "user-alice"
SKILL = "style-guide"
NOW_MS = 1_718_000_000_000


def _make_store() -> InMemoryLearningStore:
    return InMemoryLearningStore()


def _make_mem_store() -> MemKvStore:
    return MemKvStore()


def _seed_inferred_idea(
    store: InMemoryLearningStore,
    idea_id: str = "inferred-001",
    body: str = "Use snake_case for all identifiers.",
    skill: str = SKILL,
) -> IdeaRecord:
    """Seed a typical inferred (PR-gated) idea into the store."""
    idea = IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill,
        org=ORG,
        body=body,
        status="open",
        corroborationVersion=0,
        authorityKind="merged",
    )
    store.put_idea(idea)
    src = IdeaSourceRecord(
        ideaId=idea_id,
        sourceId="pr#42",
        org=ORG,
        prRef="owner/repo#42",
        authorityKind="merged",
        verificationRung="normal",
        authorId="bob",
    )
    store.put_idea_source(src)
    return idea


def _nli_classify_contradiction(premise: str, hypothesis: str):
    """Stub NLI that always returns 'contradiction' for any pair."""
    class _R:
        label = "contradiction"
        confidence = 0.95
    return _R()


def _nli_classify_neutral(premise: str, hypothesis: str):
    """Stub NLI that always returns 'neutral'."""
    class _R:
        label = "neutral"
        confidence = 0.90
    return _R()


_supersede_calls: list[dict] = []


def _stub_supersede(**kwargs):
    """Stub supersede_fn that records calls and stamps invalidAt on the incumbent."""
    _supersede_calls.append(kwargs)


def _make_supersede_fn(store: InMemoryLearningStore):
    """Return a supersede_fn that actually retires the incumbent in the store."""
    def _fn(
        org: str,
        incumbent_idea_id: str,
        incumbent_skill_base_name: str,
        challenger_idea_id: str,
        challenger_authority_kind: str,
        now_ms: int,
    ) -> None:
        idea = store.get_idea(org, incumbent_skill_base_name, incumbent_idea_id)
        if idea is None or idea.invalidAt is not None:
            return
        retired = IdeaRecord(
            ideaId=idea.ideaId,
            skillBaseName=idea.skillBaseName,
            org=idea.org,
            body=idea.body,
            status=idea.status,
            corroborationVersion=idea.corroborationVersion + 1,
            foldedIntoRev=idea.foldedIntoRev,
            invalidAt=now_ms,
            supersededBy=challenger_idea_id,
            supersedes=idea.supersedes,
            authored=idea.authored,
            authorityKind=idea.authorityKind,
            refines=idea.refines,
            revivedAt=idea.revivedAt,
            legacyRecurrenceFold=idea.legacyRecurrenceFold,
            sourceRef=idea.sourceRef,
            scopeTag=idea.scopeTag,
            authorId=idea.authorId,
            verificationRung=idea.verificationRung,
        )
        store.put_idea_conditional(retired, idea.corroborationVersion)
    return _fn


# ---------------------------------------------------------------------------
# 1. test_user_directive_persists_to_mem_kv_and_bridges_to_graph
# ---------------------------------------------------------------------------


def test_user_directive_persists_to_mem_kv_and_bridges_to_graph():
    """A directive must persist to the MEM# KV store AND create an authored idea."""
    store = _make_store()
    mem_store = _make_mem_store()

    result = ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="no-dashes",
        content="Do not use dashes in variable names.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
        idea_id_override="dir-001",
    )

    # MEM# KV: persisted
    entry = mem_store.get(PROJECT_ID, USER_ID, "no-dashes")
    assert entry is not None, "MEM# entry not found"
    assert "dashes" in entry.content
    assert entry.kind == "directive"

    # Graph: idea bridged
    assert len(result.nodes_written) == 1
    node = result.nodes_written[0]
    assert node.idea_id == "dir-001"
    assert node.authority_kind == AUTHORITY_USER_DIRECTIVE
    assert node.is_new is True

    idea = store.get_idea(ORG, SKILL, "dir-001")
    assert idea is not None, "Authored idea not found in store"
    assert idea.authored is True
    assert idea.authorityKind == AUTHORITY_USER_DIRECTIVE

    # sourceRef is JSON-serialised and contains the label + contentHash
    ref = json.loads(idea.sourceRef)
    assert ref["label"] == "no-dashes"
    assert ref["kind"] == AUTHORITY_USER_DIRECTIVE
    assert "contentHash" in ref


# ---------------------------------------------------------------------------
# 2. test_directive_enters_active_without_pr
# ---------------------------------------------------------------------------


def test_directive_enters_active_without_pr():
    """An authored directive must be immediately active (status=open, no PR required)."""
    store = _make_store()
    mem_store = _make_mem_store()

    ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="prefer-list-comp",
        content="Prefer list comprehensions over map() calls.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
        idea_id_override="dir-002",
    )

    idea = store.get_idea(ORG, SKILL, "dir-002")
    assert idea is not None
    assert idea.status == "open", f"Expected status='open', got {idea.status!r}"
    assert idea.invalidAt is None, "Authored idea should NOT have invalidAt set"
    assert idea.foldedIntoRev is None, "Authored idea should NOT be folded (no verified_K)"

    # Verify no prRef on sources (no PR involved)
    sources = store.list_idea_sources(ORG, "dir-002")
    assert all(s.prRef is None for s in sources), "Authored directive should have no prRef"


# ---------------------------------------------------------------------------
# 3. test_directive_supersedes_colliding_inferred_idea
# ---------------------------------------------------------------------------


def test_directive_supersedes_colliding_inferred_idea():
    """An authored directive must supersede a colliding inferred idea (authority wins)."""
    store = _make_store()
    mem_store = _make_mem_store()

    # Seed an inferred idea with overlapping content.
    _seed_inferred_idea(
        store,
        idea_id="inferred-snake",
        body="Use snake_case for all variable names identifiers style.",
    )

    ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="naming-convention",
        content="Use camelCase for all variable names identifiers style.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        nli_classify_fn=_nli_classify_contradiction,
        supersede_fn=_make_supersede_fn(store),
        now_ms=NOW_MS,
        idea_id_override="dir-003",
    )

    # The inferred idea should be retired.
    retired = store.get_idea(ORG, SKILL, "inferred-snake")
    assert retired is not None
    assert retired.invalidAt is not None, "Inferred idea should have been retired by the directive"
    assert retired.supersededBy == "dir-003"


# ---------------------------------------------------------------------------
# 4. test_inferred_idea_cannot_supersede_directive
# ---------------------------------------------------------------------------


def test_inferred_idea_cannot_supersede_directive():
    """An inferred (merged) idea must NEVER be able to supersede an authored directive."""
    from learning_service.supersession import (
        SupersedeRequest,
        supersede,
        FpCalibrationGate,
    )

    store = _make_store()
    mem_store = _make_mem_store()

    # Write an authored directive.
    ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="no-tabs",
        content="Never use tabs; always use 4 spaces for indentation.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
        idea_id_override="dir-auth-001",
    )

    # Seed an inferred idea.
    _seed_inferred_idea(
        store,
        idea_id="inferred-tabs",
        body="Use tabs for indentation.",
    )

    # Attempt to supersede the authored directive with the inferred idea.
    req = SupersedeRequest(
        org=ORG,
        incumbent_idea_id="dir-auth-001",
        incumbent_skill_base_name=SKILL,
        challenger_idea_id="inferred-tabs",
        challenger_skill_base_name=SKILL,
        challenger_pr_number=99,
        challenger_owner_repo="owner/repo",
        challenger_authority_kind="merged",  # inferred
    )

    # Create a gate that is "open" (so authority is the only block).
    gate = FpCalibrationGate(fp_ceiling=1.0, min_samples=0)
    for i in range(30):
        gate.record_verdict(f"v{i}", is_false_positive=False)

    result = supersede(
        req,
        store,
        unfold_mode="enforce",
        fp_gate=gate,
        now_ms=NOW_MS + 1_000,
    )

    assert result.action == "blocked_authority", (
        f"Expected blocked_authority but got {result.action!r}; "
        "inferred idea must not supersede an authored directive"
    )

    # The authored directive must remain active.
    directive = store.get_idea(ORG, SKILL, "dir-auth-001")
    assert directive is not None
    assert directive.invalidAt is None, "Authored directive was incorrectly retired"


# ---------------------------------------------------------------------------
# 5. test_later_directive_supersedes_earlier
# ---------------------------------------------------------------------------


def test_later_directive_supersedes_earlier():
    """A later directive for the same MEM# slot supersedes the earlier one."""
    store = _make_store()
    mem_store = _make_mem_store()

    # Write the first directive.
    ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="separator-style",
        content="Use dashes as word separators.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
        idea_id_override="dir-v1",
    )

    idea_v1 = store.get_idea(ORG, SKILL, "dir-v1")
    assert idea_v1 is not None
    assert idea_v1.invalidAt is None

    # Write a second (later) directive for the same name with different content.
    result = ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="separator-style",
        content="Use underscores as word separators (updated policy).",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS + 60_000,
        idea_id_override="dir-v2",
    )

    # The earlier directive must be retired.
    retired = store.get_idea(ORG, SKILL, "dir-v1")
    assert retired is not None
    assert retired.invalidAt is not None, "Earlier directive should be superseded"
    assert retired.supersededBy == "dir-v2"

    # The new directive is active.
    new_idea = store.get_idea(ORG, SKILL, "dir-v2")
    assert new_idea is not None
    assert new_idea.invalidAt is None

    # The MEM# KV store holds the updated content.
    entry = mem_store.get(PROJECT_ID, USER_ID, "separator-style")
    assert entry is not None
    assert "underscores" in entry.content


# ---------------------------------------------------------------------------
# 6. test_directive_is_necessity_exempt
# ---------------------------------------------------------------------------


def test_directive_is_necessity_exempt():
    """An authored directive must be exempt from the necessity ablation gate."""
    store = _make_store()
    mem_store = _make_mem_store()

    ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="format-rule",
        content="Always end function bodies with a blank line.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
        idea_id_override="dir-exempt",
    )

    idea = store.get_idea(ORG, SKILL, "dir-exempt")
    assert idea is not None
    assert idea.authorityKind in _AUTHORED_AUTHORITY_KINDS, (
        f"authorityKind {idea.authorityKind!r} not in authored kinds"
    )

    # The necessity gate's triviality_dedup_filter must pass (authored are exempt
    # from the "no anchors" check).
    from learning_service.necessity import triviality_dedup_filter, assess_necessity
    result = triviality_dedup_filter(idea, anchors=[], live_ideas=[])
    assert result.passed, f"Authored directive should pass triviality filter: {result.reason}"

    # assess_necessity must return 'exempt' for authored ideas.
    verdict = assess_necessity(idea, store, now_ms=NOW_MS)
    assert verdict.outcome == "exempt", (
        f"Expected 'exempt' for authored directive, got {verdict.outcome!r}"
    )


# ---------------------------------------------------------------------------
# 7. test_memory_delete_unbridges_graph_projection
# ---------------------------------------------------------------------------


def test_memory_delete_unbridges_graph_projection():
    """Deleting a MEM# source must un-bridge all its authored graph nodes."""
    store = _make_store()
    mem_store = _make_mem_store()

    # Write a paste with two paragraphs → two nodes.
    text = (
        "Use consistent naming conventions throughout the project.\n\n"
        "All functions must have docstrings explaining their purpose."
    )
    result = ingest_pasted_text(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        source_name="team-guide",
        text=text,
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
        idea_id_factory=_id_factory(["paste-001", "paste-002"]),
    )

    assert len(result.nodes_written) == 2

    # Confirm both are active.
    for nid in ["paste-001", "paste-002"]:
        idea = store.get_idea(ORG, SKILL, nid)
        assert idea is not None
        assert idea.invalidAt is None

    # Delete the source.
    un_bridged = delete_authored_source(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        source_name="team-guide",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS + 10_000,
    )

    assert set(un_bridged) == {"paste-001", "paste-002"}, (
        f"Expected both nodes un-bridged; got {un_bridged}"
    )

    # Both ideas retired (invalidAt set).
    for nid in ["paste-001", "paste-002"]:
        idea = store.get_idea(ORG, SKILL, nid)
        assert idea is not None
        assert idea.invalidAt is not None, f"Node {nid} was not un-bridged"

    # MEM# KV store no longer has the entry.
    assert mem_store.get(PROJECT_ID, USER_ID, "team-guide") is None

    # But ideas are still queryable as history via get_idea.
    for nid in ["paste-001", "paste-002"]:
        idea = store.get_idea(ORG, SKILL, nid)
        assert idea is not None, f"Node {nid} must be in history (not deleted)"


# ---------------------------------------------------------------------------
# 8. test_pasted_text_decomposes_into_atomic_authored_nodes
# ---------------------------------------------------------------------------


def test_pasted_text_decomposes_into_atomic_authored_nodes():
    """Pasted prose must be split into N atomic authored_import nodes."""
    store = _make_store()
    mem_store = _make_mem_store()

    text = (
        "Always write tests before implementing the feature.\n\n"
        "Keep functions small and focused on a single responsibility.\n\n"
        "Document all public APIs with clear docstrings."
    )

    result = ingest_pasted_text(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        source_name="best-practices",
        text=text,
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
    )

    # Three paragraphs → three nodes.
    assert len(result.nodes_written) == 3, (
        f"Expected 3 nodes, got {len(result.nodes_written)}"
    )
    assert result.nodes_deduped == 0
    assert result.kind == "paste"

    for node in result.nodes_written:
        assert node.authority_kind == AUTHORITY_AUTHORED_IMPORT
        assert node.is_new is True

        idea = store.get_idea(ORG, SKILL, node.idea_id)
        assert idea is not None
        assert idea.authored is True
        assert idea.authorityKind == AUTHORITY_AUTHORED_IMPORT

        # Each node has a distinct contentHash.
        ref = json.loads(idea.sourceRef)
        assert ref["label"] == "best-practices"
        assert ref["kind"] == AUTHORITY_AUTHORED_IMPORT
        assert "contentHash" in ref

    # All hashes are distinct.
    hashes = [n.content_hash for n in result.nodes_written]
    assert len(set(hashes)) == 3, "Content hashes must be unique per paragraph"


# ---------------------------------------------------------------------------
# 9. test_authored_text_enters_top_authority_necessity_exempt
# ---------------------------------------------------------------------------


def test_authored_text_enters_top_authority_necessity_exempt():
    """Authored_import nodes must be top authority (authored=True) and necessity-exempt."""
    from learning_service.necessity import assess_necessity

    store = _make_store()
    mem_store = _make_mem_store()

    text = "Always prefer explicit over implicit when writing Python code."

    result = ingest_pasted_text(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        source_name="python-guide",
        text=text,
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
        idea_id_factory=_id_factory(["paste-auth-001"]),
    )

    assert len(result.nodes_written) == 1
    idea = store.get_idea(ORG, SKILL, "paste-auth-001")
    assert idea is not None
    assert idea.authored is True
    assert idea.authorityKind == AUTHORITY_AUTHORED_IMPORT

    # Necessity gate must return 'exempt'.
    verdict = assess_necessity(idea, store, now_ms=NOW_MS)
    assert verdict.outcome == "exempt", (
        f"authored_import should be exempt from necessity gate; got {verdict.outcome!r}"
    )

    # Status is immediately active (no verified_K threshold).
    assert idea.status == "open"
    assert idea.invalidAt is None


# ---------------------------------------------------------------------------
# 10. test_repaste_dedupes_by_content_hash
# ---------------------------------------------------------------------------


def test_repaste_dedupes_by_content_hash():
    """Re-pasting the same text must not create duplicate authored nodes."""
    store = _make_store()
    mem_store = _make_mem_store()

    text = "Use type annotations for all public function signatures."

    # First paste.
    result1 = ingest_pasted_text(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        source_name="typing-rules",
        text=text,
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
        idea_id_factory=_id_factory(["paste-dedup-001"]),
    )

    assert len(result1.nodes_written) == 1
    assert result1.nodes_deduped == 0

    # Second paste with the same text.
    result2 = ingest_pasted_text(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        source_name="typing-rules",
        text=text,
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS + 5_000,
    )

    assert len(result2.nodes_written) == 0, (
        "Re-paste of identical text should not write a new node"
    )
    assert result2.nodes_deduped == 1, (
        f"Expected 1 deduped node; got {result2.nodes_deduped}"
    )

    # Only one idea exists in the store.
    ideas = store.list_current_ideas(ORG, SKILL)
    authored = [i for i in ideas if i.authorityKind == AUTHORITY_AUTHORED_IMPORT]
    assert len(authored) == 1, f"Expected 1 authored node, found {len(authored)}"


# ---------------------------------------------------------------------------
# 11. test_authored_semantic_scan_supersedes_colliding_inferred_without_anchor
# ---------------------------------------------------------------------------


def test_authored_semantic_scan_supersedes_colliding_inferred_without_anchor():
    """Authored idea's semantic scan supersedes a colliding inferred idea even with no anchor."""
    store = _make_store()
    mem_store = _make_mem_store()

    # Seed an inferred idea (no anchors set — the author directive has none either).
    _seed_inferred_idea(
        store,
        idea_id="inferred-naming",
        body="Use camelCase naming convention for variables identifiers style.",
    )

    # Write an authored directive with opposing content using contradiction NLI.
    result = ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="naming-policy",
        content="Use snake_case naming convention for variables identifiers style.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        nli_classify_fn=_nli_classify_contradiction,
        supersede_fn=_make_supersede_fn(store),
        now_ms=NOW_MS,
        idea_id_override="dir-scan-001",
    )

    # The semantic scan should have triggered a supersession.
    assert "inferred-naming" in result.supersessions, (
        f"Inferred idea should be in supersessions; got {result.supersessions}"
    )

    # The inferred idea is retired.
    retired = store.get_idea(ORG, SKILL, "inferred-naming")
    assert retired is not None
    assert retired.invalidAt is not None, "Inferred idea should be retired by semantic scan"


def test_authored_semantic_scan_does_not_supersede_on_neutral_nli():
    """Authored semantic scan must NOT supersede when NLI returns neutral."""
    store = _make_store()
    mem_store = _make_mem_store()

    _seed_inferred_idea(
        store,
        idea_id="inferred-ok",
        body="Use consistent spacing around operators identifiers style.",
    )

    result = ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="spacing-rule",
        content="Always put spaces around binary operators identifiers style.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        nli_classify_fn=_nli_classify_neutral,
        supersede_fn=_make_supersede_fn(store),
        now_ms=NOW_MS,
        idea_id_override="dir-neutral-001",
    )

    # No supersessions on neutral.
    assert result.supersessions == [], (
        f"Expected no supersessions on neutral NLI; got {result.supersessions}"
    )

    inferred = store.get_idea(ORG, SKILL, "inferred-ok")
    assert inferred is not None
    assert inferred.invalidAt is None, "Neutral NLI must not retire the inferred idea"


# ---------------------------------------------------------------------------
# 12. test_agent_tool_and_rest_write_same_authored_memory
# ---------------------------------------------------------------------------


def test_agent_tool_and_rest_write_same_authored_memory():
    """The agent 'remember' tool and the REST directive path write identical records."""
    store_rest = _make_store()
    mem_store_rest = _make_mem_store()

    store_agent = _make_store()
    mem_store_agent = _make_mem_store()

    content = "Always add type hints to function parameters."
    name = "type-hints-rule"

    # REST path (ingest_directive).
    result_rest = ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name=name,
        content=content,
        skill_base_name=SKILL,
        store=store_rest,
        mem_store=mem_store_rest,
        now_ms=NOW_MS,
        idea_id_override="dir-rest-001",
    )

    # Agent tool path (remember_tool).
    result_agent = remember_tool(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name=name,
        content=content,
        skill_base_name=SKILL,
        store=store_agent,
        mem_store=mem_store_agent,
        now_ms=NOW_MS,
        idea_id_override="dir-agent-001",
    )

    # Both produce exactly one node.
    assert len(result_rest.nodes_written) == 1
    assert len(result_agent.nodes_written) == 1

    # Both nodes have the same authority kind, content hash, and source ref structure.
    rest_node = result_rest.nodes_written[0]
    agent_node = result_agent.nodes_written[0]

    assert rest_node.authority_kind == agent_node.authority_kind == AUTHORITY_USER_DIRECTIVE
    assert rest_node.content_hash == agent_node.content_hash, (
        "REST and agent paths must produce the same contentHash"
    )

    # MEM# KV stores have the same content.
    rest_entry = mem_store_rest.get(PROJECT_ID, USER_ID, name)
    agent_entry = mem_store_agent.get(PROJECT_ID, USER_ID, name)
    assert rest_entry is not None
    assert agent_entry is not None
    assert rest_entry.content == agent_entry.content

    # Both ideas have identical structure (except idea_id).
    rest_idea = store_rest.get_idea(ORG, SKILL, "dir-rest-001")
    agent_idea = store_agent.get_idea(ORG, SKILL, "dir-agent-001")
    assert rest_idea is not None
    assert agent_idea is not None
    assert rest_idea.authored == agent_idea.authored == True
    assert rest_idea.authorityKind == agent_idea.authorityKind
    assert rest_idea.status == agent_idea.status == "open"
    assert rest_idea.invalidAt is None
    assert agent_idea.invalidAt is None


# ---------------------------------------------------------------------------
# Additional robustness tests
# ---------------------------------------------------------------------------


def test_org_guard_on_directive():
    """Blank org must raise OrgGuardError."""
    store = _make_store()
    mem_store = _make_mem_store()
    with pytest.raises(OrgGuardError):
        ingest_directive(
            org="",
            project_id=PROJECT_ID,
            user_id=USER_ID,
            name="x",
            content="y",
            skill_base_name=SKILL,
            store=store,
            mem_store=mem_store,
        )


def test_org_guard_on_pasted_text():
    """Blank org must raise OrgGuardError for paste ingestion."""
    store = _make_store()
    mem_store = _make_mem_store()
    with pytest.raises(OrgGuardError):
        ingest_pasted_text(
            org="",
            project_id=PROJECT_ID,
            user_id=USER_ID,
            source_name="x",
            text="A reasonably long paragraph of text.",
            skill_base_name=SKILL,
            store=store,
            mem_store=mem_store,
        )


def test_size_cap_truncates_large_text():
    """Text exceeding the size cap must be truncated before ingestion."""
    large_text = "A" * 70_000  # > 64 KB cap
    result = sanitize_authored_text(large_text)
    assert len(result.encode("utf-8")) <= 64_000 + 10  # small tolerance for decode


def test_secret_scrub_removes_aws_key():
    """AWS AKIA key IDs must be scrubbed from authored text."""
    text = "Use AKIAIOSFODNN7EXAMPLE to authenticate."
    result = sanitize_authored_text(text)
    assert "AKIA" not in result
    assert "<AWS_KEY>" in result


def test_secret_scrub_removes_bearer_token():
    """Bearer tokens must be scrubbed."""
    text = "Set Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def in requests."
    result = sanitize_authored_text(text)
    assert "Bearer <TOKEN>" in result or "eyJ" not in result


def test_decompose_text_splits_on_blank_lines():
    """decompose_text_into_paragraphs must split on blank lines."""
    text = "First claim.\n\nSecond claim.\n\nThird claim."
    paras = decompose_text_into_paragraphs(text)
    assert len(paras) == 3
    assert paras[0] == "First claim."
    assert paras[2] == "Third claim."


def test_decompose_text_drops_trivial_paragraphs():
    """Paragraphs shorter than 10 characters must be dropped."""
    text = "Short.\n\nA reasonably long paragraph that has enough content."
    paras = decompose_text_into_paragraphs(text)
    assert len(paras) == 1
    assert "reasonably" in paras[0]


def test_authored_ingestion_handler_directive():
    """The Lambda handler dispatches directive kind correctly."""
    from learning_service.entrypoints.authored_ingestion import handler

    store = _make_store()
    mem_store = _make_mem_store()

    event = {
        "body": json.dumps({
            "kind": "directive",
            "org": ORG,
            "projectId": PROJECT_ID,
            "userId": USER_ID,
            "name": "handler-test",
            "content": "Always use type annotations.",
            "skillBaseName": SKILL,
        }),
        "_test_store": store,
        "_test_mem_store": mem_store,
    }

    resp = handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["nodesWritten"] == 1
    assert body["nodesDeduped"] == 0
    assert body["kind"] == "directive"


def test_authored_ingestion_handler_delete():
    """The Lambda handler dispatches delete kind correctly."""
    from learning_service.entrypoints.authored_ingestion import handler

    store = _make_store()
    mem_store = _make_mem_store()

    # First write a directive.
    ingest_directive(
        org=ORG,
        project_id=PROJECT_ID,
        user_id=USER_ID,
        name="to-delete",
        content="This directive will be deleted shortly.",
        skill_base_name=SKILL,
        store=store,
        mem_store=mem_store,
        now_ms=NOW_MS,
        idea_id_override="dir-del-001",
    )

    event = {
        "body": json.dumps({
            "kind": "delete",
            "org": ORG,
            "projectId": PROJECT_ID,
            "userId": USER_ID,
            "name": "to-delete",
            "skillBaseName": SKILL,
        }),
        "_test_store": store,
        "_test_mem_store": mem_store,
    }

    resp = handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert "dir-del-001" in body["unBridged"]


def test_authored_ingestion_handler_missing_field():
    """The Lambda handler returns 400 for missing required fields."""
    from learning_service.entrypoints.authored_ingestion import handler

    event = {
        "body": json.dumps({
            "kind": "directive",
            "org": ORG,
            # projectId missing
            "userId": USER_ID,
            "name": "test",
            "content": "test",
            "skillBaseName": SKILL,
        }),
    }

    resp = handler(event, None)
    assert resp["statusCode"] == 400


def test_authored_ingestion_handler_unknown_kind():
    """The Lambda handler returns 400 for an unknown kind."""
    from learning_service.entrypoints.authored_ingestion import handler

    event = {
        "body": json.dumps({
            "kind": "unknown-kind",
            "org": ORG,
            "projectId": PROJECT_ID,
            "userId": USER_ID,
            "name": "test",
            "content": "test",
            "skillBaseName": SKILL,
        }),
    }

    resp = handler(event, None)
    assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# Helper: deterministic ID factory
# ---------------------------------------------------------------------------


def _id_factory(ids: list[str]):
    """Return a factory that yields ids in order, then falls back to uuid."""
    import uuid
    counter = [0]
    def _factory():
        if counter[0] < len(ids):
            val = ids[counter[0]]
            counter[0] += 1
            return val
        return str(uuid.uuid4())
    return _factory
