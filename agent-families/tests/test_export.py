"""U8: SKILL.md export (R17).

Fully offline: bodies come from concat renderings or a pre-written compiled
document — no judge calls, no subprocesses, no quota.
"""

from __future__ import annotations

import pytest
import yaml

from agent_families.export import (
    SLUG_MAX_LENGTH,
    export_skills,
    slugify,
)
from agent_families.rendering import Renderer
from agent_families.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def agent_id(store):
    family_id = store.create_family("engineering")
    return store.create_agent(family_id, "builder")


@pytest.fixture
def out_dir(tmp_path):
    return tmp_path / "exported-skills"


def make_skill(store, agent_id, name, description="A load-bearing description."):
    return store.create_skill(agent_id, name, description)


def add_active_member(store, skill_id, key):
    insight_id = store.insert_insight(
        precondition=f"pre-{key}",
        action=f"act-{key}",
        expected_outcome=f"out-{key}",
        content_hash=f"hash-{skill_id}-{key}",
        status="quarantined",
    )
    store.append_member(skill_id, insight_id)
    with store.queue_operation("promote", "test batch") as snapshot_id:
        store.set_status(insight_id, "active", snapshot_id)
    return insight_id


def parse_skill_md(path):
    raw = path.read_bytes().decode("utf-8")
    assert raw.startswith("---\n")
    end = raw.index("\n---\n", 4)
    frontmatter = yaml.safe_load(raw[4:end])
    body = raw[end + len("\n---\n") :]
    assert body.startswith("\n")  # blank line separates frontmatter from body
    return frontmatter, body[1:]


