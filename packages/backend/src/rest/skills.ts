import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import {
  orgScope,
  skillSchema,
  variantIdFor,
  type GoldenCase,
  type GoldenReplayResult,
  type Idea,
  type Skill,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  conflict,
  defaultRepo,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
} from './runtime.js';
import { isBuiltin, requireOrgCatalogAdmin, requireOrgCatalogAuth } from './scopeauth.js';
import { makeBundleMemberHandlers } from './bundles.js';
import { makeCatalogHandlers } from './catalogResource.js';
import { parseVariantId } from '../db/keys.js';
import {
  getS3Vectors,
  SKILL_VECTOR_INDEX,
  skillVectorKey,
  type S3Vectors,
} from '../embeddings/s3vectors.js';
import { getGoldenJudge, replayGoldenCases, type GoldenJudge } from '../rerank/golden.js';
import {
  callPythonAuthorRevision,
  usesPythonWriter,
  type AuthorRevisionRequest,
  type AuthorRevisionResponse,
} from '../python-writer-client.js';

/**
 * REST: skills + bundles — collapsed to a single ORG catalog.
 *
 *   GET    /skills                        — the caller's org catalog
 *   GET    /skills/:name                  — one skill from the org catalog
 *   POST   /skills                        — create (server forces org scope + createdBy; admin)
 *   PUT    /skills/:name                  — update (scope/createdBy immutable; admin)
 *   DELETE /skills/:name                  — delete (admin), 204
 *   POST   /skills/:name/members          — add a member ref to a bundle
 *   DELETE /skills/:name/members/:member  — eject a member (it stays standalone)
 *   POST   /skills/:name/dissolve         — flatten a bundle: members standalone, bundle removed
 *   GET    /skills/:name/usage            — count of agents depending on the skill
 *   POST   /skills/:name/promote          — repoint the org-wide TRUE pointer (skill-edit)
 *   POST   /skills/:name/ideas/:ideaId/fold — fold an idea into a new revision (U16)
 *
 * Bundles hold member *refs* (names). A member may itself be a bundle (nesting);
 * resolution is transitive. Ejecting a member only edits the bundle.
 *
 * SKILL-EDIT PERMISSION (U16): writing a skill revision and repointing TRUE are
 * the SAME authority — both are admin-gated server-side (the catalog write gate).
 * The pre-U16 split (revision write admin-gated, promote open to any member) let a
 * non-admin repoint TRUE to an unreviewed variant, so it was reconciled: `fold`
 * (which writes a revision) and `promote` (which repoints TRUE) both require admin.
 */

export interface SkillsDeps {
  repo: Repo;
  /**
   * S3 Vectors client for the U21 delete path. Optional + injectable for tests;
   * the runtime handler defaults to the process-wide `getS3Vectors()`.
   */
  vectors?: S3Vectors;
  /**
   * OpenRouter golden judge for the U18 fold→promote regression replay. Optional +
   * injectable for tests; the runtime handler defaults to `getGoldenJudge()`.
   */
  golden?: GoldenJudge;
  /**
   * U10 / MAT-141 Gap 1 — the ONE Python author-revision writer. When configured
   * (``PYTHON_AUTHOR_REVISION_URL`` set, or this injected for tests), the fold
   * path authors the new skill revision + golden case through this single Python
   * writer instead of writing the revision itself via ``repo.putNewVersion``.
   * Injectable so tests can prove the TS path no longer authors revisions itself.
   */
  authorRevision?: (req: AuthorRevisionRequest) => Promise<AuthorRevisionResponse>;
}

const handlers = makeCatalogHandlers({
  kind: 'SKILL',
  schema: skillSchema,
  label: 'skill',
  responseKey: 'skill',
  listKey: 'skills',
  seedHint: 'catalog/skills',
  repoOps: {
    list: (repo, org, userId) => repo.listSkills(org, userId),
    get: (repo, scope, name) => repo.getSkill(scope, name),
    del: (repo, scope, name) => repo.deleteSkill(scope, name),
  },
  bundles: { noun: 'a bundle', push: 'a skill push', localName: 'skill' },
  adminGatedPromote: true,
  enrichGetWithAuthorNames: true,
});

