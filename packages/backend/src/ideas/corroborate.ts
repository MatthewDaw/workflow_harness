import { randomUUID } from 'node:crypto';
import {
  corroborationCount,
  type Idea,
  type IdeaSource,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { type BedrockEmbedder, getEmbedder } from '../embeddings/bedrock.js';
import {
  IDEA_VECTOR_INDEX,
  getS3Vectors,
  type S3Vectors,
} from '../embeddings/s3vectors.js';
import { getIdeaWriter, type IdeaFinding, type IdeaWriter } from './synth.js';

/**
 * U7 — Corroboration + within-skill dedup with MERGE-REWRITE.
 *
 * On a new finding for skill S:
 *  1. Synthesize the finding into a skill-ready concept (the idea-writer), embed
 *     it, and query the org's IDEA index scoped to S for the nearest existing
 *     idea.
 *  2. If the top match's similarity ≥ `IDEA_MERGE_THRESHOLD`, MERGE: re-synthesize
 *     the matched idea's `text` (fold in the near-duplicate's nuance — the
 *     concept sharpens, not just the count) and add this session via
 *     `corroborateIdeaConditional`. Adding a session already counted is a no-op
 *     (AE4 idempotency), since corroboration is `|distinct sessionId|`.
 *  3. Otherwise CREATE a new idea and index its vector.
 *
 * Segments within ONE session are deduped BEFORE counting — the corroboration
 * unit is the SESSION, so a long multi-segment session counts once.
 *
 * The threshold biases STRICT (under-merge rather than over) so genuinely
 * different lessons never falsely corroborate; `/skill-idea-iterate` is where a
 * human merges near-duplicates the strict matcher missed (plan KTD).
 */

/**
 * Idea-merge similarity threshold (cosine similarity in [0,1]). Strict-ish: a
 * top match must be at/above this to merge; below it, a new idea is born. Env
 * overrideable for tuning against real topics.
 */
export const IDEA_MERGE_THRESHOLD = Number(process.env.IDEA_MERGE_THRESHOLD ?? 0.9);
/** Corroboration K — distinct sessions required to mark an idea corroborated. */
export const CORROBORATION_K = Number(process.env.CORROBORATION_K ?? 2);

/** Resolve the configured merge threshold at call time (env overrideable). */
function mergeThreshold(): number {
  return Number(process.env.IDEA_MERGE_THRESHOLD ?? IDEA_MERGE_THRESHOLD);
}

/**
 * A finding for skill S arriving from the association pipeline (U9/U10). It
 * carries the synthesis inputs (topic `description` + `impl_learning`s) and the
 * per-`(sessionId, segmentId)` provenance. One call may carry multiple segments
 * from the SAME session; they are deduped onto a single source before counting.
 */
export interface Finding {
  /** Owning org — every idea row + vector is org-partitioned for isolation. */
  org: string;
  /** The skill family this finding routes to (the chosen best skill). */
  skillBaseName: string;
  /** Synthesis inputs for the idea-writer (description + impl learnings + snippet). */
  content: IdeaFinding;
  /** The corroboration unit: this finding's session. */
  sessionId: string;
  /** One or more segments within that session that produced this finding. */
  segments: Array<{ segmentId: string; seq: number; snippet?: string }>;
  /** Provenance for variant-scoped folding (carried onto the source). */
  projectId?: string;
  repoId?: string;
}

/** The outcome of corroborating a finding. */
export interface CorroborateResult {
  /** The idea the finding landed on (created or merged-into). */
  idea: Idea;
  /** Whether a brand-new idea was created (vs merged into an existing one). */
  created: boolean;
  /**
   * Whether this finding's session was newly counted. `false` means the session
   * was already in the idea's set, so corroboration did NOT change (AE4 no-op).
   */
  sessionCounted: boolean;
  /** Whether the idea is now corroborated (`|distinct sessions| >= K`). */
  corroborated: boolean;
}

/** Injectable collaborators (tests pass mocks; the runtime uses the defaults). */
export interface CorroborateDeps {
  repo: Repo;
  embedder?: BedrockEmbedder;
  vectors?: S3Vectors;
  writer?: IdeaWriter;
}

/**
 * Collapse a finding's segments (within one session) into a single source. The
 * corroboration unit is the session, so a multi-segment finding contributes ONE
 * source; we keep the lowest-`seq` segment as the representative (earliest) and
 * its snippet as provenance.
 */
function sessionSource(finding: Finding): IdeaSource {
  const earliest = [...finding.segments].sort((a, b) => a.seq - b.seq)[0]!;
  return {
    sessionId: finding.sessionId,
    segmentId: earliest.segmentId,
    seq: earliest.seq,
    snippet: earliest.snippet ?? '',
    ...(finding.projectId !== undefined ? { projectId: finding.projectId } : {}),
    ...(finding.repoId !== undefined ? { repoId: finding.repoId } : {}),
  };
}

/** Add a session source if not already present (dedup on sessionId). Returns the new array + whether it changed. */
function withSession(sources: IdeaSource[], next: IdeaSource): { sources: IdeaSource[]; added: boolean } {
  if (sources.some((s) => s.sessionId === next.sessionId)) {
    return { sources, added: false };
  }
  return { sources: [...sources, next], added: true };
}

/**
 * Corroborate a finding into skill S's ideas: merge into the nearest existing
 * idea (re-synthesizing its concept) when within threshold, else create a new
 * idea and index its vector. Idempotent on a session already counted (AE4).
 */
export async function corroborateFinding(
  finding: Finding,
  deps: CorroborateDeps,
): Promise<CorroborateResult> {
  const { repo } = deps;
  const embedder = deps.embedder ?? getEmbedder();
  const vectors = deps.vectors ?? getS3Vectors();
  const writer = deps.writer ?? getIdeaWriter();
  const now = Date.now();

  const source = sessionSource(finding);

  // Synthesize a skill-ready concept for THIS finding, then embed it to find the
  // nearest existing idea on the same skill.
  const newConcept = await writer.write(finding.content);
  const embedding = await embedder.embed(newConcept);

  // Query the org's IDEA index. The vector key namespaces by skill (see
  // ideaVectorKey), but S3 Vectors top-k is global within the index, so we also
  // restrict to skill S by metadata via the candidate's `skillBaseName`.
  const hits = await vectors.queryTopK(IDEA_VECTOR_INDEX, embedding.vector, 5, {
    orgFilter: finding.org,
  });
  const top = hits.find(
    (h) => (h.metadata as { skillBaseName?: string }).skillBaseName === finding.skillBaseName,
  );

  if (top && top.score >= mergeThreshold()) {
    // MERGE into the matched idea. Re-read for the current corroborationVersion,
    // then re-synthesize the concept and add this session (no-op if counted).
    const ideaId = (top.metadata as { ideaId?: string }).ideaId ?? top.key;
    const existing = await repo.getIdea(finding.org, finding.skillBaseName, ideaId);
    if (existing) {
      const { sources, added } = withSession(existing.sources, source);
      // Re-synthesize only when this session adds new evidence; a re-emit from an
      // already-counted session is a pure no-op (AE4) — no rewrite, no version bump.
      if (!added) {
        return {
          idea: existing,
          created: false,
          sessionCounted: false,
          corroborated: corroborationCount(existing) >= CORROBORATION_K,
        };
      }
      const mergedText = await writer.merge(existing.text, finding.content);
      const updated: Idea = {
        ...existing,
        text: mergedText,
        sources,
        ideaEmbeddingVersion: embedding.embeddingVersion,
        updatedAt: now,
      };
      const res = await repo.corroborateIdeaConditional(updated, existing.corroborationVersion);
      // A concurrent writer won the conditional race; surface the existing idea
      // unchanged rather than clobbering (caller may retry).
      const idea = res.written ? updated : existing;
      return {
        idea,
        created: false,
        sessionCounted: res.written,
        corroborated: corroborationCount(idea) >= CORROBORATION_K,
      };
    }
    // The vector pointed at a missing idea (deleted/cascaded) — fall through to create.
  }

  // CREATE a new idea and index its vector.
  const ideaId = randomUUID();
  const idea: Idea = {
    ideaId,
    skillBaseName: finding.skillBaseName,
    org: finding.org,
    text: newConcept,
    sources: [source],
    status: 'open',
    ideaEmbeddingVersion: embedding.embeddingVersion,
    corroborationVersion: 0,
    createdAt: now,
    updatedAt: now,
  };
  const res = await repo.corroborateIdeaConditional(idea, undefined);
  // A create collision is vanishingly unlikely (uuid id) — if it loses, the
  // existing row wins; we still report the created idea's shape for the caller.
  if (res.written) {
    await vectors.putVectors(IDEA_VECTOR_INDEX, [
      {
        key: ideaVectorKey(finding.org, finding.skillBaseName, ideaId),
        vector: embedding.vector,
        metadata: {
          org: finding.org,
          skillBaseName: finding.skillBaseName,
          ideaId,
          embeddingVersion: embedding.embeddingVersion,
        },
      },
    ]);
  }
  return {
    idea,
    created: res.written,
    sessionCounted: res.written,
    corroborated: corroborationCount(idea) >= CORROBORATION_K,
  };
}

/**
 * The deterministic vector key for an idea within the `ideas` index:
 * `<org>#<skillBaseName>#<ideaId>`. One vector per idea. (Mirrors
 * `skillVectorKey`'s shape in s3vectors.ts.)
 */
export function ideaVectorKey(org: string, skillBaseName: string, ideaId: string): string {
  return `${org}#${skillBaseName}#${ideaId}`;
}
