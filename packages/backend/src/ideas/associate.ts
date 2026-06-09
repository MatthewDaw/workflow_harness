import { randomUUID } from 'node:crypto';
import { orgScope, type Idea, type IdeaSource, type UnassignedEntry } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { type BedrockEmbedder, getEmbedder } from '../embeddings/bedrock.js';
import {
  SKILL_VECTOR_INDEX,
  getS3Vectors,
  type QueryHit,
  type S3Vectors,
} from '../embeddings/s3vectors.js';
import {
  getRerankJudge,
  judgeConfidenceBar,
  type JudgeCandidate,
  type RerankJudge,
} from '../rerank/judge.js';
import {
  CORROBORATION_K,
  corroborateFinding,
  type CorroborateDeps,
  type CorroborateResult,
  type Finding,
} from './corroborate.js';
import { type IdeaWriter } from './synth.js';

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
  /** The session's owning project (provenance; present once the org resolved). */
  projectId?: string;
  /** The project's repo (provenance for variant-scoped folding; if stamped). */
  repoId?: string;
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
  /** The Bedrock Claude-Haiku rerank judge (U9). Defaults to the process judge. */
  judge?: RerankJudge;
  /** The Bedrock idea-writer (U7/U10). Defaults to the process writer. */
  writer?: IdeaWriter;
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
  const { org, projectId } = resolved;
  // Provenance carried onto every resolved result (bin entry / U10 idea source).
  const prov = {
    org,
    projectId,
    ...(resolved.repoId !== undefined ? { repoId: resolved.repoId } : {}),
  };

  // 2. Nothing to embed without a topic description → no-op (not a wrong-org guess).
  const description = finding.description?.trim();
  if (!description) {
    return { outcome: 'unresolved', ...prov, finding, candidates: [] };
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
      skillBaseName: (h.metadata as { skillBaseName?: string }).skillBaseName ?? h.key,
      score: h.score,
    }))
    .sort((a, b) => b.score - a.score);

  // 4. All below floor → mark for the unassigned-bin path (U9 writes the bin).
  if (candidates.length === 0) {
    return { outcome: 'unassigned', ...prov, finding, candidates: [] };
  }

  return { outcome: 'candidates', ...prov, finding, candidates };
}

/**
 * U9 — judge rerank → the chosen skill, or the unassigned bin.
 *
 * The DECISION half of association. It runs U8 retrieval, then for a `candidates`
 * result asks the Bedrock Claude-Haiku judge to pick the SINGLE best skill (or
 * reject all). The pipeline has TWO thresholds: U8's similarity FLOOR (does
 * anything reach the judge) and the judge CONFIDENCE BAR (does the judge's pick
 * clear the bar). The outcomes:
 *
 *  - `routed`     — the judge accepted a skill above the bar. Carries the chosen
 *                   `skillBaseName` + confidence. This is the SEAM U10 consumes:
 *                   U10 turns it into a merged/created idea (NOT done here).
 *  - `unassigned` — no candidate cleared the floor (U8), OR the judge said `none`,
 *                   OR the judge's pick was below the confidence bar (R6). In all
 *                   three cases the topic is WRITTEN to the org's unassigned bin
 *                   via `repo.putUnassigned`, carrying topic provenance (R7), and
 *                   the written entry is returned.
 *  - `unresolved` — the session/org/description could not be resolved (a no-op,
 *                   never a wrong-org guess; nothing written).
 *
 * Idea creation (U10) is deliberately NOT implemented here — `routed` is the
 * clean seam it will consume. Errors (embed/query/judge) are NOT swallowed; they
 * propagate so the stream consumer marks a batch-item-failure and the stream
 * redelivers/DLQs the record rather than silently dropping a topic.
 */
export type RouteOutcome = 'routed' | 'unassigned' | 'unresolved';

/** Why a topic was sent to the unassigned bin (observability). */
export type UnassignedReason = 'no-candidates' | 'judge-none' | 'below-confidence';