export const resolveSkills = handlers.list;
export const createSkill = handlers.create;
export const getSkill = handlers.get;

/**
 * U19 — VARIANT-SCOPED FOLDING. Derive the variant the fold should target from
 * the IDEA'S PROVENANCE rather than only the request body. An idea's `sources`
 * carry the `repoId` (and `projectId`) of the sessions that produced it; when
 * every contributing source agrees on a single `repoId`, the lesson is specific
 * to that repo's variant line and the fold should land THERE (forking the
 * built-in into `<base>#R#<repoId>#U#<author>`) rather than on the org base.
 * When the sources carry no `repoId`, or DISAGREE (a cross-repo lesson), the
 * provenance implies nothing and the fold targets the org base (empty repo) —
 * the caller may still fork explicitly via the request body.
 *
 * Precedence: an EXPLICIT `repoId` in the request body always wins (the human
 * picked a target); provenance only fills in a `repoId` the body omitted. The
 * `authorUserId` follows the same rule — body first, else the verified caller
 * (a fork is authored by whoever performs the fold). Returns `{}` when the fold
 * targets the base.
 */
export function foldTargetVariant(
  idea: Idea,
  body: { repoId?: string; authorUserId?: string },
  callerUserId: string,
): { repoId?: string; authorUserId?: string } {
  // An explicit body repoId is authoritative. Otherwise infer from provenance:
  // the single repoId shared by EVERY source. Disagreement (or none) => base.
  let repoId = body.repoId;
  if (repoId === undefined) {
    const repoIds = new Set(
      idea.sources
        .map((s) => s.repoId)
        .filter((r): r is string => typeof r === 'string' && r !== ''),
    );
    if (repoIds.size === 1 && idea.sources.every((s) => typeof s.repoId === 'string' && s.repoId)) {
      repoId = [...repoIds][0];
    }
  }
  // A fork (repoId set) is authored by the body's author if given, else the
  // verified caller. The base variant (no repoId) carries no author.
  const authorUserId = body.authorUserId ?? (repoId ? callerUserId : undefined);
  return {
    ...(repoId !== undefined ? { repoId } : {}),
    ...(authorUserId !== undefined ? { authorUserId } : {}),
  };
}

/**
 * U18 — replay the skill's golden cases against the CANDIDATE revision body
 * being promoted, returning any REGRESSIONS (cases the candidate no longer
 * satisfies). ADVISORY: this never throws and never blocks the promote — a
 * replay error or a regression is surfaced to the human, who is the gate. The
 * candidate body is resolved from the revision row the promote points at (the
 * given `rev`, else the variant's latest). Returns the full per-case results so
 * the caller can both report regressions and confirm the clean cases.
 */
async function replayGoldenForPromote(
  deps: SkillsDeps,
  org: string,
  baseName: string,
  variantId: string,
  rev: number | undefined,
): Promise<GoldenReplayResult[]> {
  try {
    const cases = await deps.repo.listGoldenCasesForSkill(org, baseName);
    if (cases.length === 0) return [];
    const scope = orgScope(org);
    const variant = parseVariantId(baseName, variantId);
    // Resolve the candidate body: the named rev's snapshot, else the variant's
    // latest snapshot, and — as a final fallback — the LIVE "current" record (the
    // latest write, which is what a just-folded candidate is). The fallback keeps
    // the advisory replay robust even when a revision snapshot row is unavailable.
    let candidateBody = '';
    let revisionRow: Record<string, unknown> | undefined;
    if (rev !== undefined) {
      revisionRow = await deps.repo.getRevision(scope, 'SKILL', baseName, rev, variant);
    } else {
      const variants = await deps.repo.listVariants(scope, 'SKILL', baseName);
      revisionRow = variants.find((v) => (v.variantId as string) === variantId);
    }
    if (typeof revisionRow?.body === 'string') {
      candidateBody = revisionRow.body as string;
    } else {
      const live = await deps.repo.getSkill(scope, baseName);
      if (typeof live?.body === 'string') candidateBody = live.body;
    }
    const judge = deps.golden ?? getGoldenJudge();
    return await replayGoldenCases(judge, cases, candidateBody);
  } catch {
    // Advisory — a replay failure never blocks a promote nor leaks an error.
    return [];
  }
}

