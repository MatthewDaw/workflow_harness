export const meta = {
  name: 'org-membership',
  description: 'Add create/join-organization onboarding + org-scoped data to Command HQ',
  phases: [
    { title: 'Foundation', detail: 'shared DTOs, keys, password hashing, repo methods' },
    { title: 'Build', detail: 'backend endpoints | frontend OrgGate | documentation' },
    { title: 'Seed scope', detail: 'seed only command-hq-starter at org scope; narrow the other three' },
    { title: 'Verify', detail: 'typecheck + tests across workspaces, repair failures' },
  ],
};

const ROOT = 'C:/Users/mattd/Documents/gauntlet/workflow_harness';

// Shared context handed to every agent so they share one mental model.
const CONTEXT = `
You are implementing ONE feature in the Command HQ monorepo at ${ROOT}.

FEATURE (from the user, verbatim intent):
"When a user signs in or creates an account, if they are not in an organization they
must either select/join an existing organization or create one. Creating an org
requires a name AND a password. Joining an org requires the user to type an existing
org's name EXACTLY and its password. All data is then scoped to the org the user is in.
The database (skills, agents, etc.) must be easily partitionable by org id AND by user id."

KEY ARCHITECTURAL DECISION (already made — implement it, do not redesign):
- Today the user's org comes from the Cognito token claim 'custom:org' (auto-assigned),
  so every user always "has" an org. We are changing membership to be DB-DRIVEN via the
  user PROFILE record so a user can genuinely have NO org and be forced to onboard.
- Source of truth for membership = the PROFILE record \`USER#<userId> / PROFILE\`, field \`org\`.
- After login the web calls \`GET /me\`; if \`org\` is null the OrgGate forces create/join.
- Backend data handlers resolve the EFFECTIVE org via \`effectiveOrg(event, repo)\` =
  \`profile.org ?? token-claim-org\`. The token fallback is deliberate: it keeps existing
  handler tests (which seed no profile) passing while real onboarded users get their
  chosen org.

SINGLE-TABLE DynamoDB key facts (packages/backend/src/db/keys.ts):
- userKey(userId) -> { PK: 'USER#'+userId, SK: 'PROFILE' }  (already exists)
- The org partition 'ORG#<org>' already holds objectives (SK 'RCDO#...') and the DoD
  (SK 'CONFIG#DOD'). The org record will be SK 'META'.
- scopeId(scope) builds 'org#<id>' | 'user#<id>' | 'proj#<id>'; agentKey/skillKey use
  PK 'SCOPE#'+scopeId(scope). So skills/agents ALREADY partition by org AND user — we
  just need repo queries + schema that allow the user tier.

CONVENTIONS:
- TypeScript ESM: imports use the '.js' extension even for .ts files.
- DTOs/zod schemas live in packages/shared/src/dto.ts and are re-exported via
  packages/shared/src/index.ts (export * from './dto.js').
- Repo (packages/backend/src/db/repo.ts) is the ONLY DB access layer; handlers depend on it.
- REST handlers return via helpers in packages/backend/src/rest/runtime.ts:
  ok/created/badRequest/unauthorized/forbidden/notFound/json, principalOf(event),
  parseBody(event), pathParam, queryParam. isAdmin(event) lives in rest/scopeauth.ts.
- Do NOT use Date.now() is fine in app code (only the workflow ENGINE forbids it; your
  edited source files may use Date.now()/new Date() normally — repo.ts already does).
- Match the surrounding code's style, comment density, and naming exactly. These files
  are heavily commented; write comments that explain WHY, like the neighbors.
`;

// ----------------------------------------------------------------------------
phase('Foundation');

