"""anchors.py — U2: Code-anchor model + locality join (MAT-138).

Responsibilities
----------------
1. **Symbol extraction** — parse a diff hunk (or a full file) with
   tree-sitter (TypeScript / Go / Python) and return the *smallest enclosing
   named symbol* that contains each changed line.  Falls back to the file-level
   sentinel ``__file__`` when the grammar is absent, the parse fails, or no
   named node encloses the hunk.

2. **Anchor write on fold** — given a set of ``(file, symbol)`` pairs produced
   by ``extract_anchors_from_diff``, write one ``AnchorRecord`` per pair into
   the store (org-scoped, mirrors the ``corroborate.ts`` ORG GUARD).

3. **Locality join on merge** — given the touched ``(file, symbol)`` set for a
   new PR's diff, query the anchor index and return the union of incumbent
   idea IDs (deduplicated, the fan-out set).

4. **Anchor retirement on un-fold** — when an idea is retired (superseded /
   un-folded) mark its anchor records ``active=False`` so they stop appearing
   in future locality joins (kept in history for audit).

5. **Telemetry** — every extraction logs ``anchor_resolution=symbol`` or
   ``anchor_resolution=file_fallback`` with the file extension so the
   symbol-vs-file-level resolution rate can be tracked from day one.

Design notes
------------
- **Tree-sitter, not hunk headers.** ``@@`` context lines are empty or wrong
  for TypeScript arrow consts, JSX, and Go closures.  Tree-sitter parses the
  *base-ref file* to find the enclosing named symbol; the hunk only supplies
  the line numbers.
- **v1 languages:** TypeScript, Go, Python.  Unknown extensions fall back to
  file-level (logged as ``file_fallback``).
- **File-level sentinel:** the string ``__file__`` (never a real symbol name
  for any of the three supported grammars).
- **Org guard:** mirrors the non-blank-org guard in ``corroborate.ts``; every
  write raises ``OrgGuardError`` if org is blank.
- **Fan-out dedup:** ``get_incumbent_ideas`` unions idea IDs across all touched
  ``(file, symbol)`` pairs and removes duplicates while preserving first-seen
  order.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from learning_service.db.store import LearningStore

from learning_service.db.store import OrgGuardError
from learning_service.schema.generated.py_types import AnchorRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FILE_LEVEL_SYMBOL = "__file__"

# Node types that count as "named symbols" per language — smallest enclosing
# wins (we walk from deepest child back toward root and take the first match).
_TS_NAMED_TYPES: frozenset[str] = frozenset(
    {
        # Regular function / method / class
        "function_declaration",
        "method_definition",
        "class_declaration",
        # Arrow function assigned to a const: the *variable_declarator* holds
        # the name (identifier child) + the arrow_function body.
        "variable_declarator",
        # TypeScript-specific
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        # JSX component (an arrow const wrapped in lexical_declaration)
        "export_statement",
    }
)

_GO_NAMED_TYPES: frozenset[str] = frozenset(
    {
        "function_declaration",
        "method_declaration",
        # short_var_declaration whose RHS is a func_literal:
        #   handler := func(...) { ... }
        # The enclosing *short_var_declaration* carries the name.
        "short_var_declaration",
        "type_declaration",
        "var_declaration",
    }
)

_PY_NAMED_TYPES: frozenset[str] = frozenset(
    {
        "function_definition",
        "class_definition",
        # decorated versions
        "decorated_definition",
    }
)

_EXT_TO_LANG: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",   # close enough for symbol extraction
    ".jsx": "typescript",
    ".go": "go",
    ".py": "python",
}


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class Anchor:
    """A single (file, symbol) pair extracted from a diff hunk."""

    file: str
    symbol: str
    resolution: str  # "symbol" | "file_fallback"


@dataclass
class DiffHunk:
    """Minimal representation of a unified-diff hunk for anchor extraction.

    ``start_line`` is 1-based (the ``@@`` base-ref start line).
    ``end_line`` is inclusive.
    ``file`` is the repo-relative path (the ``b/`` side after normalisation).
    ``content`` is the raw file content bytes *at the base ref* (pre-merge).
    """

    file: str
    start_line: int
    end_line: int
    content: bytes  # base-ref file bytes (may be empty for new files)


# ---------------------------------------------------------------------------
# Tree-sitter helpers (lazy-loaded so missing grammars don't crash the import)
# ---------------------------------------------------------------------------


def _get_parser(lang: str):  # type: ignore[return]
    """Return a tree-sitter Parser for *lang*, or None if unavailable."""
    try:
        from tree_sitter import Language, Parser

        if lang == "typescript":
            import tree_sitter_typescript as tsts
            language = Language(tsts.language_typescript())
        elif lang == "go":
            import tree_sitter_go as tsgo
            language = Language(tsgo.language())
        elif lang == "python":
            import tree_sitter_python as tspy
            language = Language(tspy.language())
        else:
            return None

        return Parser(language)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tree-sitter parser unavailable for %r: %s", lang, exc)
        return None


def _ts_declarator_is_function(node) -> bool:
    """Return True if this variable_declarator's RHS is an arrow/function.

    This guards against matching plain ``const x = someValue`` inside a body —
    we only want to match ``const myFn = () => { ... }`` or
    ``const myFn = function() { ... }``.
    """
    for child in node.children:
        if child.type in ("arrow_function", "function", "function_expression", "generator_function"):
            return True
        # async arrow: async (req) => ...
        if child.type == "call_expression":
            # Not a function literal
            pass
    return False


def _node_name(node, lang: str) -> str | None:
    """Extract the display name of a named node, or None if it has no name."""
    if lang == "typescript":
        # variable_declarator: first child is the identifier.
        # ONLY match if the RHS is a function/arrow (not plain const x = value).
        if node.type == "variable_declarator":
            if not _ts_declarator_is_function(node):
                return None
            for child in node.children:
                if child.type == "identifier":
                    return child.text.decode("utf-8", errors="replace") if child.text else None
        # Most other named nodes have an `identifier` or `name` child
        for child in node.children:
            if child.type in ("identifier", "type_identifier", "property_identifier"):
                return child.text.decode("utf-8", errors="replace") if child.text else None

    elif lang == "go":
        if node.type == "short_var_declaration":
            # LHS expression_list → first identifier child
            for child in node.children:
                if child.type == "expression_list":
                    for sub in child.children:
                        if sub.type == "identifier":
                            return sub.text.decode("utf-8", errors="replace") if sub.text else None
        # function_declaration / method_declaration: second child is the name
        for child in node.children:
            if child.type in ("identifier", "field_identifier", "type_identifier"):
                return child.text.decode("utf-8", errors="replace") if child.text else None

    elif lang == "python":
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8", errors="replace") if child.text else None

    return None


def _named_types_for_lang(lang: str) -> frozenset[str]:
    if lang == "typescript":
        return _TS_NAMED_TYPES
    if lang == "go":
        return _GO_NAMED_TYPES
    if lang == "python":
        return _PY_NAMED_TYPES
    return frozenset()


def _find_enclosing_symbol(root_node, line: int, lang: str) -> str | None:
    """Walk the AST and find the smallest named node enclosing *line* (1-based).

    Returns the symbol's display name, or None if no named node covers the line.
    """
    named_types = _named_types_for_lang(lang)

    def _search(node):
        """DFS; returns (name, size) of best match found under node."""
        # node.start_point and end_point are (row, col), 0-based row.
        node_start = node.start_point[0] + 1  # to 1-based
        node_end = node.end_point[0] + 1

        if not (node_start <= line <= node_end):
            return None  # prune: line not in this subtree

        # Check children first (prefer smaller enclosing node).
        best = None
        for child in node.children:
            result = _search(child)
            if result is not None:
                # Choose the smallest node (fewest lines)
                if best is None or result[1] < best[1]:
                    best = result

        if best is not None:
            return best

        # No child matched — check if *this* node is a named type.
        if node.type in named_types:
            name = _node_name(node, lang)
            if name:
                size = node_end - node_start + 1
                return (name, size)

        return None

    result = _search(root_node)
    return result[0] if result else None


# ---------------------------------------------------------------------------
# Core API: extract anchors from diff hunks
# ---------------------------------------------------------------------------


def extract_anchors_from_diff(hunks: list[DiffHunk]) -> list[Anchor]:
    """Parse tree-sitter ASTs and extract ``(file, symbol)`` anchors.

    One ``Anchor`` is returned per **distinct** ``(file, symbol)`` pair seen
    across all hunks.  If multiple hunks touch the same ``(file, symbol)``
    they are deduplicated.

    For each hunk:
    - The file extension determines the language (TypeScript / Go / Python).
    - The base-ref file content is parsed; each line in ``[start_line, end_line]``
      is mapped to the smallest enclosing named symbol.
    - If the grammar is unavailable, the parse fails, or no named node covers
      *all* sample lines, the hunk falls back to ``__file__`` (file-level).

    Telemetry: each anchor's ``resolution`` field is ``"symbol"`` or
    ``"file_fallback"``; the caller may aggregate these for the
    ``anchor_resolution`` metric.
    """
    seen: dict[tuple[str, str], Anchor] = {}

    for hunk in hunks:
        ext = _file_ext(hunk.file)
        lang = _EXT_TO_LANG.get(ext)

        if lang is None or not hunk.content:
            _add_anchor(seen, hunk.file, FILE_LEVEL_SYMBOL, "file_fallback")
            logger.debug(
                "anchor_resolution=file_fallback ext=%r file=%r (no grammar or empty content)",
                ext,
                hunk.file,
            )
            continue

        parser = _get_parser(lang)
        if parser is None:
            _add_anchor(seen, hunk.file, FILE_LEVEL_SYMBOL, "file_fallback")
            logger.debug(
                "anchor_resolution=file_fallback ext=%r file=%r (parser unavailable)",
                ext,
                hunk.file,
            )
            continue

        try:
            tree = parser.parse(hunk.content)
        except Exception as exc:  # noqa: BLE001
            _add_anchor(seen, hunk.file, FILE_LEVEL_SYMBOL, "file_fallback")
            logger.warning(
                "anchor_resolution=file_fallback ext=%r file=%r (parse error: %s)",
                ext,
                hunk.file,
                exc,
            )
            continue

        # Try each line in the hunk; collect all distinct symbols found.
        # Strategy: use the midpoint of the hunk as the representative line
        # (avoids boundary noise from blank lines at the top/bottom of hunks).
        mid_line = (hunk.start_line + hunk.end_line) // 2
        candidate = _find_enclosing_symbol(tree.root_node, mid_line, lang)

        if candidate is None:
            # Try start_line and end_line as fallbacks before giving up.
            for probe in (hunk.start_line, hunk.end_line):
                candidate = _find_enclosing_symbol(tree.root_node, probe, lang)
                if candidate is not None:
                    break

        if candidate:
            _add_anchor(seen, hunk.file, candidate, "symbol")
            logger.debug(
                "anchor_resolution=symbol ext=%r file=%r symbol=%r",
                ext,
                hunk.file,
                candidate,
            )
        else:
            _add_anchor(seen, hunk.file, FILE_LEVEL_SYMBOL, "file_fallback")
            logger.debug(
                "anchor_resolution=file_fallback ext=%r file=%r (no enclosing symbol)",
                ext,
                hunk.file,
            )

    return list(seen.values())


def parse_unified_diff(diff_text: str, file_contents: dict[str, bytes]) -> list[DiffHunk]:
    """Parse a unified diff string into DiffHunk objects.

    ``file_contents`` maps repo-relative paths to base-ref file bytes.
    Files not present in the dict get empty content (triggers file-level fallback).

    Handles the ``--- a/`` / ``+++ b/`` header and ``@@`` hunk headers.
    """
    hunks: list[DiffHunk] = []
    current_file: str | None = None
    hunk_start: int | None = None
    hunk_end: int | None = None

    b_file_re = re.compile(r"^\+\+\+ b/(.+)$")
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        m_file = b_file_re.match(line)
        if m_file:
            # Flush previous hunk if any
            if current_file is not None and hunk_start is not None:
                content = file_contents.get(current_file, b"")
                hunks.append(DiffHunk(
                    file=current_file,
                    start_line=hunk_start,
                    end_line=hunk_end or hunk_start,
                    content=content,
                ))
                hunk_start = None
                hunk_end = None
            current_file = m_file.group(1)
            continue

        m_hunk = hunk_re.match(line)
        if m_hunk and current_file is not None:
            # Flush previous hunk
            if hunk_start is not None:
                content = file_contents.get(current_file, b"")
                hunks.append(DiffHunk(
                    file=current_file,
                    start_line=hunk_start,
                    end_line=hunk_end or hunk_start,
                    content=content,
                ))
            # Base-ref (old) start line from the "-N" side
            hunk_start = int(m_hunk.group(1))
            # Hunk line count from old side (group 3 is new count — we want old)
            # Actually we want to know the lines in the base file, use group 1+size
            # group(2) is the new start, group(3) is the new count
            # For finding enclosing symbol we care about the OLD side lines.
            hunk_end = hunk_start  # will be overridden by last + line
            continue

    # Flush final hunk
    if current_file is not None and hunk_start is not None:
        content = file_contents.get(current_file, b"")
        hunks.append(DiffHunk(
            file=current_file,
            start_line=hunk_start,
            end_line=hunk_end or hunk_start,
            content=content,
        ))

    return hunks


# ---------------------------------------------------------------------------
# Store operations: write anchors on fold, retire on un-fold, locality join
# ---------------------------------------------------------------------------


def write_anchors_on_fold(
    store: "LearningStore",
    org: str,
    owner_repo: str,
    idea_id: str,
    anchors: list[Anchor],
) -> None:
    """Write one AnchorRecord per anchor to the store (on idea fold).

    Org guard: raises ``OrgGuardError`` if org is blank (mirrors the
    ``corroborate.ts`` ORG GUARD).

    Each record is written with ``active=True``.  If the same (file, symbol)
    appears multiple times (shouldn't, but be safe), duplicates are skipped.
    """
    if not org or not org.strip():
        raise OrgGuardError("write_anchors_on_fold")

    seen_pairs: set[tuple[str, str]] = set()
    for anchor in anchors:
        pair = (anchor.file, anchor.symbol)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        record = AnchorRecord(
            ideaId=idea_id,
            ownerRepo=owner_repo,
            file=anchor.file,
            symbol=anchor.symbol,
            org=org,
            active=True,
        )
        store.put_anchor(record)
        logger.info(
            "anchor_written org=%r repo=%r idea=%r file=%r symbol=%r",
            org,
            owner_repo,
            idea_id,
            anchor.file,
            anchor.symbol,
        )


def retire_anchors_on_unfold(
    store: "LearningStore",
    org: str,
    owner_repo: str,
    idea_id: str,
) -> None:
    """Mark all anchors for *idea_id* as inactive when an idea is un-folded.

    The records are kept in the store (history) but will no longer appear in
    ``get_incumbent_ideas`` results (which filter ``active=True`` only).
    """
    if not org or not org.strip():
        raise OrgGuardError("retire_anchors_on_unfold")

    existing = store.get_anchors_for_idea(org, owner_repo, idea_id)
    for anchor in existing:
        retired = AnchorRecord(
            ideaId=anchor.ideaId,
            ownerRepo=anchor.ownerRepo,
            file=anchor.file,
            symbol=anchor.symbol,
            org=anchor.org,
            active=False,
        )
        store.put_anchor(retired)
        logger.info(
            "anchor_retired org=%r repo=%r idea=%r file=%r symbol=%r",
            org,
            owner_repo,
            idea_id,
            anchor.file,
            anchor.symbol,
        )


def get_incumbent_ideas(
    store: "LearningStore",
    org: str,
    owner_repo: str,
    anchors: list[Anchor],
) -> list[str]:
    """Return all active idea IDs anchored to any of the given (file, symbol) pairs.

    Fan-out: queries each pair individually, unions the results, deduplicates
    (preserving first-seen order) and returns the list.  An empty list means
    no incumbents — no locality collision.
    """
    seen: dict[str, None] = {}  # ordered set
    for anchor in anchors:
        idea_ids = store.get_ideas_by_anchor(org, owner_repo, anchor.file, anchor.symbol)
        for iid in idea_ids:
            seen.setdefault(iid, None)
    return list(seen.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_ext(path: str) -> str:
    """Return the lowercase extension including the dot, e.g. '.ts'."""
    idx = path.rfind(".")
    if idx < 0:
        return ""
    return path[idx:].lower()


def _add_anchor(
    seen: dict[tuple[str, str], Anchor],
    file: str,
    symbol: str,
    resolution: str,
) -> None:
    """Add to the seen dict if not already present (dedup)."""
    key = (file, symbol)
    if key not in seen:
        seen[key] = Anchor(file=file, symbol=symbol, resolution=resolution)
