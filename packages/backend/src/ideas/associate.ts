import type { Repo } from '../db/repo.js';
import { type BedrockEmbedder, getEmbedder } from '../embeddings/bedrock.js';
import {
  SKILL_VECTOR_INDEX,
  getS3Vectors,
  type QueryHit,
  type S3Vectors,
} from '../embeddings/s3vectors.js';

/**
 * U8 — Topic ingestion & top-k skill retrieval (the RETRIEVAL half of
 * association).
 *
 * On a `session.topic` event (or the session-projection `description` change it
 * folds into), this embeds the topic `description` and pulls the org's top-k
 * MOST-SIMILAR skills from the `skills` vector index, dropping any below a
 * pre-judge similarity FLOOR. Association is CATALOG-WIDE and SESSION-INDEPENDENT
 * (R5): candidates are every skill in the org's catalog above the floor — NOT
 * limited to the skills the session happens to have enabled.
 *
 * This is the retrieval half ONLY. The judge rerank (U9) and idea
 * creation/merge (U10) are SEPARATE downstream units; this module deliberately
 * stops at a typed candidate list and leaves a clean seam:
 *
 *   - `AssociationCandidates` — the input U9's rerank will consume (the topic
 *     finding + the scored, floor-passing candidate skills). U9 ranks these and
 *     picks the single best (or rejects all to the bin); U10 then turns the
 *     verdict into a merged/created idea.
 *   - `AssociationUnassigned` — produced when NO candidate clears the floor
 *     (nothing reaches the judge). This MARKS the topic for the unassigned-bin
 *     path (R6/R7) but does NOT itself write the bin — the bin write is U9's
 *     responsibility. It is a seam, not the sink.
 *
 * ORG RESOLUTION is the security-critical part. There is NO `principal` in the
 * stream/event context — and even where one exists it MUST NOT be trusted here.
 * The org is resolved from the SESSION's owning PROJECT (the stamped
 * `project.org`, set from the creator's effective org at create time). A topic
 * whose session/project/org cannot be resolved yields an `unresolved` result
 * rather than guessing or defaulting to a wrong org, so org A's topic can never
 * retrieve org B's skills (R5/F2 cross-org isolation).
 */

/**
 * Pre-judge similarity floor (cosine similarity in [0,1]). A candidate skill
 * below this never reaches the judge (U9). Env overrideable for tuning against
 * real topics; conservative default kept here as a documented knob.
 */
export const SIMILARITY_FLOOR = Number(process.env.ASSOCIATION_SIMILARITY_FLOOR ?? 0.5);
/** Default number of candidate skills to retrieve for the judge (top-k). */
export const ASSOCIATION_TOP_K = Number(process.env.ASSOCIATION_TOP_K ?? 5);

/** Resolve the configured similarity floor at call time (env overrideable). */
function similarityFloor(): number {
  return Number(process.env.ASSOCIATION_SIMILARITY_FLOOR ?? SIMILARITY_FLOOR);
}

/** Resolve the configured top-k at call time (env overrideable). */
function topK(): number {
  return Number(process.env.ASSOCIATION_TOP_K ?? ASSOCIATION_TOP_K);
}

/**
 * A topic to associate. Carries the synthesis inputs U10 will eventually fold
 * (the topic `description` + the segment's `impl_learning`s) and the
 * per-`(sessionId, segmentId)` provenance the bin/idea will record. The org is
 * NOT carried here — it is resolved server-side from the session's project, so a
 * caller can never inject a foreign org.
 */
export interface TopicFinding {
  /** The session the topic event belongs to (used to resolve the owning org). */
  sessionId: string;
  /** The topic segment within the session. */
  segmentId: string;
  /** The stable topic label. */
  topicLabel: string;
  /** The rich, self-contained topic summary — what gets embedded for retrieval. */
  description?: string;
  /** The segment's `impl_learning`s — carried to U10 for synthesis (not embedded). */
  implLearnings?: string[];
  /** The event seq (provenance / ordering). */
  seq?: number;
}

/** A scored candidate skill that cleared the pre-judge floor (the U9 input). */
export interface CandidateSkill {
  /** The skill family name (matches the skill vector's `skillBaseName`). */
  skillBaseName: string;
  /** Cosine similarity in [0,1] (higher is closer). */
  score: number;
}

/** Why association produced no candidate list (the bin / no-op reasons). */
export type AssociationOutcome = 'candidates' | 'unassigned' | 'unresolved';