export interface RouteResult {
  outcome: RouteOutcome;
  /** Resolved owning org (present unless `unresolved`). */
  org?: string;
  /** The session's owning project (provenance; present once the org resolved). */
  projectId?: string;
  /** The project's repo (provenance for variant-scoped folding; if stamped). */
  repoId?: string;
  /** The originating topic (carried through to U10 on `routed`). */
  finding: TopicFinding;
  /** The chosen skill (only on `routed`) — the SEAM U10 consumes. */
  skillBaseName?: string;
  /** The judge's confidence in the pick (only on `routed`). */
  confidence?: number;
  /** Why the topic was binned (only on `unassigned`). */
  reason?: UnassignedReason;
  /** The bin entry written (only on `unassigned`). */
  binEntry?: UnassignedEntry;
}

/** Build the provenance source for a binned topic (mirrors an idea source). */
function topicSource(finding: TopicFinding, projectId?: string, repoId?: string): IdeaSource {
  return {
    sessionId: finding.sessionId,
    segmentId: finding.segmentId,
    seq: finding.seq ?? 0,
    snippet: finding.description?.trim() ?? '',
    ...(projectId !== undefined ? { projectId } : {}),
    ...(repoId !== undefined ? { repoId } : {}),
  };
}

/** Write the topic to the org's unassigned bin with full provenance (R6/R7). */
async function bin(
  repo: Repo,
  org: string,
  finding: TopicFinding,
  projectId: string | undefined,
  repoId: string | undefined,
  reason: UnassignedReason,
): Promise<RouteResult> {
  const now = Date.now();
  const entry: UnassignedEntry = {
    entryId: randomUUID(),
    org,
    text: finding.description?.trim() ?? finding.topicLabel,
    sources: [topicSource(finding, projectId, repoId)],
    createdAt: now,
    updatedAt: now,
  };
  await repo.putUnassigned(entry);
  return { outcome: 'unassigned', org, finding, reason, binEntry: entry };
}

/**
 * Run the full association decision: retrieve (U8) → judge rerank (U9) → the
 * chosen skill or the unassigned bin. See `RouteResult` for the outcomes; this
 * leaves idea creation to U10 (the `routed` result is its input seam).
 */
export async function routeTopic(finding: TopicFinding, deps: AssociateDeps): Promise<RouteResult> {
  const retrieval = await associateTopic(finding, deps);

  if (retrieval.outcome === 'unresolved') {
    return { outcome: 'unresolved', org: retrieval.org, finding };
  }

  const org = retrieval.org!;
  const { projectId, repoId } = retrieval;

  // No candidate cleared the pre-judge floor → straight to the bin (U8 seam).
  if (retrieval.outcome === 'unassigned') {
    return bin(deps.repo, org, finding, projectId, repoId, 'no-candidates');
  }

  // Ask the judge to pick the single best skill among the floor-passing set.
  const judge = deps.judge ?? getRerankJudge();
  const candidates = await loadCandidateDescriptions(deps.repo, org, retrieval.candidates);
  const verdict = await judge.judge(
    { topicLabel: finding.topicLabel, description: finding.description!.trim() },
    candidates,
  );

  if (verdict.outcome === 'none') {
    return bin(deps.repo, org, finding, projectId, repoId, 'judge-none');
  }
  // Below the confidence bar → bin (R6), exactly like a `none`.
  if (verdict.confidence < judgeConfidenceBar()) {
    return bin(deps.repo, org, finding, projectId, repoId, 'below-confidence');
  }

  // Accepted → the SEAM U10 consumes (idea creation done in finalizeRoute).
  // Provenance (projectId/repoId) rides along so U10 can stamp the idea source.
  return {
    outcome: 'routed',
    org,
    ...(projectId !== undefined ? { projectId } : {}),
    ...(repoId !== undefined ? { repoId } : {}),
    finding,
    skillBaseName: verdict.skillBaseName,
    confidence: verdict.confidence,
  };
}