const foundationSummary = await agent(
  `${CONTEXT}

YOUR PHASE: Foundation. You own the shared types, key builders, password hashing, and
Repo methods. Other agents build on top of what you write, so your signatures must be
exact and your code must typecheck.

Read these first to match style/shape:
- packages/shared/src/dto.ts  (zod DTO conventions)
- packages/shared/src/scope.ts (scopeRefSchema, orgScopeRefSchema, resolveScoped, ScopeContext)
- packages/shared/src/index.ts
- packages/backend/src/db/keys.ts
- packages/backend/src/db/repo.ts  (Repo class; note getProject parses through a schema)

DO THE FOLLOWING:

1) packages/shared/src/dto.ts — ADD these zod schemas + exported types (place them near the
   other small DTOs; mirror the commenting style):
   - userProfileSchema = z.object({ userId: z.string().min(1), name: z.string().optional(),
       org: z.string().optional(), admin: z.boolean().optional() }); type UserProfile.
   - orgSchema (the PUBLIC org record returned to clients — NO password fields):
       z.object({ name: z.string().min(1), createdBy: z.string().min(1),
       createdAt: z.number().int().nonnegative() }); type Org.
   - meResponseSchema = z.object({ userId: z.string().min(1), name: z.string().optional(),
       org: z.string().nullable(), admin: z.boolean().optional() }); type MeResponse.
   - Request validation schemas:
       orgNameSchema = z.string().trim().min(1).max(64);
       orgPasswordSchema = z.string().min(6).max(200);
       createOrgRequestSchema = z.object({ name: orgNameSchema, password: orgPasswordSchema });
       joinOrgRequestSchema = z.object({ name: orgNameSchema, password: orgPasswordSchema });
       (export the inferred types too.)

2) packages/shared/src/scope.ts — ADD a convenience constructor next to orgScope():
       export function userScope(userId: string): ScopeRef { return { tier: 'user', id: userId }; }
   (Return type ScopeRef, NOT a literal-narrowed type.)

3) packages/shared/src/dto.ts — RELAX the catalog scope so agents/skills can live at the
   user tier as well as org (this is the "partition by org AND user" requirement):
   change \`agentSchema\`'s \`scope: orgScopeRefSchema\` and \`skillSchema\`'s
   \`scope: orgScopeRefSchema\` to \`scope: scopeRefSchema\`. (scopeRefSchema is a strict
   superset, so every existing org-scoped record still validates.) Update the import of
   scopeRefSchema (it is already imported). Update the inline comments that say
   "tier is always 'org'" to note that org is the default but user-scoped catalog items
   are also representable.

4) packages/backend/src/db/keys.ts — ADD:
       export const orgKey = (name: string): PrimaryKey => ({ PK: \`ORG#\${name}\`, SK: 'META' });
   Place it near objectiveKey/orgDodKey with a short comment noting it shares the org
   partition with objectives + DoD.

5) NEW FILE packages/backend/src/auth/orgPassword.ts — scrypt-based hashing using node:crypto
   (NO new dependencies). Export:
       hashOrgPassword(password: string): { salt: string; hash: string }
         -> random 16-byte salt (hex), scryptSync(password, salt, 64) hex.
       verifyOrgPassword(password: string, salt: string, hash: string): boolean
         -> recompute scryptSync, compare with crypto.timingSafeEqual on equal-length
            Buffers (guard length first to avoid timingSafeEqual throwing).
   Use \`import { randomBytes, scryptSync, timingSafeEqual } from 'node:crypto';\`.
   Comment WHY scrypt + timing-safe compare.

6) packages/backend/src/db/repo.ts — ADD methods to the Repo class. The stored ORG item
   carries password fields that must NEVER be returned to clients, so define a private
   internal shape. Add:

   // --- Users (profiles) + org membership ---
   - async getUser(userId: string): Promise<UserProfile | undefined>
       GetCommand on k.userKey(userId); parse through userProfileSchema.safeParse (fall back
       to raw on failure, like getProject); return undefined when no Item.
   - async putUser(profile: UserProfile): Promise<void>
       PutCommand Item: { ...k.userKey(profile.userId), ...profile }.
   - async setUserOrg(userId: string, org: string, opts?: { name?: string; admin?: boolean }):
       Promise<void>
       Load existing profile (getUser), merge { userId, org, name: opts.name ?? existing.name,
       admin: opts.admin ?? existing.admin }, then putUser. (Upsert.)

   // --- Organizations ---
   Internal stored shape (define an interface OrgRecord in repo.ts, NOT exported via shared):
       interface OrgRecord { name: string; createdBy: string; createdAt: number;
         passwordSalt: string; passwordHash: string; }
   - async getOrg(name: string): Promise<OrgRecord | undefined>
       GetCommand on k.orgKey(name); return res.Item as OrgRecord | undefined.
   - async createOrg(rec: OrgRecord): Promise<{ created: boolean }>
       PutCommand Item: { ...k.orgKey(rec.name), ...rec } with
       ConditionExpression 'attribute_not_exists(PK)'. Catch ConditionalCheckFailedException
       -> { created: false } (mirror appendEvent's catch); else { created: true }.

   // --- Skills/agents: org AND user partitions ---
   Make the catalog queryable by user scope too, and let the catalog endpoints merge:
   - Change \`listAgents(org)\` and \`listSkills(org)\` to accept an OPTIONAL userId:
       async listAgents(org: string, userId?: string): Promise<Agent[]>
       async listSkills(org: string, userId?: string): Promise<Skill[]>
     When userId is undefined, behave EXACTLY as today (org-only — keep back-compat for all
     existing callers/tests). When userId is provided, query BOTH the org scope and the
     user scope (k.scopePartition(orgScope(org)) and k.scopePartition(userScope(userId)))
     and merge with resolveScoped(items, { org, userId }) imported from '@harness/shared'
     so a user-scoped item shadows an org-scoped one of the same name. Reuse the existing
     private listScoped helper; import userScope + resolveScoped from '@harness/shared'.

   Add intent comments to each new method matching the file's voice.

AFTER EDITING: run these and FIX any failures you introduced before returning:
   cd ${ROOT} && npm run build -w @harness/shared
   cd ${ROOT} && npm run typecheck -w @harness/backend
   cd ${ROOT} && npm test -w @harness/backend
(If a pre-existing test was already failing before your change, note it but do not chase it.)

RETURN a concise but COMPLETE summary listing every new/changed symbol with its exact
signature and file path (other agents depend on this): the new shared types/schemas, the
orgKey signature, the orgPassword function signatures, and every new Repo method signature.
Also state the exact ORG and PROFILE item shapes you wrote.`,
  { label: 'foundation', phase: 'Foundation' },
);