/**
 * The retrieval result handed to U9. Three shapes, discriminated on `outcome`:
 *
 *  - `candidates`  — at least one skill cleared the floor; U9 reranks these.
 *  - `unassigned`  — the topic resolved to an org but NO skill cleared the floor;
 *                    U9 routes it to the unassigned bin (this is the SEAM, not
 *                    the bin write).
 *  - `unresolved`  — the session/project/org could not be resolved (no pointer,
 *                    no project, no stamped org), or the topic had no embeddable
 *                    description; nothing to do. A NO-OP, never a wrong-org guess.
 */
export interface AssociationResult {
  outcome: AssociationOutcome;
  /** Resolved owning org (present unless `unresolved`). */
  org?: string;
  /** The originating topic (carried through to U9/U10). */
  finding: TopicFinding;
  /** Floor-passing candidates, sorted strongest-first (only on `candidates`). */
  candidates: CandidateSkill[];
}

/** Injectable collaborators (tests pass mocks; the runtime uses the defaults). */
export interface AssociateDeps {
  repo: Repo;
  embedder?: BedrockEmbedder;
  vectors?: S3Vectors;
}

/**
 * Resolve the owning org for a session WITHOUT trusting any caller-supplied org
 * or `principal.org`. The session pointer resolves the projectId; the project
 * carries the org stamped from the creator's effective org at create time. Any
 * missing link returns undefined so the caller treats it as `unresolved` rather
 * than defaulting to a wrong org (cross-org isolation, F2).
 */
async function resolveOrg(
  repo: Repo,
  sessionId: string,
): Promise<{ org: string; projectId: string; repoId?: string } | undefined> {
  const session = await repo.getSessionById(sessionId);
  if (!session?.projectId) return undefined;
  const project = await repo.getProject(session.projectId);
  const org = project?.org;
  if (!org) return undefined;
  return {
    org,
    projectId: session.projectId,
    ...(project?.repo !== undefined ? { repoId: project.repo } : {}),
  };
}

/**
 * Retrieve the top-k candidate skills for a topic (U8). Embeds the topic
 * `description`, queries the org's skill vector index with the org filter and
 * the pre-judge floor, and returns a typed `AssociationResult` for U9:
 *
 *  - resolves the org from the session's project (NEVER `principal.org`);
 *  - embeds `description` and runs `queryTopK(SKILL_VECTOR_INDEX, ...)` with the
 *    `orgFilter` (isolation) and `floor` (pre-judge cut);
 *  - `candidates` when ≥1 skill clears the floor; `unassigned` (the bin seam)
 *    when the org resolved but all candidates fell below the floor; `unresolved`
 *    when the org or an embeddable description is missing.
 *
 * Errors (embed / query) are NOT swallowed — they propagate so the stream
 * consumer marks the record a batch-item-failure and the stream redelivers/DLQs
 * it, rather than silently dropping a topic (mirrors the U3 skill-embed path).
 */
export async function associateTopic(
  finding: TopicFinding,
  deps: AssociateDeps,
): Promise<AssociationResult> {
  const { repo } = deps;

  // 1. Resolve the owning org from the session's project — never a caller org.
  const resolved = await resolveOrg(repo, finding.sessionId);
  if (!resolved) {
    return { outcome: 'unresolved', finding, candidates: [] };
  }
  const { org } = resolved;

  // 2. Nothing to embed without a topic description → no-op (not a wrong-org guess).
  const description = finding.description?.trim();
  if (!description) {
    return { outcome: 'unresolved', org, finding, candidates: [] };
  }

  // 3. Embed the topic and pull the org's top-k skills above the pre-judge floor.
  const embedder = deps.embedder ?? getEmbedder();
  const vectors = deps.vectors ?? getS3Vectors();
  const floor = similarityFloor();
  const { vector } = await embedder.embed(description);
  const hits: QueryHit[] = await vectors.queryTopK(SKILL_VECTOR_INDEX, vector, topK(), {
    orgFilter: org,
    floor,
  });

  const candidates: CandidateSkill[] = hits
    .map((h) => ({
      skillBaseName:
        (h.metadata as { skillBaseName?: string }).skillBaseName ?? h.key,
      score: h.score,
    }))
    .sort((a, b) => b.score - a.score);

  // 4. All below floor → mark for the unassigned-bin path (U9 writes the bin).
  if (candidates.length === 0) {
    return { outcome: 'unassigned', org, finding, candidates: [] };
  }

  return { outcome: 'candidates', org, finding, candidates };
}
