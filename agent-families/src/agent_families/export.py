"""SKILL.md export: skills as valid Claude Code skill files (R17).

Each exported skill becomes ``<out_dir>/<slug>/SKILL.md`` — the Claude Code
skill layout — with frontmatter ``name`` (the slugified skill name) and
``description`` (the skill's load-bearing description), and a body that is the
skill's current rendering: the compiled document if one exists for the current
snapshot, else the concat rendering. Insights and skills are read, never
modified.

Slugification is deterministic: lowercase, runs of non-alphanumerics collapsed
to single hyphens, length-capped. Two skills slugifying identically within one
export set get stable numeric suffixes — skills are processed in ascending
skill-ID order, so the same library exports the same slugs every time, which
also makes re-export over an existing directory byte-idempotent.

Empty skills (no active members at the current snapshot) are skipped, recorded
in the report with a notice; no file or directory is written for them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .rendering import Renderer

# Claude Code skill-name format constraint (lowercase/digits/hyphens, max 64
# chars per the skill spec) — a format constant, not a tunable threshold.
SLUG_MAX_LENGTH = 64

_NON_SLUG_RUN = re.compile(r"[^a-z0-9]+")


def slugify(name: str, *, max_length: int = SLUG_MAX_LENGTH) -> str:
    """Deterministic slug: lowercase, hyphen-collapsed, length-capped."""
    slug = _NON_SLUG_RUN.sub("-", name.lower()).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "skill"


def _allocate_slug(base: str, used: set[str], max_length: int) -> str:
    """First taker keeps the base; collisions get -2, -3, ... within the cap."""
    if base not in used:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = base[: max_length - len(suffix)].rstrip("-") + suffix
        if candidate not in used:
            return candidate
        n += 1


@dataclass(frozen=True)
class ExportedSkill:
    skill_id: int
    name: str
    slug: str
    path: Path
    compiled: bool


@dataclass(frozen=True)
class SkippedSkill:
    skill_id: int
    name: str
    notice: str


@dataclass(frozen=True)
class ExportReport:
    exported: tuple[ExportedSkill, ...]
    skipped: tuple[SkippedSkill, ...]

    @property
    def notices(self) -> tuple[str, ...]:
        return tuple(s.notice for s in self.skipped)


def _skill_md(slug: str, description: str, body: bytes) -> bytes:
    # json.dumps yields a double-quoted scalar that is valid YAML for any
    # description content (quotes, colons, newlines), keeping the file parseable.
    frontmatter = (
        "---\n"
        f"name: {slug}\n"
        f"description: {json.dumps(description)}\n"
        "---\n"
        "\n"
    )
    return frontmatter.encode("utf-8") + body


def export_skills(
    renderer: Renderer,
    out_dir: str | Path,
    skill_ids: list[int] | None = None,
) -> ExportReport:
    """Export skills as Claude Code SKILL.md files under ``out_dir``.

    ``skill_ids`` defaults to every skill in the library. Whatever subset and
    order the caller supplies, skills are processed in ascending ID order so
    collision suffixes are stable across runs.
    """
    store = renderer.store
    out = Path(out_dir)
    if skill_ids is None:
        rows = store.conn.execute("SELECT id FROM skills ORDER BY id ASC").fetchall()
        skill_ids = [row["id"] for row in rows]
    snapshot_id = store.current_snapshot_id()

    exported: list[ExportedSkill] = []
    skipped: list[SkippedSkill] = []
    used_slugs: set[str] = set()
    for skill_id in sorted(set(skill_ids)):
        skill = store.conn.execute(
            "SELECT * FROM skills WHERE id = ?", (skill_id,)
        ).fetchone()
        if skill is None:
            raise ValueError(f"skill {skill_id} does not exist")
        rendering = renderer.render_concat(skill_id, snapshot_id=snapshot_id)
        if rendering.empty:
            skipped.append(
                SkippedSkill(
                    skill_id=skill_id,
                    name=skill["name"],
                    notice=(
                        f"skill {skill_id} ({skill['name']!r}) has no active"
                        " insights; skipped"
                    ),
                )
            )
            continue
        compiled = (
            renderer.get_compiled(skill_id, snapshot_id)
            if renderer.compiled_dir is not None
            else None
        )
        body = compiled if compiled is not None else rendering.content
        slug = _allocate_slug(slugify(skill["name"]), used_slugs, SLUG_MAX_LENGTH)
        used_slugs.add(slug)
        path = out / slug / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_skill_md(slug, skill["description"], body))
        exported.append(
            ExportedSkill(
                skill_id=skill_id,
                name=skill["name"],
                slug=slug,
                path=path,
                compiled=compiled is not None,
            )
        )
    return ExportReport(exported=tuple(exported), skipped=tuple(skipped))
