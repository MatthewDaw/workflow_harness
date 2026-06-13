/**
 * GENERATED — do not edit by hand.
 *
 * This file re-exports the learning-service IDL-generated schema types into
 * @harness/shared so TS consumers (packages/backend, packages/web, the Go
 * wrapper's golden fixture) can import the single-source-of-truth key builders
 * and record interfaces without depending directly on packages/learning-service.
 *
 * The canonical source is:
 *   packages/learning-service/src/learning_service/schema/generated/ts_types.ts
 *
 * Regenerate the canonical source with:
 *   python -m learning_service.schema.codegen
 *
 * CI freshness check:
 *   python -m learning_service.schema.codegen --check
 *
 * The codegen also writes this file (packages/shared/src/learning-schema.ts) so
 * both copies are always in sync.  If they diverge, CI fails.
 *
 * IDL fingerprint: 55379a9c92d0b91e  (schema version 1.0.0)
 */

// ---------------------------------------------------------------------------
// Pad widths (mirrored from the IDL)
// ---------------------------------------------------------------------------

export const PAD_TS = 15 as const;
export const PAD_SEQ = 12 as const;
export const PAD_REV = 12 as const;

const _pad = (n: number, width: number): string =>
  String(Math.trunc(n)).padStart(width, '0');

// ---------------------------------------------------------------------------
// Key builders  (org-scope single-table design — SCOPE#org#<org> PK)
// ---------------------------------------------------------------------------

/** Live skill record (the current canonical body for a baseName). */
export const learningSkillKey = (org: string | number, baseName: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `SKILL#${baseName}`,
});

/** Immutable revision snapshot. rev is zero-padded to PAD_WIDTHS['rev']. */
export const learningRevisionKey = (org: string | number, variantId: string | number, rev: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `SKILL#${variantId}#r${_pad(Number(rev), 12)}`,
});

/** Per-baseName org-wide TRUE pointer (which variant+rev is the default). */
export const learningTruePointerKey = (org: string | number, baseName: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `SKILL#${baseName}#TRUE`,
});

/** A candidate/folded insight co-located with its skill family. */
export const learningIdeaKey = (org: string | number, skillBaseName: string | number, ideaId: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `IDEA#${skillBaseName}#${ideaId}`,
});

/** One contribution (a merged PR or authored source) under its idea. */
export const learningIdeaSourceKey = (org: string | number, ideaId: string | number, sourceId: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `IDEASRC#${ideaId}#${sourceId}`,
});

/** Before→after golden regression case captured at fold time. */
export const learningGoldenCaseKey = (org: string | number, skillBaseName: string | number, caseId: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `IDEAGOLD#${skillBaseName}#${caseId}`,
});

/** Code-anchor index entry (file, symbol) → ideaId for the locality join. */
export const learningAnchorKey = (org: string | number, ownerRepo: string | number, file: string | number, symbol: string | number, ideaId: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `ANCHOR#${ownerRepo}#${file}#${symbol}#${ideaId}`,
});

/** Org-scoped idempotency cursor — marks a PR as already ingested. */
export const learningProcessedPrKey = (org: string | number, ownerRepo: string | number, prNumber: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `PROCESSED#${ownerRepo}#${prNumber}`,
});

/** Append-only audit row under an idea (supersede/corroborate events). */
export const learningVerifyEventKey = (org: string | number, ideaId: string | number, seq: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `VERIFY#${ideaId}#${_pad(Number(seq), 12)}`,
});

/** Branch→session link written at git-push time (U7). Maps a (repo, branch) pair to the sessionId(s) + turn-range + eagerly-distilled context that produced the push.  TTL 90 days.  U1 reads distilledContext at merge time without re-reading EVT# records. */
export const learningBranchSessionKey = (org: string | number, ownerRepo: string | number, branch: string | number): { PK: string; SK: string } => ({
  PK: `SCOPE#org#${org}`,
  SK: `BSLINK#${ownerRepo}#${branch}`,
});

// ---------------------------------------------------------------------------
// Record interfaces  (generated from IDL — single source of truth)
// ---------------------------------------------------------------------------

/** The canonical live body of a skill (the current revision's content). */
export interface LearningSkillRecord {
  baseName: string;  // The skill's base name (org-unique).
  variantId: string;  // Active variant id (empty = org base).
  rev: number;  // Active revision number.
  body: string;  // Markdown/YAML skill body.
  org: string;  // Owning org slug.
  description?: string | undefined;  // Human-readable description.
  createdBy?: string | undefined;  // User who created this skill.
  updatedAt?: number | undefined;  // Epoch-ms timestamp of last revision write.
}

/** Immutable snapshot of a skill variant at a specific revision. */
export interface LearningRevisionRecord {
  baseName: string;  // Base name of the skill family.
  variantId: string;  // Variant id (empty = org base).
  rev: number;  // Revision number (monotonically increasing per variant).
  body: string;  // Skill body at this revision.
  org: string;  // Owning org slug.
  description?: string | undefined;  // Description at this revision.
  createdBy?: string | undefined;  // Author of this revision.
  createdAt?: number | undefined;  // Epoch-ms of when this revision was written.
  ideaId?: string | undefined;  // The idea that triggered this revision (fold path).
}

