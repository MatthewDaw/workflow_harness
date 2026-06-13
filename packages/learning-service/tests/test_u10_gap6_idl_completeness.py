"""MAT-141 (U10) Gap 6 — IDL / data-model completeness for F2 to build against.

The IDL must define the IdeaSource record type and the full set of IdeaRecord
fields the Verified Learning loop needs:
    refines, revivedAt, legacyRecurrenceFold, sourceRef, scopeTag, authorId,
    verificationRung
…and the codegen output (py_types.py + ts_types.ts) must carry them.  Keeps
``test_schema_codegen_is_fresh_or_ci_fails`` green (the generated files are
regenerated from this IDL).
"""

from __future__ import annotations

from pathlib import Path

from learning_service.schema.generated.py_types import IdeaRecord, IdeaSourceRecord

GAP6_IDEA_FIELDS = [
    "refines",
    "revivedAt",
    "legacyRecurrenceFold",
    "sourceRef",
    "scopeTag",
    "authorId",
    "verificationRung",
]


def test_idea_source_record_type_exists():
    """The IdeaSource record type is defined and carries the inferred-lane fields."""
    src = IdeaSourceRecord()
    for f in ("prRef", "anchors", "authorityKind", "verificationRung", "authorId"):
        assert hasattr(src, f), f"IdeaSourceRecord missing field {f!r}"


def test_idea_record_has_all_gap6_fields():
    rec = IdeaRecord(
        ideaId="i-1",
        skillBaseName="s",
        org="acme",
        body="b",
        status="open",
        corroborationVersion=0,
    )
    for f in GAP6_IDEA_FIELDS:
        assert hasattr(rec, f), f"IdeaRecord missing Gap-6 field {f!r}"


def test_generated_files_carry_gap6_fields():
    base = Path(__file__).parent.parent / "src" / "learning_service" / "schema" / "generated"
    py = (base / "py_types.py").read_text(encoding="utf-8")
    ts = (base / "ts_types.ts").read_text(encoding="utf-8")
    for f in GAP6_IDEA_FIELDS:
        assert f in py, f"py_types.py missing {f!r} — regenerate codegen"
        assert f in ts, f"ts_types.ts missing {f!r} — regenerate codegen"
    # The IdeaSource record type is generated into both languages.
    assert "class IdeaSourceRecord" in py
    assert "interface IdeaSourceRecord" in ts


def test_shared_export_carries_gap6_idea_fields():
    """The @harness/shared re-export (consumed by TS) carries the Gap-6 fields too."""
    shared = (
        Path(__file__).parent.parent.parent
        / "shared"
        / "src"
        / "learning-schema.ts"
    )
    if not shared.exists():
        return  # learning-service-only checkout; the canonical ts_types covers it.
    src = shared.read_text(encoding="utf-8")
    for f in GAP6_IDEA_FIELDS:
        assert f in src, f"shared/learning-schema.ts missing {f!r} — regenerate codegen"
