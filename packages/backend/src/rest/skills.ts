import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import {
  orgScope,
  skillSchema,
  type GoldenCase,
  type GoldenReplayResult,
  type Idea,
  type Skill,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  conflict,
  created,
  defaultRepo,
  forbidden,
  gone,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
  unauthorized,
} from './runtime.js';
import { isBuiltin, resolveOrgCatalogAuth } from './scopeauth.js';
import { effectiveOrg } from './membership.js';
import { resolvePrincipal } from './bearerAuth.js';
import { withAuthorNames } from './authorNames.js';
import { flattenBundle } from './bundles.js';
import {
  getS3Vectors,
  SKILL_VECTOR_INDEX,
  skillVectorKey,
  type S3Vectors,
} from '../embeddings/s3vectors.js';
import { getGoldenJudge, replayGoldenCases, type GoldenJudge } from '../rerank/golden.js';

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
 *   POST   /skills/:name/scope            — RETIRED (410 Gone): no tiers in the org catalog
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
}

export async function resolveSkills(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept EITHER the gateway Cognito JWT (HQ web) OR a raw bearer device token
  // (the claude+ wrapper) — this route is HttpNoneAuthorizer so the gateway does
  // not pre-reject the device token. Fall back to the device token's own org claim
  // when there are no gateway claims to drive effectiveOrg.
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
  if (!org) return ok({ skills: [] });

  // Pass the caller's userId so the merged org+user catalog is returned (a
  // user-scoped skill shadows an org-scoped one of the same name).
  const all = await deps.repo.listSkills(org, principal.userId);
  const byName = new Map(all.map((s) => [s.name, s]));
  // Annotate bundles with their transitively-resolved leaf members.
  const annotated = all.map((s) =>
    s.kind === 'bundle' ? { ...s, resolvedMembers: flattenBundle(s, byName) } : s,
  );
  // Show the author's real name (their email) instead of the raw Cognito sub that
  // claude+ device-token writes stamp into createdBy.name.
  return ok({ skills: await withAuthorNames(deps.repo, annotated) });
}

export async function createSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept the gateway Cognito JWT OR a raw device token (HttpNoneAuthorizer
  // route); admin is decided server-side from the profile so the device token
  // (no role claim) can write.
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  if (!auth.org) return unauthorized();
  const { principal, org } = auth;

  const name = pathParam(event, 'name');
  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');

  // Force org scope (ignore any client-supplied scope) and parse the rest.
  const candidate = { ...(body as Record<string, unknown>), scope: orgScope(org) };
  const parsed = skillSchema.safeParse(candidate);
  if (!parsed.success) return badRequest(parsed.error.message);
  const skill: Skill = parsed.data;

  // Canonical built-ins are owned by the git seed: reject an in-place write to the
  // BASE variant (no repo/author). Forking (repoId + authorUserId) is still allowed —
  // that is how a project customizes a built-in without touching the canonical.
  const targetName = name ?? skill.name;
  const existing = await deps.repo.getSkill(orgScope(org), targetName);
  if (isBuiltin(existing) && !skill.repoId && !skill.authorUserId) {
    return conflict(
      `"${targetName}" is a canonical built-in skill — fork it (set repoId + authorUserId) ` +
        `or change it in catalog/skills and re-seed; in-place writes are rejected.`,
    );
  }

  // Bundle-overwrite guard: a non-bundle skill write must NOT clobber an existing
  // `kind:bundle` of the same name. `claude+ sync` is bidirectional and upserts by
  // name, so a local skill dir sharing a bundle's name would otherwise be pushed
  // over the bundle and wipe its members (this destroyed a 35-member bundle once).
  // Refuse it server-side — the author must rename the local skill or the bundle.
  if (existing?.kind === 'bundle' && skill.kind !== 'bundle') {
    return conflict(
      `"${targetName}" is already a bundle in the org catalog — a skill push under the ` +
        `same name would clobber it and wipe its members. Rename the local skill (or the ` +
        `bundle) so their names don't collide.`,
    );
  }

  if (name) {
    // PUT /skills/:name — update; preserve the existing createdBy stamp.
    skill.createdBy = existing?.createdBy ?? skill.createdBy;
    // Carry the variant identity forward so an edit snapshots the NEXT revision
    // of the SAME variant rather than starting a new family at rev 1.
    skill.baseName = existing?.baseName ?? skill.baseName ?? name;
  } else {
    // POST — stamp authorship from the principal.
    skill.createdBy = { userId: principal.userId, name: principal.name ?? principal.userId };
    skill.baseName = skill.baseName ?? skill.name;
  }

  // VERSIONING (KTD6): every create/update SNAPSHOTS an immutable revision and
  // upserts the live record, forking/advancing the variant `(baseName, repoId,
  // person)` instead of clobbering. The base (org-seeded) variant has empty
  // repo/author. `putNewVersion` also writes the live record under `skillKey`,
  // so the existing read path is unchanged.
  const stamped = await deps.repo.putNewVersion('SKILL', skill, {
    repoId: skill.repoId,
    authorUserId: skill.authorUserId,
  });
  return name ? ok({ skill: stamped }) : created({ skill: stamped });
}