/**
 * POST /skills/:name/promote — repoint the org-wide TRUE variant for a baseName.
 * SKILL-EDIT gated (U16): admin-gated like `fold`/`createSkill` — promoting an
 * unreviewed variant to the org default is the same authority as writing the
 * revision itself. Body: `{ variantId, rev? }`. Promotion ONLY repoints TRUE; it
 * never edits or deletes a variant. Returns the new pointer.
 *
 * U18 — GOLDEN-SET REGRESSION (advisory): before repointing TRUE, the skill's
 * golden cases (one per prior fold) are REPLAYED against the candidate revision
 * body via an OpenRouter judge. Any case the candidate no longer satisfies is a
 * REGRESSION — the candidate appears to undo an earlier fold. v1 is advisory:
 * the human is the gate, so regressions are SURFACED on the response
 * (`goldenRegressions` + the full `goldenReplay`) but the promote still
 * succeeds. The replay never throws (an OpenRouter error just yields no findings).
 */
export async function promoteSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const gate = await requireOrgCatalogAdmin(event, deps.repo);
  if ('error' in gate) return gate.error;
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = gate.auth.org;

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const variantId = (body as { variantId?: unknown })?.variantId;
  if (typeof variantId !== 'string' || !variantId) return badRequest('missing variantId');
  const revRaw = (body as { rev?: unknown })?.rev;
  const rev = typeof revRaw === 'number' ? revRaw : undefined;

  // Replay the golden cases against the candidate BEFORE repointing TRUE so the
  // regression report reflects exactly what is about to become the org default.
  const goldenReplay = await replayGoldenForPromote(deps, org, name, variantId, rev);
  const goldenRegressions = goldenReplay.filter((r) => !r.satisfied);

  const pointer = { baseName: name, variantId, ...(rev !== undefined ? { rev } : {}) };
  await deps.repo.setTrueVariant(orgScope(org), 'SKILL', pointer);
  return ok({ true: pointer, goldenReplay, goldenRegressions });
}

/**
 * POST /skills/:name/ideas/:ideaId/fold — fold an idea into a NEW skill revision
 * (U16). `:name` is the skill base name; `:ideaId` is the idea on that family.
 *
 * Folding does NOT overwrite the skill. It SNAPSHOTS a new revision via the
 * existing `putNewVersion` machinery and leaves the org-wide `#TRUE` pointer
 * exactly where it was — a human promotes it separately (prior research: curated
 * folds beat blind overwrites). For a canonical `built-in`, an in-place revision
 * is rejected for the same reason `createSkill` rejects it: the base is git-seed
 * owned. The caller must FORK (supply `repoId` + `authorUserId`) so the fold lands
 * on a variant line; `putNewVersion` then mints rev 1 of that fork.
 *
 * SKILL-EDIT gated: same server-side admin as `createSkill`/`promoteSkill`. The
 * body carries the merged revision the human/agent drafted plus the fork identity:
 *   `{ body, description?, repoId?, authorUserId? }`.
 *
 * The idea is marked `folded` with `foldedIntoRev` AFTER the revision write, via
 * the optimistic-concurrency conditional update (`corroborateIdeaConditional`),
 * so a fold racing an incoming corroboration is safe: if a concurrent writer
 * advanced the idea's `corroborationVersion` between our read and our mark, the
 * conditional fails and we 409 rather than clobbering the new evidence — the fold
 * is retried against the fresh idea.
 */
