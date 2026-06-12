"""GENERATED — do not edit by hand.

This file is produced by ``learning_service.schema.codegen``.
IDL fingerprint: ff4765c52834e28b  (schema version 1.0.0)

Regenerate with:
    python -m learning_service.schema.codegen

CI freshness check:
    python -m learning_service.schema.codegen --check
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Pad widths (mirrored from the IDL)
# ---------------------------------------------------------------------------

PAD_TS = 15
PAD_SEQ = 12
PAD_REV = 12

# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------

def skill_key(org: str | int, baseName: str | int) -> dict[str, str]:
    """Live skill record (the current canonical body for a baseName)."""
    return {"PK": f"SCOPE#org#{org}", "SK": f"SKILL#{baseName}"}

def revision_key(org: str | int, variantId: str | int, rev: str | int) -> dict[str, str]:
    """Immutable revision snapshot. rev is zero-padded to PAD_WIDTHS['rev']."""
    return {"PK": f"SCOPE#org#{org}", "SK": f"SKILL#{variantId}#r{str(rev).zfill(12)}"}

def true_pointer_key(org: str | int, baseName: str | int) -> dict[str, str]:
    """Per-baseName org-wide TRUE pointer (which variant+rev is the default)."""
    return {"PK": f"SCOPE#org#{org}", "SK": f"SKILL#{baseName}#TRUE"}

def idea_key(org: str | int, skillBaseName: str | int, ideaId: str | int) -> dict[str, str]:
    """A candidate/folded insight co-located with its skill family."""
    return {"PK": f"SCOPE#org#{org}", "SK": f"IDEA#{skillBaseName}#{ideaId}"}

def idea_source_key(org: str | int, ideaId: str | int, sourceId: str | int) -> dict[str, str]:
    """One contribution (a merged PR or authored source) under its idea."""
    return {"PK": f"SCOPE#org#{org}", "SK": f"IDEASRC#{ideaId}#{sourceId}"}

def golden_case_key(org: str | int, skillBaseName: str | int, caseId: str | int) -> dict[str, str]:
    """Before→after golden regression case captured at fold time."""
    return {"PK": f"SCOPE#org#{org}", "SK": f"IDEAGOLD#{skillBaseName}#{caseId}"}

def anchor_key(org: str | int, ownerRepo: str | int, file: str | int, symbol: str | int, ideaId: str | int) -> dict[str, str]:
    """Code-anchor index entry (file, symbol) → ideaId for the locality join."""
    return {"PK": f"SCOPE#org#{org}", "SK": f"ANCHOR#{ownerRepo}#{file}#{symbol}#{ideaId}"}

def processed_pr_key(org: str | int, ownerRepo: str | int, prNumber: str | int) -> dict[str, str]:
    """Org-scoped idempotency cursor — marks a PR as already ingested."""
    return {"PK": f"SCOPE#org#{org}", "SK": f"PROCESSED#{ownerRepo}#{prNumber}"}

def verify_event_key(org: str | int, ideaId: str | int, seq: str | int) -> dict[str, str]:
    """Append-only audit row under an idea (supersede/corroborate events)."""
    return {"PK": f"SCOPE#org#{org}", "SK": f"VERIFY#{ideaId}#{str(seq).zfill(12)}"}

# ---------------------------------------------------------------------------
# Record dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SkillRecord:
    """The canonical live body of a skill (the current revision's content)."""
    baseName: str  # The skill's base name (org-unique).
    variantId: str  # Active variant id (empty = org base).
    rev: int  # Active revision number.
    body: str  # Markdown/YAML skill body.
    org: str  # Owning org slug.
    description: str | None = None  # Human-readable description.
    createdBy: str | None = None  # User who created this skill.
    updatedAt: int | None = None  # Epoch-ms timestamp of last revision write.

@dataclass
class RevisionRecord:
    """Immutable snapshot of a skill variant at a specific revision."""
    baseName: str  # Base name of the skill family.
    variantId: str  # Variant id (empty = org base).
    rev: int  # Revision number (monotonically increasing per variant).
    body: str  # Skill body at this revision.
    org: str  # Owning org slug.
    description: str | None = None  # Description at this revision.
    createdBy: str | None = None  # Author of this revision.
    createdAt: int | None = None  # Epoch-ms of when this revision was written.
    ideaId: str | None = None  # The idea that triggered this revision (fold path).

@dataclass
class TruePointerRecord:
    """Per-baseName org-wide TRUE pointer — which variant+rev is the active default."""
    baseName: str  # Base name.
    variantId: str  # Currently-active variant id.
    rev: int  # Currently-active revision number.
    org: str  # Owning org slug.
    updatedAt: int | None = None  # Epoch-ms of last pointer update.

@dataclass
class IdeaSourceRecord:
    """One contribution to an idea (a PR or authored source that corroborates it)."""
    ideaId: str | None = None  # The idea this source contributes to (the IDEASRC# partition key bridge).
    sourceId: str | None = None  # Stable id for this contribution (e.g. prRef or an authored content hash).
    org: str | None = None  # Owning org slug.
    sessionId: str | None = None  # Session that produced this contribution (enrichment only).
    segmentId: str | None = None  # Segment within the session.
    seq: int | None = None  # Per-session monotonic seq for ordering/observability.
    prRef: str | None = None  # Merged PR reference: owner/repo#<number> (inferred lane).
    anchors: str | None = None  # JSON-serialised list of {file, symbol} anchor pairs.
    authorityKind: str | None = None  # user_directive | authored_import | merged.
    verificationRung: str | None = None  # test | normal | bare — inferred from diff/PR metadata.
    authorId: str | None = None  # PR/commit author id for the author-credibility multiplier.

@dataclass
class IdeaRecord:
    """A candidate or folded insight co-located with its skill family."""
    ideaId: str  # Unique idea identifier (UUID or slug).
    skillBaseName: str  # The skill family this idea belongs to.
    org: str  # Owning org slug.
    body: str  # The insight body (synthesised paragraph).
    status: str  # open | folded.
    corroborationVersion: int  # Optimistic-concurrency token.
    foldedIntoRev: int | None = None  # Revision this idea was folded into.
    invalidAt: int | None = None  # Epoch-ms of temporal retirement (supersession).
    supersededBy: str | None = None  # IdeaId that superseded this one.
    supersedes: list[str] = field(default_factory=list)  # IdeaIds this idea supersedes.
    authored: bool | None = None  # True if this is an authored (non-inferred) idea.
    authorityKind: str | None = None  # user_directive | authored_import | merged.
    refines: str | None = None  # IdeaId this idea refines (scoped-nuance coexistence edge).
    revivedAt: int | None = None  # Epoch-ms when a superseded idea was revived.
    legacyRecurrenceFold: bool | None = None  # True if this idea was folded under the old recurrence model (grandfather flag).
    sourceRef: str | None = None  # JSON-serialised source reference for authored ideas: {kind, label?, contentHash?}.
    scopeTag: str | None = None  # Scope tag for authored ideas: project | user | org | repo | skill.
    authorId: str | None = None  # Author id for the author-credibility weighting.
    verificationRung: str | None = None  # test | normal | bare — inferred from diff/PR metadata.

@dataclass
class GoldenCaseRecord:
    """Before→after golden regression case captured at idea-fold time."""
    caseId: str  # Typically the ideaId of the folded idea.
    skillBaseName: str  # The skill family guarded by this case.
    org: str  # Owning org slug.
    before: str  # The pre-fold skill body excerpt (the prompt context).
    after: str  # The expected post-fold skill body (ground truth).
    ideaBody: str  # The folded idea body (for drift detection).
    createdAt: int | None = None  # Epoch-ms when this case was captured.

@dataclass
class AnchorRecord:
    """Code-anchor index entry linking (file, symbol) to an idea for the locality join."""
    ideaId: str  # The idea this anchor points to.
    ownerRepo: str  # owner/repo (e.g. acme/backend).
    file: str  # File path within the repo.
    symbol: str  # Enclosing named symbol (or '__file__' for file-level fallback).
    org: str  # Owning org slug.
    active: bool  # False when the anchor has been retired (idea un-folded/superseded).