log('Foundation complete — fanning out backend, frontend, and docs.');

// ----------------------------------------------------------------------------
phase('Build');

const API_CONTRACT = `
REST CONTRACT (frozen — backend implements, frontend consumes):
- GET /me
    -> 200 { userId: string, name?: string, org: string | null, admin?: boolean }
    org is null when the caller has no membership (profile.org unset). NEVER falls back to
    the token claim for THIS endpoint (gating must reflect real membership). 401 if no principal.
- POST /orgs            body { name: string, password: string }
    Creates a new org. 201 { org: string, admin: true } on success.
    409 { error: 'organization already exists' } when the name is taken.
    400 { error: <validation message> } when name/password invalid (name 1..64, password >= 6).
    Creator becomes admin of the org and their profile.org is set to the new name.
- POST /orgs/join       body { name: string, password: string }
    Joins an existing org. Name must match EXACTLY (after trim); password verified.
    200 { org: string, admin: boolean } on success (sets caller profile.org = name).
    403 { error: 'invalid organization name or password' } on missing org OR wrong password
    (one generic message — do NOT reveal which was wrong; avoids org enumeration).
    400 on validation failure.
All three are authenticated like the other REST handlers (principalOf(event) reads the
gateway JWT claims; in local dev the devServer puts synthetic claims on the event).
`;