/**
 * Parse a `variantId` infix back into `{ repoId, userId }`. The base variant's
 * id is just the `baseName` (→ no repo/user); a fork is `<baseName>#R#<repoId>#U#<userId>`
 * (mirrors `variantIdFor` / `variantInfix`). Returns `{}` for the base.
 */
function parseVariantId(baseName: string, variantId: string): { repoId?: string; userId?: string } {
  if (variantId === baseName) return {};
  const m = new RegExp(`^${baseName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}#R#(.*)#U#(.*)$`).exec(
    variantId,
  );
  if (!m) return {};
  return {
    repoId: m[1] ? m[1] : undefined,
    userId: m[2] ? m[2] : undefined,
  };
}

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
      idea.sources.map((s) => s.repoId).filter((r): r is string => typeof r === 'string' && r !== ''),
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
 * SKILL-EDIT gated (U16): promoting an unreviewed variant to the org default is
 * the same authority as writing the revision itself, so it requires the same
 * server-side admin as `fold`/`createSkill` (reconciling the pre-U16 split that
 * left promote open to any member). The device-token caller's admin status is
 * resolved server-side from the PROFILE by the shared resolver. Body:
 * `{ variantId, rev? }`. Promotion ONLY repoints TRUE; it never edits or deletes
 * a variant. Returns the new pointer.
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
  // Promote is now admin-gated (skill-edit permission), accepting the device
  // token via the shared resolver; admin is decided server-side from the profile.
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return unauthorized();
  const org = auth.org;

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
  // Same skill-edit authority as createSkill/promoteSkill: a verified caller who
  // is admin of the org (decided server-side from the profile, so the device
  // token works). Non-admin → 403; no principal → 401.
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  if (!auth.org) return unauthorized();
  const { principal, org } = auth;

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
  const description =
    typeof b.description === 'string' ? b.description : existing.description;

  // Carry the existing skill forward, overlay the merged content + variant
  // identity, and SNAPSHOT a new revision. `putNewVersion` writes the immutable
  // revision row + the live "current" record and leaves TRUE untouched (a human
  // promotes), forking the variant when repoId/authorUserId are set.
  const candidate: Skill = {
    ...existing,
    scope: orgScope(org),
    baseName: existing.baseName ?? name,
    body: newBody,
    description,
    ...(repoId !== undefined ? { repoId } : {}),
    ...(authorUserId !== undefined ? { authorUserId } : {}),
  };
  const stamped = await deps.repo.putNewVersion('SKILL', candidate, { repoId, authorUserId });

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

  return ok({ skill: stamped, idea: { ...foldedIdea, corroborationVersion: idea.corroborationVersion + 1 } });
}

export async function getSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return notFound();
  const skill = await deps.repo.getSkill(orgScope(auth.org), name);
  if (!skill) return notFound();
  const [enriched] = await withAuthorNames(deps.repo, [skill]);
  return ok({ skill: enriched });
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
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return ok({ name, count: 0 });
  const count = await usageCount(deps.repo, auth.org, name);
  return ok({ name, count });
}