export async function foldIdea(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const gate = await requireOrgCatalogAdmin(event, deps.repo);
  if ('error' in gate) return gate.error;
  const { principal, org } = gate.auth;

  const name = pathParam(event, 'name');
  const ideaId = pathParam(event, 'ideaId');
  if (!name || !ideaId) return badRequest('missing name or ideaId');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const b = (body ?? {}) as Record<string, unknown>;

  // The idea must exist on this skill family before we touch the skill revision —
  // we read it FIRST so the conditional mark below can guard on the version we saw.
  const idea = await deps.repo.getIdea(org, name, ideaId);
  if (!idea) return notFound();
  // Folding an already-folded idea is a no-op conflict (it is out of the live
  // block and its lesson is in the body); the caller decides what to do.
  if (idea.status === 'folded') {
    return conflict(`idea "${ideaId}" is already folded into rev ${idea.foldedIntoRev ?? '?'}`);
  }

  // Resolve the fold target (U19 — variant-scoped folding). The variant is
  // implied by the IDEA'S PROVENANCE (the `repoId` its sources agree on) when the
  // request body does not pick one explicitly; absent/conflicting provenance
  // targets the org base. The base variant has empty repo/author; a fork sets both
  // (the variant model). For a canonical built-in the base is git-seed owned, so an
  // in-place fold is rejected exactly like createSkill — the fold must fork, which
  // a repo-scoped idea does automatically via its provenance.
  const { repoId, authorUserId } = foldTargetVariant(
    idea,
    {
      repoId: typeof b.repoId === 'string' ? b.repoId : undefined,
      authorUserId: typeof b.authorUserId === 'string' ? b.authorUserId : undefined,
    },
    principal.userId,
  );

  const existing = await deps.repo.getSkill(orgScope(org), name);
  if (!existing) return notFound();
  if (isBuiltin(existing) && !repoId && !authorUserId) {
    return conflict(
      `"${name}" is a canonical built-in skill — fold into a fork (set repoId + authorUserId); ` +
        `an in-place fold is rejected so the git-seeded base is never overwritten.`,
    );
  }

  // The merged revision the human/agent drafted. Default to the existing skill's
  // body when the caller omits it (a fold that only records provenance), so the
  // new revision is never accidentally blanked.
  const newBody = typeof b.body === 'string' ? b.body : existing.body;
  const description = typeof b.description === 'string' ? b.description : existing.description;

  // U10 / MAT-141 Gap 1 — DRY single-writer contract. When the Python
  // author-revision writer is configured, the fold MUST author the new skill
  // revision (and its IDEAGOLD# golden case) through that ONE writer rather than
  // writing the revision itself via `repo.putNewVersion` + `repo.putGoldenCase`.
  // This is the whole point of U10: exactly one revision-author implementation
  // (Python). The TS handler only orchestrates (auth, idea-state, provenance) and
  // delegates the actual revision/golden write across the contract.
  const authorRevision =
    deps.authorRevision ?? (usesPythonWriter() ? callPythonAuthorRevision : undefined);

  // The variant id the Python writer keys the revision under (mirrors the TS
  // `variantInfix`: base variant id === baseName, a fork carries the repo/user
  // infix). For the org base the variantId is the empty string the Python writer
  // expects (it stores under `SKILL##r<N>`); a fork carries the variant infix.
  const stampedBaseName = existing.baseName ?? name;

  let stamped: Skill;
  if (authorRevision) {
    // --- ROUTE THROUGH THE ONE PYTHON WRITER (no TS revision write) ----------
    const isFork = repoId !== undefined || authorUserId !== undefined;
    const writerVariantId = isFork ? variantIdFor(stampedBaseName, repoId, authorUserId) : '';
    const req: AuthorRevisionRequest = {
      org,
      baseName: stampedBaseName,
      variantId: writerVariantId,
      body: newBody,
      ...(authorUserId !== undefined ? { authorUserId } : {}),
      ...(description !== undefined ? { description } : {}),
      ideaId,
      // Fold path → capture the before→after golden case via the SAME writer, so
      // there is one IDEAGOLD# capture path (Python), not a separate TS one.
      goldenCase: {
        caseId: ideaId,
        before: existing.body,
        after: newBody,
        ideaBody: idea.text,
      },
    };
    const written = await authorRevision(req);
    // The handler does NOT write the revision row, the live record, the TRUE
    // pointer, or the golden case — the Python writer owns all of those. We carry
    // the writer's `rev` forward as the version the idea was folded into.
    stamped = {
      ...existing,
      scope: orgScope(org),
      baseName: stampedBaseName,
      variantId: writerVariantId || stampedBaseName,
      body: newBody,
      description,
      version: written.rev,
      ...(repoId !== undefined ? { repoId } : {}),
      ...(authorUserId !== undefined ? { authorUserId } : {}),
    } as Skill;
  } else {
    // --- LEGACY LOCAL-DEV / TEST FALLBACK (no Python writer configured) -------
    // Carry the existing skill forward, overlay the merged content + variant
    // identity, and SNAPSHOT a new revision. `putNewVersion` writes the immutable
    // revision row + the live "current" record and leaves TRUE untouched (a human
    // promotes), forking the variant when repoId/authorUserId are set. This branch
    // exists only for local dev / tests where the Python writer is not wired; the
    // deployed Lambda always has `PYTHON_AUTHOR_REVISION_URL` set so the one
    // writer is authoritative.
    const candidate: Skill = {
      ...existing,
      scope: orgScope(org),
      baseName: stampedBaseName,
      body: newBody,
      description,
      ...(repoId !== undefined ? { repoId } : {}),
      ...(authorUserId !== undefined ? { authorUserId } : {}),
    };
    stamped = await deps.repo.putNewVersion('SKILL', candidate, { repoId, authorUserId });
  }

  // AFTER the revision write: mark the idea folded with the rev it was folded
  // into, via the optimistic-concurrency conditional. If a concurrent
  // corroboration advanced the version since our read, this fails → 409 (the
  // fold↔corroboration race is safe; the revision still exists, the human re-folds
  // against the freshly-corroborated idea).
  const foldedIdea: Idea = {
    ...idea,
    status: 'folded',
    foldedIntoRev: stamped.version as number,
    updatedAt: Date.now(),
  };
  const { written } = await deps.repo.corroborateIdeaConditional(
    foldedIdea,
    idea.corroborationVersion,
  );
  if (!written) {
    return conflict(
      `idea "${ideaId}" was corroborated concurrently with the fold — re-read and re-fold ` +
        `(the revision was written; the mark was not, to avoid clobbering the new evidence).`,
    );
  }

  // U18 — capture this fold as a before→after GOLDEN CASE co-located with the
  // skill (`IDEAGOLD#<baseName>#<ideaId>`). `before` is the body the fold
  // started from, `after` is the merged revision body, `lesson` is the
  // synthesized idea text the fold was meant to encode. A later promote replays
  // this case against the candidate revision so a fold that would undo this
  // lesson is flagged (advisory). The `caseId` is the `ideaId`, so re-folding
  // the same idea refreshes its case rather than duplicating it. The case is
  // best-effort: a failure here does NOT roll back the (already-marked) fold —
  // the revision + folded idea stand; the regression guard is advisory anyway.
  //
  // U10 / MAT-141 Gap 1: when the fold routed through the Python writer, the
  // golden case was captured by that SAME single writer (the `goldenCase` payload
  // above) — so the TS handler must NOT also write it here. Writing it in TS too
  // would re-introduce the duplicate write path U10 exists to remove. Only the
  // legacy local-dev/test fallback (no Python writer) captures the case in TS.
  if (!authorRevision) {
    try {
      const goldenCase: GoldenCase = {
        caseId: ideaId,
        skillBaseName: name,
        org,
        ideaId,
        lesson: idea.text,
        before: existing.body,
        after: newBody,
        foldedIntoRev: stamped.version as number,
        createdAt: Date.now(),
      };
      await deps.repo.putGoldenCase(goldenCase);
    } catch {
      // Advisory guard — never fail the fold on a golden-case write error.
    }
  }

  return ok({
    skill: stamped,
    idea: { ...foldedIdea, corroborationVersion: idea.corroborationVersion + 1 },
  });
}