await parallel([
  // --- 2a: backend endpoints + wiring -------------------------------------
  () =>
    agent(
      `${CONTEXT}

FOUNDATION (already implemented by another agent — these symbols now EXIST; read the files
to confirm exact signatures):
${foundationSummary}

${API_CONTRACT}

YOUR PHASE: Backend endpoints + wiring. Implement the REST surface and thread the effective
org through the org-scoped handlers. Read first: rest/runtime.ts, rest/objectives.ts,
rest/dod.ts, rest/agents.ts, rest/skills.ts, rest/scopeauth.ts, local/devServer.ts, and an
existing handler test (e.g. test/objectives.test.ts or test/repo.test.ts) for test style.

DO THE FOLLOWING:

1) NEW FILE packages/backend/src/rest/membership.ts — export:
     export async function effectiveOrg(event, repo): Promise<string | undefined>
       const p = principalOf(event); if (!p) return undefined;
       const profile = await repo.getUser(p.userId);
       return profile?.org ?? p.org;
   Comment WHY the token fallback exists (back-compat + device tokens carry a member's org).

2) NEW FILE packages/backend/src/rest/orgs.ts — a handler module like objectives.ts with an
   exported \`handler(event)\` plus testable inner functions (getMe, createOrg, joinOrg) that
   take (event, deps) where deps = { repo }. Wire defaultRepo() in handler().
   - getMe: principal = principalOf(event); 401 if absent. profile = await repo.getUser(userId).
       Return ok({ userId, name: principal.name ?? profile?.name, org: profile?.org ?? null,
       admin: isAdmin(event) || profile?.admin === true }). (org from profile ONLY — no token fallback.)
   - createOrg: principal or 401. parse body; validate with createOrgRequestSchema (400 on fail).
       hash the password (hashOrgPassword). repo.createOrg({ name, createdBy: principal.userId,
       createdAt: Date.now(), passwordSalt, passwordHash }). If { created:false } -> 409
       { error: 'organization already exists' }. On success:
       await repo.setUserOrg(principal.userId, name, { name: principal.name, admin: true });
       return created({ org: name, admin: true }).
   - joinOrg: principal or 401. parse + validate with joinOrgRequestSchema (400).
       org = await repo.getOrg(name). If !org OR !verifyOrgPassword(password, org.passwordSalt,
       org.passwordHash) -> forbidden with { error: 'invalid organization name or password' }
       (use json(403, {...}) so the message is exact). On success:
       const existing = await repo.getUser(principal.userId);
       await repo.setUserOrg(principal.userId, name, { name: principal.name });
       return ok({ org: name, admin: existing?.admin === true }).
   - handler(event): route by method + path. POST /orgs/join -> joinOrg; POST /orgs -> createOrg;
       GET /me -> getMe. (You can distinguish join by event.rawPath / requestContext.http.path
       ending in '/join', mirroring how devServer routes are matched.)

3) Thread effectiveOrg through the ORG-SCOPED handlers so data follows membership. In EACH of
   rest/objectives.ts, rest/dod.ts, rest/agents.ts, rest/skills.ts: wherever the handler
   currently scopes by \`principal.org\`, replace that org value with
   \`await effectiveOrg(event, deps.repo)\` (keep principalOf for userId/admin/401). Guard:
   if effectiveOrg returns undefined, treat as unauthorized()/empty as appropriate. Keep the
   admin checks unchanged. For the catalog LIST reads (agents.ts list, skills.ts list), pass
   the caller's userId so the merged org+user catalog is returned:
   repo.listAgents(org, principal.userId) / repo.listSkills(org, principal.userId).
   IMPORTANT: do not change behavior when no profile exists — effectiveOrg already falls back
   to the claim org, so existing tests keep passing.

4) Stamp org onto newly created projects for future org partitioning. In rest/projects.ts
   createProject, set the new project's \`org\` to await effectiveOrg(event, repo) if the
   Project schema has/【add】 an optional \`org\` field. ADD \`org: z.string().optional()\` to
   projectSchema in packages/shared/src/dto.ts if not present, and persist it. (Listing stays
   by owner — do not change listProjectsForUser.) Keep this minimal.

5) local/devServer.ts — register the new routes so \`npm run dev\` serves them. Import
   \`{ handler as orgsHandler } from '../rest/orgs.js'\` and add routes (place BEFORE the
   generic catch-alls; order matters):
     { re: /^\\/me$/, handler: orgsHandler },
     { re: /^\\/orgs\\/join$/, handler: orgsHandler },
     { re: /^\\/orgs$/, handler: orgsHandler },
   Note: the offline mockClaims still returns org 'dev-org'; that is fine — getMe reads the
   PROFILE (empty on a fresh start) so a new dev user correctly sees org:null and the OrgGate.

6) infra: if packages or infra define the API Gateway route table (look for
   infra/lib/api-stack.ts), add GET /me, POST /orgs, POST /orgs/join mirroring how
   /objectives and /dod routes are declared (same authorizer). If you cannot find a clear
   pattern, skip and note it — do not guess CDK APIs.

7) TESTS: add packages/backend/test/orgs.test.ts covering: getMe returns org:null with no
   profile; createOrg creates + makes caller admin + sets profile (second create of same name
   -> 409); joinOrg with correct password joins, wrong password -> 403 generic message,
   unknown org -> 403; validation 400s. Use the same in-memory Repo/mock pattern the existing
   handler tests use (read one first). Also add a small membership.test.ts if natural.

AFTER EDITING run and FIX failures you introduced:
   cd ${ROOT} && npm run build -w @harness/shared && npm run typecheck -w @harness/backend && npm test -w @harness/backend
RETURN: list of files changed + the final route wiring + any infra step you skipped and why.`,
      { label: 'backend-endpoints', phase: 'Build' },
    ),

  // --- 2b: frontend OrgGate + onboarding ----------------------------------
  () =>
    agent(
      `${CONTEXT}

${API_CONTRACT}

YOUR PHASE: Frontend. Build the post-login OrgGate that forces create/join when the user has
no org, and the API hooks behind it. You depend ONLY on the REST contract above (define your
own response interface locally — do NOT block on shared types).

Read first: packages/web/src/api/baseApi.ts (RTK Query patterns, prepareHeaders, tagTypes,
unwrapOne/unwrapArray, the exported hooks block), packages/web/src/app/App.tsx,
packages/web/src/auth/AuthProvider.tsx, packages/web/src/auth/LoginGate.tsx (style for the
gate + form), packages/web/src/test/loginGate.test.tsx, and packages/web/src/test/testUtils.ts
(the installFetchStub helper — READ IT so you know how the fetch stub maps URLs to responses).

DO THE FOLLOWING:

1) baseApi.ts — add a 'Me' tag to tagTypes and three endpoints:
   - Local interface (declared in baseApi.ts like ProjectDoc):
       export interface Me { userId: string; name?: string; org: string | null; admin?: boolean }
   - getMe: build.query<Me, void>({ query: () => 'me', providesTags: ['Me'] })
       (the /me response is a bare object; unwrapOne<Me>('me') is safe if you want symmetry).
   - createOrg: build.mutation<{ org: string; admin: boolean }, { name: string; password: string }>(
       { query: (body) => ({ url: 'orgs', method: 'POST', body }), invalidatesTags: ['Me'] }).
   - joinOrg: build.mutation<{ org: string; admin: boolean }, { name: string; password: string }>(
       { query: (body) => ({ url: 'orgs/join', method: 'POST', body }), invalidatesTags: ['Me'] }).
   Export the hooks (useGetMeQuery, useCreateOrgMutation, useJoinOrgMutation) in the hooks block.
   Because switching org changes EVERY org-scoped dataset, make create/join also reset the cache:
   in each mutation's onQueryStarted, after \`await queryFulfilled\`, dispatch
   \`baseApi.util.invalidateTags(['Me','Objective','Agent','Skill','Project','Session','Weekly','Dod'])\`
   (or resetApiState) so the app reloads under the new org.

2) NEW FILE packages/web/src/auth/OrgGate.tsx — a gate component like LoginGate:
   export function OrgGate({ children }: { children: ReactNode }).
   - const { data, isLoading } = useGetMeQuery();
   - While isLoading -> a centered "Loading…" status (copy LoginGate's loading block).
   - If data?.org -> render <>{children}</>.
   - Else render the onboarding screen: a card (reuse the hq-frame / hq-btn classes and the
     Wordmark from '../components/Emblem.js' exactly like LoginGate) with TWO modes toggled by
     local state, e.g. a segmented control / two tabs: "Create organization" and
     "Join organization".
       * Create: inputs for Organization name + Password; submit calls useCreateOrgMutation.
         On success the 'Me' invalidation reloads /me and the gate flips automatically.
         Surface a 409 ("organization already exists") and 400 messages via a role="alert".
       * Join: inputs for Organization name + Password with helper text
         "Type the organization's exact name and password." submit calls useJoinOrgMutation;
         on 403 show "Invalid organization name or password." in role="alert".
   - Disable the submit button while the mutation isLoading; show "Creating…"/"Joining…".
   - Give the form an aria-label ("Set up your organization") and the inputs proper labels
     (Organization name / Password) with htmlFor, so tests can find them by label.
   - Keep the visual language consistent with LoginGate (same Tailwind utility classes:
     grid min-h-screen place-items-center bg-bg, hq-frame w-[340px] p-6, etc.).

3) App.tsx — insert OrgGate BETWEEN LoginGate and the router so it only mounts when
   authenticated:  <LoginGate><OrgGate>{wrapRouter(<AppRoutes />)}</OrgGate></LoginGate>.
   Import OrgGate from '../auth/OrgGate.js'.

4) testUtils.ts — the existing installFetchStub must now answer GET /me or the existing
   loginGate tests (which expect to land on Objectives after sign-in) will get stuck on the
   OrgGate. Update installFetchStub so that, by DEFAULT, a request to a URL ending in '/me'
   resolves to a MEMBER ({ userId: 'dev', name: 'Dev', org: 'gmail.com', admin: true }),
   while still allowing per-test overrides (keep its existing override mechanism — read the
   current signature and extend it minimally/back-compatibly). If installFetchStub maps paths
   to bodies, add a '/me' default entry; ensure unknown paths still behave as before.

5) loginGate.test.tsx — verify it still passes with the OrgGate in the tree (the /me default
   member should let it reach Objectives). Adjust ONLY if necessary (prefer fixing via the
   testUtils default over editing each test).

6) NEW FILE packages/web/src/test/orgGate.test.tsx — mirror loginGate.test.tsx's harness
   (App with makeStore(), createMockClient(null), MemoryRouter). Cases:
   - After signing in, when /me returns { org: null }, the onboarding form
     ("Set up your organization") is shown and Objectives is NOT.
   - Filling Create org name+password and submitting, with the fetch stub answering
     POST /orgs success AND then /me returning a member, flips to Objectives
     (data-testid 'objectives-screen').
   - Join tab with a 403 from POST /orgs/join shows "Invalid organization name or password".
   Drive /me + /orgs + /orgs/join responses through installFetchStub (extend it if needed to
   let a test script sequential/per-URL responses — keep it simple, e.g. allow a function or a
   queued response for /me so it returns null first then a member after create).

AFTER EDITING run and FIX failures you introduced:
   cd ${ROOT} && npm run build -w @harness/shared && npm run typecheck -w @harness/web && npm test -w @harness/web
RETURN: files changed, the final installFetchStub change (so the verify agent understands it),
and any test you had to adjust.`,
      { label: 'frontend-orggate', phase: 'Build' },
    ),

  // --- 2c: documentation --------------------------------------------------
  () =>
    agent(
      `${CONTEXT}

YOUR PHASE: Documentation. Docs in this repo are hand-authored .html with YAML frontmatter
(NOT generated from markdown). Read these to match format exactly:
- docs/plans/features/agentforge.html  (representative feature doc: frontmatter + <h1>/<h2>/
  <ul>/<blockquote> body; "Problem frame", "Key decisions", "Actors" sections)
- docs/plans/specs_overview.html  (the canonical top-level map; you will add a reference)
- docs/plans/features/platform_architecture.html  (data model + auth model — KTDs; update it)
- docs/PRD.html  (light touch if it enumerates auth/onboarding)

DO THE FOLLOWING:

1) NEW FILE docs/plans/features/organization_management.html — a full feature doc with
   frontmatter:
     ---
     status: active
     type: feature
     created: 2026-06-06
     completion: 90
     feature: organization-management
     ---
   Body (<h1> title, then <h2> sections) covering:
   - Problem frame: org was a fixed auto-assigned Cognito claim, so no user was ever
     "without an org"; we make membership DB-driven so users genuinely onboard.
   - Key decisions (mirror the bullet style of agentforge.html, <strong>lead-in.</strong> ...):
       * Membership lives on the PROFILE record (USER#<uid>/PROFILE.org), not the token claim.
       * GET /me drives the OrgGate; org:null forces create/join before the app renders.
       * Create org = name + password (scrypt-hashed, ORG#<name>/META, conditional create,
         409 on collision); creator becomes org admin.
       * Join org = EXACT name + password; one generic 403 on any mismatch (no enumeration).
       * effectiveOrg(event, repo) = profile.org ?? token-claim-org threads the chosen org
         through every org-scoped handler; the fallback preserves back-compat + device tokens.
       * Data partitions: ORG#<org> (objectives, DoD, org record) AND SCOPE#org#<id> /
         SCOPE#user#<id> for the skills/agents catalog — partitionable by org id AND user id.
   - Actors: new user (no org), org creator (becomes admin), joining user, org admin.
   - User flow: sign in -> GET /me -> (org null) OrgGate create/join -> profile.org set ->
     app renders org-scoped.
   - Data model: show the ORG#<name>/META record { name, createdBy, createdAt, passwordSalt,
     passwordHash } and the PROFILE { userId, name?, org?, admin? }; note password fields are
     never returned to clients.
   - REST contract: GET /me, POST /orgs, POST /orgs/join with the exact response/status shapes:
       GET /me -> { userId, name?, org: string|null, admin? }
       POST /orgs {name,password} -> 201 {org,admin:true} | 409 | 400
       POST /orgs/join {name,password} -> 200 {org,admin} | 403 'invalid organization name or password' | 400

2) docs/plans/specs_overview.html — add a reference/list entry pointing to
   ./features/organization_management.html, placed naturally among the other feature links
   (it is a foundational auth/onboarding feature — present it as a prerequisite). Match the
   surrounding markup; do not reflow unrelated content.

3) docs/plans/features/platform_architecture.html — update the auth-model and data-model
   sections so they reflect DB-driven membership: org is now chosen via create/join (not only
   the auto-assigned 'custom:org' claim), the ORG#<org>/META record exists alongside
   objectives + DoD, the PROFILE carries org membership, and effectiveOrg scopes data by the
   chosen org. Keep edits surgical and consistent with the existing KTD voice.

4) docs/PRD.html — only if it describes the auth/onboarding/sign-in flow, add a sentence that
   users select or create an organization (name + password) on first sign-in and all data is
   org-scoped. If PRD.html does not cover this area, leave it unchanged and say so.

These are .html files: write valid HTML matching the neighbors (entity-escape apostrophes as
the existing docs do, e.g. &#39;). Do not run a build (docs are static).
RETURN: the list of doc files created/updated and a one-line note on each.`,
      { label: 'docs', phase: 'Build' },
    ),
]);