def snapshot_tree(root):
    return {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


# --- frontmatter + body ----------------------------------------------------------


def test_exported_file_parses_as_valid_frontmatter_plus_body(
    store, agent_id, out_dir
):
    skill_id = make_skill(
        store, agent_id, "Windows Subprocess", 'Quotes "and" colons: handled.'
    )
    add_active_member(store, skill_id, "a")
    renderer = Renderer(store)
    report = export_skills(renderer, out_dir)
    (entry,) = report.exported
    assert entry.path == out_dir / "windows-subprocess" / "SKILL.md"
    frontmatter, body = parse_skill_md(entry.path)
    assert frontmatter == {
        "name": "windows-subprocess",
        "description": 'Quotes "and" colons: handled.',
    }
    # Body is the current concat rendering, byte for byte.
    assert body.encode("utf-8") == renderer.render_concat(skill_id).content
    assert entry.compiled is False


def test_compiled_rendering_preferred_over_concat_when_present(
    store, agent_id, out_dir, tmp_path
):
    skill_id = make_skill(store, agent_id, "compiled-skill")
    add_active_member(store, skill_id, "a")
    renderer = Renderer(store, tmp_path / "compiled")
    compiled = b"# Skill: compiled-skill (compiled)\n\n## One\nBody.\n"
    path = renderer.compiled_path(skill_id, store.current_snapshot_id())
    path.parent.mkdir(parents=True)
    path.write_bytes(compiled)
    report = export_skills(renderer, out_dir)
    (entry,) = report.exported
    assert entry.compiled is True
    _, body = parse_skill_md(entry.path)
    assert body.encode("utf-8") == compiled


# --- slugification -----------------------------------------------------------------


def test_slugify_handles_spaces_punctuation_and_length():
    assert slugify("Windows Subprocess Discipline") == "windows-subprocess-discipline"
    assert slugify("  C++/CLI: tips & tricks!  ") == "c-cli-tips-tricks"
    assert slugify("a" * 100) == "a" * SLUG_MAX_LENGTH
    capped = slugify("word " * 30)
    assert len(capped) <= SLUG_MAX_LENGTH
    assert not capped.endswith("-")  # truncation never leaves a trailing hyphen
    assert slugify("!!!") == "skill"  # all-punctuation names get the fallback


def test_export_uses_slugified_directory_name(store, agent_id, out_dir):
    skill_id = make_skill(store, agent_id, "  Git: Rebase & Merge!  ")
    add_active_member(store, skill_id, "a")
    report = export_skills(Renderer(store), out_dir)
    (entry,) = report.exported
    assert entry.slug == "git-rebase-merge"
    assert entry.path == out_dir / "git-rebase-merge" / "SKILL.md"
    assert entry.path.exists()


# --- collision suffixes ---------------------------------------------------------------


def test_identical_slugs_get_stable_distinct_suffixes(store, out_dir):
    family_id = store.create_family("engineering")
    agent_a = store.create_agent(family_id, "builder")
    agent_b = store.create_agent(family_id, "reviewer")
    first = make_skill(store, agent_a, "My Skill")
    second = make_skill(store, agent_b, "my skill!")  # same slug: my-skill
    add_active_member(store, first, "a")
    add_active_member(store, second, "b")
    renderer = Renderer(store)
    report = export_skills(renderer, out_dir)
    slugs = {e.skill_id: e.slug for e in report.exported}
    assert slugs == {first: "my-skill", second: "my-skill-2"}
    # Stable regardless of caller-supplied order, and across re-export.
    report_reversed = export_skills(renderer, out_dir, skill_ids=[second, first])
    assert {e.skill_id: e.slug for e in report_reversed.exported} == slugs
    frontmatter, _ = parse_skill_md(out_dir / "my-skill-2" / "SKILL.md")
    assert frontmatter["name"] == "my-skill-2"


def test_suffixed_slug_respects_length_cap(store, out_dir):
    family_id = store.create_family("engineering")
    agent_a = store.create_agent(family_id, "builder")
    agent_b = store.create_agent(family_id, "reviewer")
    long_name = "x" * 100
    first = make_skill(store, agent_a, long_name)
    second = make_skill(store, agent_b, long_name)
    add_active_member(store, first, "a")
    add_active_member(store, second, "b")
    report = export_skills(Renderer(store), out_dir)
    slugs = [e.slug for e in report.exported]
    assert slugs[0] == "x" * SLUG_MAX_LENGTH
    assert slugs[1] == "x" * (SLUG_MAX_LENGTH - 2) + "-2"
    assert all(len(s) <= SLUG_MAX_LENGTH for s in slugs)


# --- empty skills ----------------------------------------------------------------------


def test_empty_skill_is_skipped_and_reported(store, agent_id, out_dir):
    populated = make_skill(store, agent_id, "populated")
    empty = make_skill(store, agent_id, "still-empty")
    add_active_member(store, populated, "a")
    report = export_skills(Renderer(store), out_dir)
    assert [e.skill_id for e in report.exported] == [populated]
    (skip,) = report.skipped
    assert skip.skill_id == empty
    assert skip.name == "still-empty"
    assert "no active insights" in skip.notice
    assert report.notices == (skip.notice,)
    assert not (out_dir / "still-empty").exists()


def test_quarantined_only_skill_counts_as_empty(store, agent_id, out_dir):
    skill_id = make_skill(store, agent_id, "quarantine-only")
    insight_id = store.insert_insight(
        precondition="pre",
        action="act",
        expected_outcome="out",
        content_hash="hash-q",
        status="quarantined",
    )
    store.append_member(skill_id, insight_id)
    report = export_skills(Renderer(store), out_dir)
    assert report.exported == ()
    assert [s.skill_id for s in report.skipped] == [skill_id]


# --- idempotency -------------------------------------------------------------------------


def test_reexport_over_existing_dir_is_idempotent(store, out_dir):
    family_id = store.create_family("engineering")
    agent_a = store.create_agent(family_id, "builder")
    agent_b = store.create_agent(family_id, "reviewer")
    for agent, name in ((agent_a, "My Skill"), (agent_b, "my skill!"), (agent_a, "other")):
        add_active_member(store, make_skill(store, agent, name), "a")
    renderer = Renderer(store)
    export_skills(renderer, out_dir)
    first = snapshot_tree(out_dir)
    assert first  # the comparison below is vacuous on an empty tree
    export_skills(renderer, out_dir)
    assert snapshot_tree(out_dir) == first


def test_unknown_skill_id_raises(store, out_dir):
    with pytest.raises(ValueError, match="999"):
        export_skills(Renderer(store), out_dir, skill_ids=[999])


# --- derived membership: NULL-safe names + naming trigger (U8, R18) ----------------


def add_active_member_to(store, skill_id, key):
    insight_id = store.insert_insight(
        precondition=f"pre-{key}",
        action=f"act-{key}",
        expected_outcome=f"out-{key}",
        content_hash=f"hash-{skill_id}-{key}",
        status="quarantined",
    )
    store.append_member(skill_id, insight_id)
    with store.queue_operation("promote", "test batch") as snapshot_id:
        store.set_status(insight_id, "active", snapshot_id)
    return insight_id


def test_export_null_safe_slug_fallback(store, agent_id, out_dir):
    """R18: a NULL-named module exports with the ``module-{id}`` fallback slug.

    No namer is supplied, so the name stays NULL: slugify/_skill_md must produce a
    valid SKILL.md (no exception, no empty slug, no literal ``None``).
    """
    module_id = store.create_skill(agent_id, None, "")  # unnamed derived module
    add_active_member_to(store, module_id, "a")
    report = export_skills(Renderer(store), out_dir)
    (entry,) = report.exported
    assert entry.slug == f"module-{module_id}"
    assert entry.path == out_dir / f"module-{module_id}" / "SKILL.md"
    assert entry.path.exists()
    frontmatter, body = parse_skill_md(entry.path)  # parses => valid YAML frontmatter
    assert frontmatter == {"name": f"module-{module_id}", "description": ""}
    assert "None" not in entry.path.read_bytes().decode("utf-8")


def test_export_triggers_naming_for_leaf_community_first(store, agent_id, out_dir):
    """R18: a supplied namer names the NULL leaf community before it is written."""
    module_id = store.create_skill(agent_id, None, "")
    add_active_member_to(store, module_id, "a")
    calls: list[tuple[int, tuple[int, ...]]] = []

    def namer(s, skill_id, members):
        calls.append((skill_id, tuple(members)))
        return "Resolved Name", "A resolved description."

    report = export_skills(Renderer(store), out_dir, namer_fn=namer)
    # The namer was invoked exactly once, for the unnamed leaf community.
    assert len(calls) == 1
    assert calls[0][0] == module_id
    (entry,) = report.exported
    assert entry.slug == "resolved-name"  # slug now comes from the resolved name
    frontmatter, body = parse_skill_md(entry.path)
    assert frontmatter == {
        "name": "resolved-name",
        "description": "A resolved description.",
    }
    # Naming is persisted and precedes rendering: the body header carries the name.
    assert body.encode("utf-8") == Renderer(store).render_concat(module_id).content
    assert b"# Skill: Resolved Name" in body.encode("utf-8")
    # Re-export does not re-name an already-named module.
    calls.clear()
    export_skills(Renderer(store), out_dir, namer_fn=namer)
    assert calls == []


# ## Conformance (plan-009 U8, R18)
#
# Each named R18 invariant maps to the behavioral test that enforces it:
#
# - "slugify/_skill_md on a NULL name produce the module-{id} fallback slug and a
#    valid SKILL.md (no exception, no empty slug, no literal None)"
#       -> test_export_null_safe_slug_fallback
# - "export triggers naming for the leaf community first (named, persisted, and the
#    rendered body carries the name) when a namer is supplied"
#       -> test_export_triggers_naming_for_leaf_community_first