/** Count the agents (in the org catalog) whose `skills[]` references a skill. */
async function usageCount(repo: Repo, org: string, name: string): Promise<number> {
  const agents = await repo.listAgents(org);
  return agents.filter((a) => a.skills.includes(name)).length;
}

export async function getUsage(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const gate = await requireOrgCatalogAuth(event, deps.repo);
  if ('error' in gate) return gate.error;
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!gate.auth.org) return ok({ name, count: 0 });
  const count = await usageCount(deps.repo, gate.auth.org, name);
  return ok({ name, count });
}

export async function deleteSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const gate = await requireOrgCatalogAdmin(event, deps.repo);
  if ('error' in gate) return gate.error;
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = gate.auth.org;
  const existing = await deps.repo.getSkill(orgScope(org), name);
  if (isBuiltin(existing)) {
    return conflict(
      `"${name}" is a canonical built-in skill — remove it from catalog/skills and ` +
        `re-seed; it cannot be deleted via REST.`,
    );
  }
  // U21 — skill delete/rename → idea orphan policy. Ideas key off
  // `baseName` + org (the base variant has `name === baseName`). BEFORE
  // dropping the skill row, cascade its ideas to the org's unassigned bin
  // (preserving corroboration + provenance) and delete the idea rows, so no
  // idea is left pointing at a now-nonexistent skill. Rename is delete+create
  // today, so this same cascade covers a rename: the old name's ideas land in
  // the bin and re-associate onto the new name on its next topic event.
  const baseName = existing?.baseName ?? name;
  await deps.repo.cascadeSkillIdeasToBin(org, baseName);

  await deps.repo.deleteSkill(orgScope(org), name);

  // Remove the skill's vector so the dead skill never matches a topic query.
  // The vector key is `<org>#<baseName>` in the fixed `skills` index (U2/U3).
  const vectors = deps.vectors ?? getS3Vectors();
  await vectors.deleteVectors(SKILL_VECTOR_INDEX, [skillVectorKey(org, baseName)]);

  return ok({ deleted: true });
}

const memberHandlers = makeBundleMemberHandlers<Skill>({
  get: (repo, scope, name) => repo.getSkill(scope, name),
  put: (repo, bundle) => repo.putSkill(bundle),
  del: (repo, scope, name) => repo.deleteSkill(scope, name),
  responseKey: 'skill',
  builtinLabel: 'bundle',
  seedPath: 'catalog/skills/bundles.json',
});

export const addMember = memberHandlers.addMember;
export const removeMember = memberHandlers.removeMember;
export const dissolveBundle = memberHandlers.dissolve;

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: SkillsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;

  // Fold an idea into a new revision (U16). Checked before /promote etc. since it
  // is the most specific POST suffix on the skills resource.
  if (method === 'POST' && path.endsWith('/fold')) return foldIdea(event, deps);
  if (method === 'POST' && path.endsWith('/members')) return addMember(event, deps);
  if (method === 'DELETE' && pathParam(event, 'member')) return removeMember(event, deps);
  if (method === 'POST' && path.endsWith('/dissolve')) return dissolveBundle(event, deps);
  if (method === 'GET' && path.endsWith('/usage')) return getUsage(event, deps);
  return handlers.dispatch(event, deps, { promote: promoteSkill, remove: deleteSkill });
}