/**
 * Hydrate each candidate `skillBaseName` with its current description for the
 * judge. A candidate whose skill row can't be read (deleted/cascaded between
 * retrieval and judge) is still offered with an empty description rather than
 * dropped — the judge reasons over name alone in that rare case.
 */
async function loadCandidateDescriptions(
  repo: Repo,
  org: string,
  candidates: CandidateSkill[],
): Promise<JudgeCandidate[]> {
  const scope = orgScope(org);
  return Promise.all(
    candidates.map(async (c) => {
      const skill = await repo.getSkill(scope, c.skillBaseName);
      return { skillBaseName: c.skillBaseName, description: skill?.description ?? '' };
    }),
  );
}

/**
 * U10 — idea creation & re-evaluation MOVE semantics (the FINAL pipeline step).
 *
 * U9 stops at `routed` (the judge accepted a skill); this turns that verdict into
 * a merged/created idea on the chosen skill by mapping the route into the
 * `Finding` shape `corroborateFinding` (U7) expects and calling it — the
 * synthesize-and-merge-or-create (and the idempotent distinct-session count)
 * happen there. This module does NOT re-implement that; it only MAPS + invokes
 * (and handles the cross-skill move below).
 *
 * RE-EVALUATION MOVE (R13). Association is per `(sessionId, segmentId)` and a
 * topic EVOLVES: the same segment can re-evaluate to a DIFFERENT best skill than
 * it did before. When that happens we MOVE the session's contribution — REMOVE
 * this session from the prior skill's idea (decrementing its corroboration) and
 * ADD it to the newly-chosen skill — rather than spraying partial credit across
 * both skills. A session that drifts X→Y must leave X's count, not double-count.
 *
 * The move is done by scanning the org's ideas for any idea on a skill OTHER than
 * the chosen one whose live `sources` already counts this session, and removing
 * the session there (a conditional write so a concurrent fold/corroboration can't
 * be clobbered). FOLDED ideas are left alone — their lesson is already in the
 * body and their live `sources` are immutable history (U20); we never strip a
 * session out of a folded idea. Then the chosen skill's idea is corroborated as
 * usual, which re-adds the session there.
 */

/** The result of finalizing a `routed` association into an idea. */
export interface FinalizeResult {
  /** The corroboration outcome on the CHOSEN skill (create/merge + count). */
  corroboration: CorroborateResult;
  /**
   * Ideas the session was MOVED OFF of (R13) — a prior, different skill's idea
   * that lost this session because the topic re-evaluated elsewhere. Empty when
   * this is the session's first/stable home. Each carries the post-removal idea.
   */
  movedFrom: Idea[];
}

/**
 * Map a `routed` `RouteResult` into the corroborate `Finding` shape. The chosen
 * `skillBaseName` and the resolved provenance (org / projectId / repoId) come
 * from the route; the synthesis inputs (description + impl learnings + raw
 * snippet) come from the originating topic finding. One segment per call (the
 * `(sessionId, segmentId)` the topic event carried).
 */
function buildFinding(route: RouteResult): Finding {
  const finding = route.finding;
  const snippet = finding.description?.trim() ?? '';
  return {
    org: route.org!,
    skillBaseName: route.skillBaseName!,
    content: {
      ...(finding.description !== undefined ? { description: finding.description } : {}),
      ...(finding.implLearnings !== undefined ? { implLearnings: finding.implLearnings } : {}),
      snippet,
    },
    sessionId: finding.sessionId,
    segments: [{ segmentId: finding.segmentId, seq: finding.seq ?? 0, snippet }],
    ...(route.projectId !== undefined ? { projectId: route.projectId } : {}),
    ...(route.repoId !== undefined ? { repoId: route.repoId } : {}),
  };
}

/**
 * R13 — strip a session from every OPEN idea on a skill OTHER than `keepSkill`,
 * so a topic that drifts to a new best skill MOVES rather than double-counts.
 * Returns the post-removal ideas it touched. Folded ideas are never stripped
 * (their live sources are immutable history; U20). Uses the conditional write so
 * a concurrent corroboration/fold is not clobbered — a lost race leaves the
 * stale session in place, which a later re-eval will clear (eventual, not racy).
 */