log('Build phase complete — scoping the default seed catalog.');

// ----------------------------------------------------------------------------
phase('Seed scope');

const seedSummary = await agent(
  `${CONTEXT}

FOUNDATION NOTE: another agent already relaxed the catalog scope schema so skills/agents may
live at the USER tier as well as ORG (agentSchema/skillSchema \`scope\` is now scopeRefSchema,
and there is a \`userScope(userId)\` helper in @harness/shared). That is the mechanism you can
use to scope a skill narrower than the org default.

YOUR PHASE: Scope non-Command-HQ skills OUT of the default new-account experience.

PROBLEM: The seeder seeds EVERY directory under .claude/skills/ into the org catalog at ORG
scope, so 'compound-engineering', 'gstack', and 'playwright-cli' leak into the default catalog
that every new account sees. A brand-new account should load with ONLY the 'command-hq-starter'
bundle and its member skills.

INVESTIGATE FIRST (trace it; do not assume the fix):
- packages/backend/src/seed/skills.ts  (buildSeedSkills() / seedSkills() — the shared builder;
  this is ALSO what packages/backend/src/local/devServer.ts calls, so fixing it here fixes dev too)
- infra/scripts/seed-skills.mjs  (the standalone prod seeder with its own readSkillFiles())
- .claude/skills/bundles.json  (the bundle manifest; 'command-hq-starter' lists hq-* members —
  it currently only GROUPS, it does not gate seeding)
- packages/backend/test/seedSkills.test.ts, packages/backend/test/project-optin.test.ts
- the catalog read path so you confirm how scope decides visibility:
  packages/backend/src/db/repo.ts listSkills/listAgents, packages/shared/src/scope.ts
  (isVisible/resolveScoped), and the web Skills screens (packages/web/src/screens/Skills/*).

CHOOSE the seed-side allowlist mechanism (consistent with the existing model, do not invent a
parallel one):
- Only skills that are MEMBERS of a seeded bundle in bundles.json (i.e. the 'command-hq-starter'
  members) — plus the bundle record itself — are seeded at ORG scope (the org-wide default).
- The remaining skills that belong to NO bundle ('compound-engineering', 'gstack',
  'playwright-cli') are NOT part of the org default. Seed them at a NARROWER scope (user scope
  via userScope(<a designated grant owner>)) rather than deleting them — their SKILL.md files
  stay in the repo. Make the grant target configurable (e.g. an env var like
  SEED_GRANT_USER / SEED_GRANT_OWNER) with a sensible default, so "accounts granted them" can
  see them but a fresh org cannot. Keep this simple and documented in-code.
- Apply the SAME logic in BOTH seed paths: the shared builder (seed/skills.ts) AND the
  standalone infra/scripts/seed-skills.mjs, so prod and the bundled deploy agree. Putting the
  core filter in buildSeedSkills() means devServer inherits it with NO devServer edit — DO NOT
  edit devServer.ts (another agent owns its routing region; avoid the conflict).

CONSTRAINTS:
- Do NOT break 'command-hq-starter' or its hq-* members — they MUST still seed at org scope for
  every new account.
- Keep the seed IDEMPOTENT (re-running converges; records upsert by key).
- Keep the three SKILL.md files in the repo.

TESTS — update/extend:
- packages/backend/test/seedSkills.test.ts: assert the org-default record set is exactly the
  'command-hq-starter' bundle + its members, and that compound-engineering/gstack/playwright-cli
  are NOT seeded at org scope (and ARE seeded at the narrower scope if you chose that).
- packages/backend/test/project-optin.test.ts: keep it green; extend if opt-in now interacts
  with which skills are in the org catalog.
- If there is an isolated web Skills-screen test that asserts the default catalog, update it —
  but DO NOT touch packages/web/src/test/testUtils.ts or other web files the frontend agent
  owns (avoid conflicts). If you cannot change a web test without touching shared web files,
  leave it for the Verify phase and note it.

VERIFY your own change before returning:
   cd ${ROOT} && npm run build -w @harness/shared && npm run typecheck -w @harness/backend && npm test -w @harness/backend
   cd ${ROOT} && SEED_DRY_RUN=1 node infra/scripts/seed-skills.mjs   (confirm the reduced org-default set; on Windows PowerShell use: $env:SEED_DRY_RUN=1; node infra/scripts/seed-skills.mjs)
Fix what you broke. RETURN: the mechanism you chose, the exact files changed, the env var/default
for granting the three, and the SEED_DRY_RUN org-default record list you observed.`,
  { label: 'seed-scope', phase: 'Seed scope' },
);