/** Per-baseName org-wide TRUE pointer — which variant+rev is the active default. */
export interface LearningTruePointerRecord {
  baseName: string;  // Base name.
  variantId: string;  // Currently-active variant id.
  rev: number;  // Currently-active revision number.
  org: string;  // Owning org slug.
  updatedAt?: number | undefined;  // Epoch-ms of last pointer update.
}

/** One contribution to an idea (a PR or authored source that corroborates it). */
export interface LearningIdeaSourceRecord {
  ideaId?: string | undefined;  // The idea this source contributes to (the IDEASRC# partition key bridge).
  sourceId?: string | undefined;  // Stable id for this contribution (e.g. prRef or an authored content hash).
  org?: string | undefined;  // Owning org slug.
  sessionId?: string | undefined;  // Session that produced this contribution (enrichment only).
  segmentId?: string | undefined;  // Segment within the session.
  seq?: number | undefined;  // Per-session monotonic seq for ordering/observability.
  prRef?: string | undefined;  // Merged PR reference: owner/repo#<number> (inferred lane).
  anchors?: string | undefined;  // JSON-serialised list of {file, symbol} anchor pairs.
  authorityKind?: string | undefined;  // user_directive | authored_import | merged.
  verificationRung?: string | undefined;  // test | normal | bare — inferred from diff/PR metadata.
  authorId?: string | undefined;  // PR/commit author id for the author-credibility multiplier.
}

/** A candidate or folded insight co-located with its skill family. */
export interface LearningIdeaRecord {
  ideaId: string;  // Unique idea identifier (UUID or slug).
  skillBaseName: string;  // The skill family this idea belongs to.
  org: string;  // Owning org slug.
  body: string;  // The insight body (synthesised paragraph).
  status: string;  // open | folded.
  corroborationVersion: number;  // Optimistic-concurrency token.
  foldedIntoRev?: number | undefined;  // Revision this idea was folded into.
  invalidAt?: number | undefined;  // Epoch-ms of temporal retirement (supersession).
  supersededBy?: string | undefined;  // IdeaId that superseded this one.
  supersedes: string[];  // IdeaIds this idea supersedes.
  authored?: boolean | undefined;  // True if this is an authored (non-inferred) idea.
  authorityKind?: string | undefined;  // user_directive | authored_import | merged.
  refines?: string | undefined;  // IdeaId this idea refines (scoped-nuance coexistence edge).
  revivedAt?: number | undefined;  // Epoch-ms when a superseded idea was revived.
  legacyRecurrenceFold?: boolean | undefined;  // True if this idea was folded under the old recurrence model (grandfather flag).
  sourceRef?: string | undefined;  // JSON-serialised source reference for authored ideas: {kind, label?, contentHash?}.
  scopeTag?: string | undefined;  // Scope tag for authored ideas: project | user | org | repo | skill.
  authorId?: string | undefined;  // Author id for the author-credibility weighting.
  verificationRung?: string | undefined;  // test | normal | bare — inferred from diff/PR metadata.
}

/** Before→after golden regression case captured at idea-fold time. */
export interface LearningGoldenCaseRecord {
  caseId: string;  // Typically the ideaId of the folded idea.
  skillBaseName: string;  // The skill family guarded by this case.
  org: string;  // Owning org slug.
  before: string;  // The pre-fold skill body excerpt (the prompt context).
  after: string;  // The expected post-fold skill body (ground truth).
  ideaBody: string;  // The folded idea body (for drift detection).
  createdAt?: number | undefined;  // Epoch-ms when this case was captured.
}

/** Code-anchor index entry linking (file, symbol) to an idea for the locality join. */
export interface LearningAnchorRecord {
  ideaId: string;  // The idea this anchor points to.
  ownerRepo: string;  // owner/repo (e.g. acme/backend).
  file: string;  // File path within the repo.
  symbol: string;  // Enclosing named symbol (or '__file__' for file-level fallback).
  org: string;  // Owning org slug.
  active: boolean;  // False when the anchor has been retired (idea un-folded/superseded).
}

/** Branch→session link written by the Go wrapper at git-push time (U7). Stores the sessionId(s), turn-range, and eagerly-distilled context for a (repo, branch) pair so U1 can enrich PR distillation without re-reading 90-day-TTL EVT# records at merge time. */
export interface LearningBranchSessionRecord {
  ownerRepo: string;  // owner/repo string, e.g. acme/backend.
  branch: string;  // Git branch name, e.g. feat/my-feature.
  org: string;  // Owning org slug (the daemon's resolved org).
  sessionId: string;  // Stable tab/session id (PinnedSessionID) of the authoring Claude session.
  turnStart?: number | undefined;  // Start turn index of the relevant slice within the session.
  turnEnd?: number | undefined;  // End turn index of the relevant slice within the session.
  distilledContext?: string | undefined;  // Eagerly-distilled, scrubbed summary of the relevant turn slice.
  pushedAt?: number | undefined;  // Epoch-ms when the push was detected.
  ttlAt?: number | undefined;  // DynamoDB TTL epoch-second (90 days from pushedAt).
}
