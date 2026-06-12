"""Skill rendering: byte-stable concatenation and judged delta-patch compile (R15/R16).

Concat is the always-available fallback: a deterministic template over
(skill, snapshot, status filter), members in append order, ``\\n`` newlines
pinned, returned as **bytes** so equality means byte equality on every platform.
Only the default active-only rendering is cached, keyed (skill_id, snapshot_id),
computed lazily — a new snapshot is a new key, so invalidation is definitional.
Quarantine-inclusive renders always compute fresh: quarantined membership changes
within a snapshot, so it is not a function of the cache key (R15).

Visibility (R13): render sees active members; ``include_quarantined`` adds
quarantined. Status is resolved as-of the requested snapshot via
:meth:`Store.status_at`, so rendering at an old ``--snapshot`` reproduces the
old bytes exactly.

Compile (explicit ``render --compile``) sends the active members through the
judge seam (U4's :func:`run_judge`) and writes a compiled document whose every
section carries an insight-ID provenance annotation
(``<!-- insights: 1, 2 -->``). The write is atomic (temp + ``os.replace``) and
happens only on success — a failed compile leaves any prior compiled doc
untouched. Compiled docs are stored under their (skill, snapshot) key.
Compilation never modifies insights (R16).
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from .judge import run_judge
from .store import Store

_ACTIVE_ONLY = frozenset({"active"})
_WITH_QUARANTINED = frozenset({"active", "quarantined"})

# Structured-output contract for the compile call: section-edits with mandatory
# insight-ID provenance. Non-empty sections / known-member IDs are enforced via
# extra_validate, riding the judge seam's feedback-retry path (R6 discipline).
COMPILE_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                    "insight_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["heading", "body", "insight_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sections"],
    "additionalProperties": False,
}

_PROVENANCE_RE = re.compile(r"<!-- insights: ([0-9][0-9, ]*) -->")


class RenderingError(Exception):
    """Raised on rendering/compile misuse with an actionable message."""


@dataclass(frozen=True)
class Rendering:
    """A concat rendering: pinned bytes plus the inputs that determined them."""

    content: bytes
    skill_id: int
    snapshot_id: int
    include_quarantined: bool
    empty: bool
    from_cache: bool


@dataclass(frozen=True)
class CompiledDoc:
    """A successfully compiled document, already persisted under its snapshot key."""

    path: Path
    content: bytes
    skill_id: int
    snapshot_id: int
    provenance: tuple[tuple[int, ...], ...]


def extract_provenance(content: bytes) -> list[list[int]]:
    """Parse per-section insight-ID annotations out of a compiled document."""
    text = content.decode("utf-8")
    return [
        [int(token) for token in match.group(1).split(",")]
        for match in _PROVENANCE_RE.finditer(text)
    ]


def display_name(skill: sqlite3.Row) -> str:
    """The module's name, or a stable ``module-{id}`` placeholder when unnamed (R17/R18).

    Under derived membership a module's ``skills.name`` is NULL until lazy naming
    fills it (plan-009 U4). Rendering and export must never emit the literal string
    ``None``; the placeholder is the same ``module-{id}`` fallback :mod:`export`
    slugifies from, so an unnamed module reads coherently everywhere.
    """
    name = skill["name"]
    return name if name is not None else f"module-{skill['id']}"


def _header_text(skill: sqlite3.Row) -> str:
    description = skill["description"] if skill["description"] is not None else ""
    return f"# Skill: {display_name(skill)}\n\n{description}\n\n---\n"


def _insight_block(row: sqlite3.Row) -> str:
    # Each member appends one self-contained block, so a grown skill's rendering
    # is the old bytes plus the appended region — nothing upstream moves.
    return (
        f"\n## Insight {row['id']}\n\n"
        f"- Precondition: {row['precondition']}\n"
        f"- Action: {row['action']}\n"
        f"- Expected outcome: {row['expected_outcome']}\n"
    )


def _compiled_text(skill: sqlite3.Row, sections: list[dict]) -> str:
    parts = [f"# Skill: {display_name(skill)} (compiled)\n"]
    for section in sections:
        ids = ", ".join(str(i) for i in section["insight_ids"])
        parts.append(
            f"\n## {section['heading']}\n"
            f"<!-- insights: {ids} -->\n\n"
            f"{section['body']}\n"
        )
    return "".join(parts)


def _validate_sections(output: dict, member_ids: frozenset[int]) -> str | None:
    sections = output["sections"]
    if not sections:
        return "sections is empty; the compiled document needs at least one section"
    for i, section in enumerate(sections):
        ids = section["insight_ids"]
        if not ids:
            return (
                f"sections[{i}] cites no insight_ids;"
                " every section must carry provenance"
            )
        unknown = [iid for iid in ids if iid not in member_ids]
        if unknown:
            return (
                f"sections[{i}] cites insight id(s) {unknown} that are not"
                " active members of this skill"
            )
    return None


class Renderer:
    """Renders skills from a :class:`Store`; owns the active-only render cache.

    ``compiled_dir`` is where compiled documents live, one file per
    (skill, snapshot) key; it is only required for the compile paths.
    """

    def __init__(self, store: Store, compiled_dir: str | Path | None = None) -> None:
        self.store = store
        self.compiled_dir = Path(compiled_dir) if compiled_dir is not None else None
        self._cache: dict[tuple[int, int], Rendering] = {}

    # --- shared lookups -------------------------------------------------------

    def _skill(self, skill_id: int) -> sqlite3.Row:
        row = self.store.conn.execute(
            "SELECT * FROM skills WHERE id = ?", (skill_id,)
        ).fetchone()
        if row is None:
            raise RenderingError(f"skill {skill_id} does not exist")
        return row

    def _resolve_snapshot(self, snapshot_id: int | None) -> int:
        return (
            self.store.current_snapshot_id() if snapshot_id is None else snapshot_id
        )

    def _visible_members(
        self, skill_id: int, snapshot_id: int, include_quarantined: bool
    ) -> list[sqlite3.Row]:
        # R17: order by a derive-stable key (insight_id ascending), NOT
        # skill_members.position. Append order is meaningless once the derive pass
        # rebalances membership wholesale, so rendering must not depend on it — a
        # rebalance that scrambles position but preserves membership renders
        # byte-identically.
        visible = _WITH_QUARANTINED if include_quarantined else _ACTIVE_ONLY
        members = []
        for insight_id in sorted(self.store.skill_members(skill_id)):
            if self.store.status_at(insight_id, snapshot_id) in visible:
                members.append(self.store.get_insight(insight_id))
        return members

    # --- concatenation (R15) ----------------------------------------------------

    def render_concat(
        self,
        skill_id: int,
        *,
        snapshot_id: int | None = None,
        include_quarantined: bool = False,
    ) -> Rendering:
        snap = self._resolve_snapshot(snapshot_id)
        key = (skill_id, snap)
        if not include_quarantined:
            cached = self._cache.get(key)
            if cached is not None:
                return replace(cached, from_cache=True)
        skill = self._skill(skill_id)
        members = self._visible_members(skill_id, snap, include_quarantined)
        text = _header_text(skill) + "".join(_insight_block(m) for m in members)
        rendering = Rendering(
            content=text.encode("utf-8"),
            skill_id=skill_id,
            snapshot_id=snap,
            include_quarantined=include_quarantined,
            empty=not members,
            from_cache=False,
        )
        if not include_quarantined:
            self._cache[key] = rendering
        return rendering

    # --- delta-patch compile (R16) ------------------------------------------------

    def compiled_path(self, skill_id: int, snapshot_id: int) -> Path:
        if self.compiled_dir is None:
            raise RenderingError(
                "Renderer has no compiled_dir; pass one to read or write"
                " compiled documents"
            )
        return self.compiled_dir / f"skill-{skill_id}-snapshot-{snapshot_id}.md"

    def get_compiled(self, skill_id: int, snapshot_id: int) -> bytes | None:
        path = self.compiled_path(skill_id, snapshot_id)
        return path.read_bytes() if path.exists() else None

    def compile_prompt(self, skill_id: int, *, snapshot_id: int | None = None) -> str:
        """The deterministic compile prompt — no timestamps, paths, or floats (R23)."""
        snap = self._resolve_snapshot(snapshot_id)
        skill = self._skill(skill_id)
        members = self._visible_members(skill_id, snap, include_quarantined=False)
        if not members:
            raise RenderingError(
                f"skill {skill_id} has no active insights at snapshot {snap};"
                " nothing to compile (concat of an empty skill is the fallback)"
            )
        lines = [
            "Compile this skill's insights into a cohesive reference document.",
            "Group related insights into sections. In each section's insight_ids,",
            "cite the ID of every insight that section draws from. Use only the",
            "insights provided - never invent content.",
            "",
            f"Skill: {skill['name']}",
            f"Description: {skill['description']}",
            "",
            "Insights:",
        ]
        for row in members:
            lines.append(
                f"[{row['id']}] Precondition: {row['precondition']}"
                f" | Action: {row['action']}"
                f" | Expected outcome: {row['expected_outcome']}"
            )
        return "\n".join(lines)

    def compile_skill(
        self,
        skill_id: int,
        *,
        model: str,
        max_retries: int,
        snapshot_id: int | None = None,
        bare: bool = False,
        mode: str | None = None,
        fixtures_dir: str | Path | None = None,
    ) -> CompiledDoc:
        """Compile a skill via the judge seam; persist atomically on success only.

        Any failure (judge unavailable, schema violation after retries, dangling
        provenance) raises before a single byte is written, so the prior compiled
        document for any key stays untouched. Insights are read, never modified.
        """
        snap = self._resolve_snapshot(snapshot_id)
        skill = self._skill(skill_id)
        members = self._visible_members(skill_id, snap, include_quarantined=False)
        if not members:
            raise RenderingError(
                f"skill {skill_id} has no active insights at snapshot {snap};"
                " nothing to compile (concat of an empty skill is the fallback)"
            )
        path = self.compiled_path(skill_id, snap)  # config errors precede judge spend
        member_ids = frozenset(row["id"] for row in members)
        prompt = self.compile_prompt(skill_id, snapshot_id=snap)
        result = run_judge(
            prompt,
            COMPILE_SCHEMA,
            model,
            max_retries=max_retries,
            bare=bare,
            extra_validate=lambda output: _validate_sections(output, member_ids),
            mode=mode,
            fixtures_dir=fixtures_dir,
        )
        sections = result.output["sections"]
        content = _compiled_text(skill, sections).encode("utf-8")
        self.compiled_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
        return CompiledDoc(
            path=path,
            content=content,
            skill_id=skill_id,
            snapshot_id=snap,
            provenance=tuple(tuple(s["insight_ids"]) for s in sections),
        )