log('Seed scoping complete — running cross-workspace verification.');

// ----------------------------------------------------------------------------
phase('Verify');

const verifyReport = await agent(
  `${CONTEXT}

YOUR PHASE: Verify + repair. The foundation, backend, and frontend changes for the
create/join-organization feature are now in the working tree. Your job is to make the WHOLE
repo green and internally consistent.

RUN (from ${ROOT}, in this order) and capture output:
   npm run build -w @harness/shared
   npm run typecheck --workspaces --if-present
   npm test --workspaces --if-present
   npm run format:check   (if it fails only on formatting, run \`npm run format\` to fix)
   SEED_DRY_RUN=1 node infra/scripts/seed-skills.mjs   (PowerShell: $env:SEED_DRY_RUN=1; node infra/scripts/seed-skills.mjs)
     -> confirm the ORG-DEFAULT record set is ONLY the 'command-hq-starter' bundle + its members,
        and that compound-engineering / gstack / playwright-cli are NOT in the org default.

THEN:
- Fix any TYPECHECK or TEST failures introduced by this feature. Likely hot spots:
  * the relaxed agent/skill scope schema (orgScopeRefSchema -> scopeRefSchema) breaking a
    place that assumed scope.tier is the literal 'org';
  * listAgents/listSkills signature change breaking a caller that didn't pass userId;
  * the new OrgGate changing what the existing web tests render (the /me fetch default);
  * devServer route ordering / regex;
  * the seed allowlist change breaking seedSkills.test.ts / project-optin.test.ts, or a web
    Skills-screen test that the seed agent deferred to you (it was told not to touch shared
    web files like testUtils.ts).
- Do NOT weaken assertions to make tests pass. Fix the underlying code. If a pre-existing
  failure is clearly unrelated to this feature (present on origin/main), note it and leave it.
- Re-run the full suite until green (or until only clearly-unrelated/pre-existing failures
  remain).

RETURN a final report: the exact commands run, pass/fail per workspace, every file you changed
during repair and why, and any remaining failures with your assessment of whether they are
pre-existing/unrelated. Be honest — if something is still broken, say so plainly.`,
  { label: 'verify', phase: 'Verify' },
);

return { foundationSummary, seedSummary, verifyReport };
