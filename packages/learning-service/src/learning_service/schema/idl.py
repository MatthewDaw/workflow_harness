"""Schema IDL — the single source of truth for the harness single-table key/record shapes.

This module defines the key-format rules and record field descriptors in pure
Python data structures.  The codegen script (``codegen.py``) reads this module
and emits both the TypeScript types (``generated/ts_types.ts``) and the Python
dataclasses/builders (``generated/py_types.py``).

Rules:
  - Every key prefix, pad width, and SK template is defined ONCE here.
  - Record field types are described with a minimal descriptor vocabulary so
    that the codegen can produce both language outputs.
  - Descriptions must be kept up to date; they are injected verbatim into the
    generated docstrings / JSDoc comments.

DO NOT import this module from production hot-paths — import the generated
``py_types.py`` instead.  This file is a build-time artifact only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Primitive descriptor vocabulary
# ---------------------------------------------------------------------------

FieldKind = Literal["str", "int", "float", "bool", "str[]", "str?", "int?", "float?", "bool?"]


@dataclass
class FieldDef:
    """One record field."""
    name: str
    kind: FieldKind
    description: str = ""
    optional: bool = False  # convenience — also encoded in kind via "?"


@dataclass
class RecordDef:
    """A logical DynamoDB record family (e.g. SKILL, IDEA, IDEAGOLD)."""
    name: str               # Python class name / TS interface name
    description: str
    fields: list[FieldDef]


@dataclass
class KeyDef:
    """A PK/SK builder definition."""
    name: str               # Python function name / TS function name
    pk_template: str        # e.g. "SCOPE#org#{org}"
    sk_template: str        # e.g. "SKILL#{baseName}#TRUE"
    params: list[str]       # ordered parameter names
    description: str = ""


@dataclass
class SchemaDef:
    """Top-level schema bundle passed to the codegen."""
    version: str
    pad_widths: dict[str, int]           # e.g. {"ts": 15, "seq": 12, "rev": 12}
    key_defs: list[KeyDef]
    record_defs: list[RecordDef]


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

PAD_WIDTHS: dict[str, int] = {
    "ts": 15,    # epoch-ms timestamps, comfortably future-proof
    "seq": 12,   # event sequence numbers
    "rev": 12,   # revision numbers
}

# ---------------------------------------------------------------------------
# Key definitions (the single-table design)
# ---------------------------------------------------------------------------

KEY_DEFS: list[KeyDef] = [
    # --- Skills (versioning) ------------------------------------------------
    KeyDef(
        name="skill_key",
        pk_template="SCOPE#org#{org}",
        sk_template="SKILL#{baseName}",
        params=["org", "baseName"],
        description="Live skill record (the current canonical body for a baseName).",
    ),
    KeyDef(
        name="revision_key",
        pk_template="SCOPE#org#{org}",
        sk_template="SKILL#{variantId}#r{rev:rev}",
        params=["org", "variantId", "rev"],
        description="Immutable revision snapshot. rev is zero-padded to PAD_WIDTHS['rev'].",
    ),
    KeyDef(
        name="true_pointer_key",
        pk_template="SCOPE#org#{org}",
        sk_template="SKILL#{baseName}#TRUE",
        params=["org", "baseName"],
        description="Per-baseName org-wide TRUE pointer (which variant+rev is the default).",
    ),
    # --- Ideas --------------------------------------------------------------
    KeyDef(
        name="idea_key",
        pk_template="SCOPE#org#{org}",
        sk_template="IDEA#{skillBaseName}#{ideaId}",
        params=["org", "skillBaseName", "ideaId"],
        description="A candidate/folded insight co-located with its skill family.",
    ),
    # --- Idea sources (the per-PR / authored contributions to an idea) -------
    KeyDef(
        name="idea_source_key",
        pk_template="SCOPE#org#{org}",
        sk_template="IDEASRC#{ideaId}#{sourceId}",
        params=["org", "ideaId", "sourceId"],
        description="One contribution (a merged PR or authored source) under its idea.",
    ),
    # --- Golden cases (IDEAGOLD) --------------------------------------------
    KeyDef(
        name="golden_case_key",
        pk_template="SCOPE#org#{org}",
        sk_template="IDEAGOLD#{skillBaseName}#{caseId}",
        params=["org", "skillBaseName", "caseId"],
        description="Before→after golden regression case captured at fold time.",
    ),
    # --- Anchor index (U2 locality join) ------------------------------------
    KeyDef(
        name="anchor_key",
        pk_template="SCOPE#org#{org}",
        sk_template="ANCHOR#{ownerRepo}#{file}#{symbol}#{ideaId}",
        params=["org", "ownerRepo", "file", "symbol", "ideaId"],
        description="Code-anchor index entry (file, symbol) → ideaId for the locality join.",
    ),
    # --- Processed-PR cursor (idempotency) ----------------------------------
    KeyDef(
        name="processed_pr_key",
        pk_template="SCOPE#org#{org}",
        sk_template="PROCESSED#{ownerRepo}#{prNumber}",
        params=["org", "ownerRepo", "prNumber"],
        description="Org-scoped idempotency cursor — marks a PR as already ingested.",
    ),
    # --- Verification/supersession audit ------------------------------------
    KeyDef(
        name="verify_event_key",
        pk_template="SCOPE#org#{org}",
        sk_template="VERIFY#{ideaId}#{seq:seq}",
        params=["org", "ideaId", "seq"],
        description="Append-only audit row under an idea (supersede/corroborate events).",
    ),
    # --- Branch→session link (U7 session-link capture) ----------------------
    KeyDef(
        name="branch_session_key",
        pk_template="SCOPE#org#{org}",
        sk_template="BSLINK#{ownerRepo}#{branch}",
        params=["org", "ownerRepo", "branch"],
        description=(
            "Branch→session link written at git-push time (U7). Maps a (repo, branch) "
            "pair to the sessionId(s) + turn-range + eagerly-distilled context that "
            "produced the push.  TTL 90 days.  U1 reads distilledContext at merge time "
            "without re-reading EVT# records."
        ),
    ),
]

# ---------------------------------------------------------------------------
# Record definitions
# ---------------------------------------------------------------------------

RECORD_DEFS: list[RecordDef] = [
    RecordDef(
        name="SkillRecord",
        description="The canonical live body of a skill (the current revision's content).",
        fields=[
            FieldDef("baseName", "str", "The skill's base name (org-unique)."),
            FieldDef("variantId", "str", "Active variant id (empty = org base)."),
            FieldDef("rev", "int", "Active revision number."),
            FieldDef("body", "str", "Markdown/YAML skill body."),
            FieldDef("org", "str", "Owning org slug."),
            FieldDef("description", "str?", "Human-readable description.", optional=True),
            FieldDef("createdBy", "str?", "User who created this skill.", optional=True),
            FieldDef("updatedAt", "int?", "Epoch-ms timestamp of last revision write.", optional=True),
        ],
    ),
    RecordDef(
        name="RevisionRecord",
        description="Immutable snapshot of a skill variant at a specific revision.",
        fields=[
            FieldDef("baseName", "str", "Base name of the skill family."),
            FieldDef("variantId", "str", "Variant id (empty = org base)."),
            FieldDef("rev", "int", "Revision number (monotonically increasing per variant)."),
            FieldDef("body", "str", "Skill body at this revision."),
            FieldDef("org", "str", "Owning org slug."),
            FieldDef("description", "str?", "Description at this revision.", optional=True),
            FieldDef("createdBy", "str?", "Author of this revision.", optional=True),
            FieldDef("createdAt", "int?", "Epoch-ms of when this revision was written.", optional=True),
            FieldDef("ideaId", "str?", "The idea that triggered this revision (fold path).", optional=True),
        ],
    ),
    RecordDef(
        name="TruePointerRecord",
        description="Per-baseName org-wide TRUE pointer — which variant+rev is the active default.",
        fields=[
            FieldDef("baseName", "str", "Base name."),
            FieldDef("variantId", "str", "Currently-active variant id."),
            FieldDef("rev", "int", "Currently-active revision number."),
            FieldDef("org", "str", "Owning org slug."),
            FieldDef("updatedAt", "int?", "Epoch-ms of last pointer update.", optional=True),
        ],
    ),
    RecordDef(
        name="IdeaSourceRecord",
        description="One contribution to an idea (a PR or authored source that corroborates it).",
        fields=[
            FieldDef("ideaId", "str?", "The idea this source contributes to (the IDEASRC# partition key bridge).", optional=True),
            FieldDef("sourceId", "str?", "Stable id for this contribution (e.g. prRef or an authored content hash).", optional=True),
            FieldDef("org", "str?", "Owning org slug.", optional=True),
            FieldDef("sessionId", "str?", "Session that produced this contribution (enrichment only).", optional=True),
            FieldDef("segmentId", "str?", "Segment within the session.", optional=True),
            FieldDef("seq", "int?", "Per-session monotonic seq for ordering/observability.", optional=True),
            FieldDef("prRef", "str?", "Merged PR reference: owner/repo#<number> (inferred lane).", optional=True),
            FieldDef("anchors", "str?", "JSON-serialised list of {file, symbol} anchor pairs.", optional=True),
            FieldDef("authorityKind", "str?", "user_directive | authored_import | merged.", optional=True),
            FieldDef("verificationRung", "str?", "test | normal | bare — inferred from diff/PR metadata.", optional=True),
            FieldDef("authorId", "str?", "PR/commit author id for the author-credibility multiplier.", optional=True),
        ],
    ),
    RecordDef(
        name="IdeaRecord",
        description="A candidate or folded insight co-located with its skill family.",
        fields=[
            FieldDef("ideaId", "str", "Unique idea identifier (UUID or slug)."),
            FieldDef("skillBaseName", "str", "The skill family this idea belongs to."),
            FieldDef("org", "str", "Owning org slug."),
            FieldDef("body", "str", "The insight body (synthesised paragraph)."),
            FieldDef("status", "str", "open | folded."),
            FieldDef("corroborationVersion", "int", "Optimistic-concurrency token."),
            FieldDef("foldedIntoRev", "int?", "Revision this idea was folded into.", optional=True),
            FieldDef("invalidAt", "int?", "Epoch-ms of temporal retirement (supersession).", optional=True),
            FieldDef("supersededBy", "str?", "IdeaId that superseded this one.", optional=True),
            FieldDef("supersedes", "str[]", "IdeaIds this idea supersedes."),
            FieldDef("authored", "bool?", "True if this is an authored (non-inferred) idea.", optional=True),
            FieldDef("authorityKind", "str?", "user_directive | authored_import | merged.", optional=True),
            # --- U10 additions (Gap 6) ---
            FieldDef("refines", "str?", "IdeaId this idea refines (scoped-nuance coexistence edge).", optional=True),
            FieldDef("revivedAt", "int?", "Epoch-ms when a superseded idea was revived.", optional=True),
            FieldDef("legacyRecurrenceFold", "bool?", "True if this idea was folded under the old recurrence model (grandfather flag).", optional=True),
            FieldDef("sourceRef", "str?", "JSON-serialised source reference for authored ideas: {kind, label?, contentHash?}.", optional=True),
            FieldDef("scopeTag", "str?", "Scope tag for authored ideas: project | user | org | repo | skill.", optional=True),
            FieldDef("authorId", "str?", "Author id for the author-credibility weighting.", optional=True),
            FieldDef("verificationRung", "str?", "test | normal | bare — inferred from diff/PR metadata.", optional=True),
        ],
    ),
    RecordDef(
        name="GoldenCaseRecord",
        description="Before→after golden regression case captured at idea-fold time.",
        fields=[
            FieldDef("caseId", "str", "Typically the ideaId of the folded idea."),
            FieldDef("skillBaseName", "str", "The skill family guarded by this case."),
            FieldDef("org", "str", "Owning org slug."),
            FieldDef("before", "str", "The pre-fold skill body excerpt (the prompt context)."),
            FieldDef("after", "str", "The expected post-fold skill body (ground truth)."),
            FieldDef("ideaBody", "str", "The folded idea body (for drift detection)."),
            FieldDef("createdAt", "int?", "Epoch-ms when this case was captured.", optional=True),
        ],
    ),
    RecordDef(
        name="AnchorRecord",
        description="Code-anchor index entry linking (file, symbol) to an idea for the locality join.",
        fields=[
            FieldDef("ideaId", "str", "The idea this anchor points to."),
            FieldDef("ownerRepo", "str", "owner/repo (e.g. acme/backend)."),
            FieldDef("file", "str", "File path within the repo."),
            FieldDef("symbol", "str", "Enclosing named symbol (or '__file__' for file-level fallback)."),
            FieldDef("org", "str", "Owning org slug."),
            FieldDef("active", "bool", "False when the anchor has been retired (idea un-folded/superseded)."),
        ],
    ),
    RecordDef(
        name="BranchSessionRecord",
        description=(
            "Branch→session link written by the Go wrapper at git-push time (U7). "
            "Stores the sessionId(s), turn-range, and eagerly-distilled context "
            "for a (repo, branch) pair so U1 can enrich PR distillation without "
            "re-reading 90-day-TTL EVT# records at merge time."
        ),
        fields=[
            FieldDef("ownerRepo", "str", "owner/repo string, e.g. acme/backend."),
            FieldDef("branch", "str", "Git branch name, e.g. feat/my-feature."),
            FieldDef("org", "str", "Owning org slug (the daemon's resolved org)."),
            FieldDef("sessionId", "str", "Stable tab/session id (PinnedSessionID) of the authoring Claude session."),
            FieldDef("turnStart", "int?", "Start turn index of the relevant slice within the session.", optional=True),
            FieldDef("turnEnd", "int?", "End turn index of the relevant slice within the session.", optional=True),
            FieldDef("distilledContext", "str?", "Eagerly-distilled, scrubbed summary of the relevant turn slice.", optional=True),
            FieldDef("pushedAt", "int?", "Epoch-ms when the push was detected.", optional=True),
            FieldDef("ttlAt", "int?", "DynamoDB TTL epoch-second (90 days from pushedAt).", optional=True),
        ],
    ),
]

# ---------------------------------------------------------------------------
# The top-level schema bundle
# ---------------------------------------------------------------------------

SCHEMA = SchemaDef(
    version="1.0.0",
    pad_widths=PAD_WIDTHS,
    key_defs=KEY_DEFS,
    record_defs=RECORD_DEFS,
)