async function moveSessionFromOtherSkills(
  repo: Repo,
  org: string,
  sessionId: string,
  keepSkill: string,
): Promise<Idea[]> {
  const all = await repo.listIdeasForOrg(org);
  const moved: Idea[] = [];
  for (const idea of all) {
    if (idea.skillBaseName === keepSkill) continue; // the new home — leave it.
    if (idea.status === 'folded') continue; // immutable history (U20).
    if (!idea.sources.some((s) => s.sessionId === sessionId)) continue; // not here.
    const sources = idea.sources.filter((s) => s.sessionId !== sessionId);
    const updated: Idea = { ...idea, sources, updatedAt: Date.now() };
    const res = await repo.corroborateIdeaConditional(updated, idea.corroborationVersion);
    if (res.written) moved.push(updated);
  }
  return moved;
}

/**
 * U10 — finalize a `routed` association into a merged/created idea on the chosen
 * skill, MOVING the session off any prior, different skill first (R13). The
 * `route` MUST be a `routed` result (it carries the chosen `skillBaseName` and
 * the resolved org/provenance); a non-`routed` route throws rather than guessing.
 *
 * Order matters: we move the session OFF other skills BEFORE corroborating onto
 * the new one, so a same-call no-op can't briefly drop the count to zero on a
 * skill that is in fact the same one (the move skips `keepSkill`). Provenance
 * (R15) rides through `buildFinding` onto the idea source.
 */
export async function finalizeRoute(
  route: RouteResult,
  deps: AssociateDeps,
): Promise<FinalizeResult> {
  if (route.outcome !== 'routed' || !route.org || !route.skillBaseName) {
    throw new Error('finalizeRoute requires a routed RouteResult');
  }
  const org = route.org;
  const skillBaseName = route.skillBaseName;
  const sessionId = route.finding.sessionId;

  // R13: move the session off any prior, different skill's idea first.
  const movedFrom = await moveSessionFromOtherSkills(deps.repo, org, sessionId, skillBaseName);

  // Map → the corroborate Finding and create/merge on the chosen skill (U7).
  const corroDeps: CorroborateDeps = {
    repo: deps.repo,
    ...(deps.embedder !== undefined ? { embedder: deps.embedder } : {}),
    ...(deps.vectors !== undefined ? { vectors: deps.vectors } : {}),
    ...(deps.writer !== undefined ? { writer: deps.writer } : {}),
  };
  const corroboration = await corroborateFinding(buildFinding(route), corroDeps);

  return { corroboration, movedFrom };
}

/**
 * The FULL ideas pipeline for one topic finding: retrieve (U8) → judge rerank
 * (U9) → create/merge the idea + move-on-re-eval (U10). On a `routed` verdict the
 * idea is created/merged here; on `unassigned`/`unresolved` the route result is
 * returned untouched (the bin write already happened in `routeTopic`, or it was a
 * no-op). The `finalize` is present only when an idea was actually written.
 */
export interface PipelineResult {
  route: RouteResult;
  /** Present only when the route was `routed` and an idea was created/merged. */
  finalize?: FinalizeResult;
}

/**
 * Run the end-to-end ideas pipeline for a topic finding. This is the single
 * entry point the stream consumer (U8 trigger) calls; it composes `routeTopic`
 * and `finalizeRoute` so callers don't re-stitch the seam.
 */
export async function associateAndFinalize(
  finding: TopicFinding,
  deps: AssociateDeps,
): Promise<PipelineResult> {
  const route = await routeTopic(finding, deps);
  if (route.outcome !== 'routed') {
    return { route };
  }
  const finalize = await finalizeRoute(route, deps);
  return { route, finalize };
}

/** Re-exported so callers can gate on the corroboration K without a second import. */
export { CORROBORATION_K };