export async function deleteSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return unauthorized();
  const existing = await deps.repo.getSkill(orgScope(auth.org), name);
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
  await deps.repo.cascadeSkillIdeasToBin(auth.org, baseName);

  await deps.repo.deleteSkill(orgScope(auth.org), name);

  // Remove the skill's vector so the dead skill never matches a topic query.
  // The vector key is `<org>#<baseName>` in the fixed `skills` index (U2/U3).
  const vectors = deps.vectors ?? getS3Vectors();
  await vectors.deleteVectors(SKILL_VECTOR_INDEX, [skillVectorKey(auth.org, baseName)]);

  return ok({ deleted: true });
}

/** Add a member ref to a bundle. The member may be a skill or another bundle. */
export async function addMember(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const member = (body as { member?: unknown })?.member;
  if (typeof member !== 'string' || !member) return badRequest('missing member');

  if (!auth.org) return unauthorized();
  const bundle = await deps.repo.getSkill(orgScope(auth.org), name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');
  if (isBuiltin(bundle)) {
    return conflict(
      `"${name}" is a canonical built-in bundle — change its members in ` +
        `catalog/skills/bundles.json and re-seed; in-place edits are rejected.`,
    );
  }

  if (!bundle.members.includes(member)) {
    bundle.members = [...bundle.members, member];
    await deps.repo.putSkill(bundle);
  }
  return ok({ skill: bundle });
}

/**
 * Remove/eject a member from a bundle. The member skill record is left intact —
 * "eject" simply means it is no longer in the bundle but still exists standalone.
 */
export async function removeMember(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  const member = pathParam(event, 'member');
  if (!name || !member) return badRequest('missing name or member');

  if (!auth.org) return unauthorized();
  const bundle = await deps.repo.getSkill(orgScope(auth.org), name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');
  if (isBuiltin(bundle)) {
    return conflict(
      `"${name}" is a canonical built-in bundle — change its members in ` +
        `catalog/skills/bundles.json and re-seed; in-place edits are rejected.`,
    );
  }

  bundle.members = bundle.members.filter((m) => m !== member);
  await deps.repo.putSkill(bundle);
  return ok({ skill: bundle });
}

/**
 * Dissolve a bundle: its members all remain as standalone skills (they already
 * exist as their own records), and the bundle record itself is deleted.
 */
export async function dissolveBundle(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  if (!auth.org) return unauthorized();
  const bundle = await deps.repo.getSkill(orgScope(auth.org), name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');
  if (isBuiltin(bundle)) {
    return conflict(
      `"${name}" is a canonical built-in bundle — change its members in ` +
        `catalog/skills/bundles.json and re-seed; in-place edits are rejected.`,
    );
  }

  const members = bundle.members;
  await deps.repo.deleteSkill(orgScope(auth.org), name);
  return ok({ dissolved: true, members });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: SkillsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;
  const name = pathParam(event, 'name');

  // The scope-change endpoint is retired in the org-only catalog.
  if (method === 'POST' && path.endsWith('/scope')) return gone('scope changes are retired');
  // Fold an idea into a new revision (U16). Checked before /promote etc. since it
  // is the most specific POST suffix on the skills resource.
  if (method === 'POST' && path.endsWith('/fold')) return foldIdea(event, deps);
  if (method === 'POST' && path.endsWith('/promote')) return promoteSkill(event, deps);
  if (method === 'POST' && path.endsWith('/members')) return addMember(event, deps);
  if (method === 'DELETE' && pathParam(event, 'member')) return removeMember(event, deps);
  if (method === 'POST' && path.endsWith('/dissolve')) return dissolveBundle(event, deps);
  if (method === 'GET' && path.endsWith('/usage')) return getUsage(event, deps);
  if (method === 'POST') return createSkill(event, deps);
  if (method === 'PUT') return createSkill(event, deps);
  if (method === 'DELETE') return deleteSkill(event, deps);
  if (method === 'GET' && name) return getSkill(event, deps);
  return resolveSkills(event, deps);
}
