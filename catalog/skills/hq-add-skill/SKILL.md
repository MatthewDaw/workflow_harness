---
name: hq-add-skill
description: >-
  Add one skill or a whole bundle of skills to Command HQ from inside the claude+
  PTY. Reads the prompt to decide single-skill vs bundle, scaffolds each
  `.claude/skills/<name>/SKILL.md`, then registers them in the single org-scoped
  Command HQ catalog (a direct, admin-gated REST write using the claude+ device
  token) and explains the per-project opt-in. Use
  when the user says "/hq-add-skill", "add a skill", "add a bundle of skills",
  "register a skill in HQ", "enable a skill on this project", or "put this skill in
  command-hq-starter".
---

# /hq-add-skill

Author and register Command HQ skills — a single one or a whole bundle — into the
**org catalog**, then opt the current project into the ones it needs. Runs in the
developer's claude+ session, inside a connected repo.

> **Scope model (live, 3-tier).** The catalog code is firmly 3-tier —
> `org` / `user` / `project` (see `packages/backend/src/rest/scopeauth.ts`
> `canReadScope` / `canWriteScope`). Skills and agents live in the **org catalog**
> by default (`scope: { tier: "org", id: "<org>" }`), and an **org-scope write is
> admin-gated**. Admin is decided **server-side** (`scopeauth.ts`
> `resolveOrgCatalogAuth` / `isOrgAdmin`): the caller's profile marks them an admin
> of the org (`profile.adminOrgs` includes it, or `profile.admin`), **or** the
> gateway carries the `custom:admin` claim. Because it is derived from the profile
> rather than a token claim, the **claude+ device token writes the catalog
> directly** — the write routes are public at the gateway and the handler verifies
> the bearer token + admin itself. The server **forces** `scope` and stamps
> `createdBy` from the authenticated principal — you never set them. Projects then
> **opt in** to individual items via `enabledSkills` / `enabledAgents`.
>
> **Versioning (live).** A version is `(baseName, repoId, userId)`. Editing the
> org skill `S` from project `R` as person `P` **forks/updates** the variant
> `(S, R, P)` and **snapshots an immutable revision** — it never edits the org
> **base** variant or anyone else's. One org-wide **TRUE** version per name is the
> UI default and the default added to a project; **any authed org member may
> promote** a variant to true via `POST /skills/:name/promote { variantId, rev? }`
> (NOT admin-gated). A project's enabled-set entry carries the chosen variant
> (`{ name, variantId }`, default = TRUE); a per-repo dropdown can switch it. This
> replaces the retired `POST /skills/:name/scope` verb (`410 Gone`). See the
> [org-catalog design spec](../../../docs/superpowers/specs/2026-06-03-org-catalog-scope-collapse-design.md).

## 0 · Environment — these are FIXED; do not re-discover them

Re-deriving the HQ base, the token, the plugin path, and "which repo am I in" is
the #1 time sink. Bind them once and move on:

- **HQ REST base:** `https://l5edwucexb.execute-api.us-east-1.amazonaws.com` (the
  `ApiStack.HttpApiUrl` — NOT the `wss://` url in the credentials file).
  `HQ="https://l5edwucexb.execute-api.us-east-1.amazonaws.com"`
- **Device token:** line 2 of `~/.claude-plus/credentials` (line 1 is the WS url).
  `TOK="$(sed -n 2p ~/.claude-plus/credentials)"`. The **org rides inside the
  token** — never prompt for it. Admin is decided server-side from your profile.
- **Which repo am I in?** One check: `test -f infra/scripts/seed-skills.mjs` →
  the HQ **backend** repo (catalog source is `catalog/skills/`; the seed path
  applies). Otherwise a **connected project repo** → use the **direct REST** path
  (the default below) and never hunt for the seed script.
- **Plugin skills on disk:** `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<name>/SKILL.md`
  (use the newest `<version>` dir). This is where "add plugin X as a bundle" reads.

## Fast path: register a whole plugin as a bundle

"Add `<plugin>` as a bundle in Command HQ" is ONE idempotent batch — not per-skill
hand-rolling, and not three script iterations. With `$HQ`/`$TOK` bound:

```bash
HQ="https://l5edwucexb.execute-api.us-east-1.amazonaws.com"
TOK="$(sed -n 2p ~/.claude-plus/credentials)"
SK="$(ls -d ~/.claude/plugins/cache/<mkt>/<plugin>/*/skills | sort -V | tail -1)"
PLUGIN="<plugin>" HQ="$HQ" TOK="$TOK" SK="$SK" python - <<'PY'
import os, json, glob, urllib.request, urllib.error
HQ, TOK, SK, PLUGIN = (os.environ[k] for k in ("HQ","TOK","SK","PLUGIN"))
def post(obj):
    req = urllib.request.Request(HQ+"/skills", data=json.dumps(obj).encode(),
        headers={"Authorization":"Bearer "+TOK, "content-type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r: return r.status
    except urllib.error.HTTPError as e: return e.code   # 403=not org admin · 409=canonical built-in
members=[]
for md in sorted(glob.glob(SK+"/*/SKILL.md")):
    name = os.path.basename(os.path.dirname(md)); body = open(md, encoding="utf-8").read()
    desc = next((l.split(":",1)[1].strip(" >|-") for l in body.splitlines()
                 if l.startswith("description:")), name)
    print(name, post({"name":name,"kind":"skill","source":"custom","description":desc,"body":body}))
    members.append(name)
print("bundle", post({"name":PLUGIN,"kind":"bundle","source":"custom",
    "description":PLUGIN+" plugin skills","members":members}))
PY
```

Idempotent (the server upserts by name, so re-running converges). `201/200` ok ·
`403` = your profile isn't an org admin (ask an admin / the [seed path](#fallback--the-seed-path-bootstrap--no-admin)) ·
`409` = a canonical built-in you can't clobber. Then go to **§5 opt-in — read its
fail-fast rule first** (a locally-run repo usually has no project to opt into).

## 1 · Decide: single skill or a bundle

Read the prompt:

- **Single skill** — "add a skill that does X". Produce one
  `.claude/skills/<name>/SKILL.md` and register it.
- **Bundle** — "add a bundle of skills", "add these N skills", or a list/theme
  ("a testing bundle: lint, unit, e2e"). Produce one `SKILL.md` per member, plus
  a **bundle record** (`kind: bundle`) whose `members` are the member names. A
  member may itself be a bundle (bundles nest; resolution is transitive).

If it's ambiguous, ask: "one skill, or a bundle of several?"

## 2 · Scope: org catalog by default (3-tier underneath)

A new skill/bundle defaults to the **org catalog**. You do **not** prompt for a
tier in the common path — the server forces `scope = orgScope(<org>)` and stamps
`createdBy: { userId, name }` from the authenticated principal, so you never set
`scope` or `createdBy` yourself. The thing to know is *who may write*: an
**org-scope** catalog write (`POST/PUT/DELETE /skills`, `/agents`) is
**admin-gated**, decided server-side from the caller's profile
(`profile.adminOrgs` / `profile.admin`) or the `custom:admin` claim. The claude+
**device token** the PTY already holds satisfies this when your profile is an org
admin, so the direct REST write below works from the session — no git, no AWS
creds. (The underlying model is still 3-tier — a user can write their own user
scope and a project owner their own project scope without admin.)

**An edit is a fork, not a clobber.** Editing an existing org skill from a project
context cuts a **new version/variant** keyed by `(name, repo, person)` and
snapshots an immutable revision — it never overwrites the org base variant or
anyone else's. The shared catalog stays safe.

**Canonical built-ins are fork-only.** A `source:'built-in'` skill (and any
seed-owned record, `createdBy.userId === 'system'`) is owned by the git seed: an
in-place `POST`/`PUT`/`DELETE` to its base, or a membership edit to a built-in
bundle, is rejected with **409** — fork it (set `repoId` + `authorUserId`) or
change `catalog/skills` and re-seed. The DB is still the single runtime source of
truth; this only protects the *default bundle's* canonical rows.

The bundled `command-hq-starter` bundle is part of this same org catalog.

## 3 · Scaffold the SKILL.md file(s)

For each skill, write `.claude/skills/<name>/SKILL.md` (kebab-case `<name>`):

```
---
name: <name>
description: >-
  <what it does> … Use when the user says "/<name>", "…".
---

# /<name>

<body: what it does, when it runs, the concrete steps/commands>
```

Never overwrite an existing skill file — if `<name>` exists, stop and tell the
user.

## 4 · Register into the org catalog

A skill/bundle record is:
`{ name, scope:{tier:"org",id:"<org>"}, kind:"skill"|"bundle", description,
source:"custom", members:[...], body:"<full SKILL.md text>", createdBy:{userId,name} }`
— but you supply only `name`, `kind`, `description`, `source`, `members`, `body`;
the server forces `scope` and stamps `createdBy`.

Two ways to land a skill in the org catalog. **Default: the direct REST write** —
it works straight from the claude+ PTY with the device token, no git and no AWS
creds. Fall back to the **seed path** only to bootstrap a fresh org's built-in
defaults, or when you are not an org admin and must go through review.

### Default — direct catalog REST (device token)

Register via the Command HQ skills REST, authorized with the device token claude+
already holds (HQ API base from `claude+ login`). Admin is checked server-side
from your profile, so an org-admin profile writes directly — these routes are
public at the gateway and the handler verifies the bearer token + admin itself:

```
# create each member skill in the org catalog (server forces scope + createdBy)
POST <HQ_API>/skills
  { "name":"<name>", "kind":"skill", "description":"<desc>",
    "source":"custom", "body":"<SKILL.md text>" }

# create the bundle (members reference the skill names)
POST <HQ_API>/skills
  { "name":"<bundle>", "kind":"bundle", "description":"<desc>",
    "source":"custom", "members":["<a>","<b>"] }

# …or add a member to an existing bundle
POST <HQ_API>/skills/<bundle>/members   { "member":"<name>" }
```

Agents mirror skills (`POST <HQ_API>/agents`, etc.). Reading the result:

- **`201`/`200`** — live in the catalog immediately; no deploy, no seed. Go
  straight to step 5 (opt the project in) and step 6 (sync).
- **`403`** — your profile is not an org admin. Surface that and either ask an
  admin to register it, or use the seed path below (review route).
- **`401`** — the deployed API predates device-token catalog writes (the write
  routes need `HttpNoneAuthorizer` + `resolveOrgCatalogAuth`). A
  `cdk deploy ApiStack` brings it current; until then, use the seed path.

### Fallback — the seed path (bootstrap / no admin)

The seed (`infra/scripts/seed-skills.mjs` → `packages/backend/src/seed/skills.ts`
`buildSeedSkills`) reads every `catalog/skills/*/SKILL.md` and writes each into the
**org catalog** as a standalone skill, stamping
`createdBy:{userId:'system',name:'system'}`. **Bundling is opt-in via the manifest**
`catalog/skills/bundles.json` — a new skill joins `command-hq-starter` (or any
bundle) only if you add its name to that bundle's `members`. Unless the user asks
to bundle it, leave it standalone. The registration _is_ getting the file onto
`main`, so **land it to `main` now — do not stop and ask**:
  1. Stage and commit just the new `catalog/skills/<name>/` file(s) with the
     developer's git (conventional message, e.g. `feat(skills): add /<name> to
command-hq-starter bundle`). Commit only the skill file(s), not unrelated
     untracked paths.
  2. Get the commit onto `main`. If already on `main`, push it. Otherwise prefer
     the project's land flow if one exists (e.g. `/land-and-deploy`); else fast-
     forward `main` to this commit and push, or open a PR and merge it. Use the
     developer's own `git`/`gh` — never `--no-verify`.
  3. **Seed the deployed catalog NOW — do not wait for the Action.** The
     `seed-skills.yml` workflow will eventually re-seed on the `main` push, but
     the user wants the skill live on the website the moment this skill finishes.
     So run the seed directly against the deployed `harness` table yourself
     (it is idempotent — upserts by key — so running it is always safe):

     ```bash
     npm run build -w @harness/backend \
       && SEED_ORG="${SEED_ORG:-personasearch}" HARNESS_TABLE=harness AWS_REGION=us-east-1 \
          node infra/scripts/seed-skills.mjs
     ```

     - `SEED_ORG` must match the org the website serves (the deployed default is
       `personasearch`; confirm against `packages/web/.env*` `VITE_ORG` if unsure).
     - The seed reads **every** `catalog/skills/*/SKILL.md` and writes the new
       skill into the org catalog as a **standalone** skill (no bundle) unless its
       name is listed in `catalog/skills/bundles.json`.
     - **Requires local AWS credentials** with write access to the `harness`
       table (same role the deploy uses). If the seed fails with a credentials /
       AccessDenied error, say so plainly and fall back to: trigger the
       `seed-skills` GitHub Action (Actions → seed-skills → Run workflow), or ask
       someone with deploy creds to run the seed. Do not claim it is live when the
       seed did not succeed.
     - Run `SEED_DRY_RUN=1 …` first if you want to preview the records without
       writing.

  4. **Tell the user where to see it.** A standalone skill shows directly in the
     top-level **Skills** grid after a hard refresh. If you added it to a bundle
     via the manifest, it's hidden under that bundle by default — toggle **"show
     in bundles"** on `/skills` or open the bundle to see it.

## 5 · Opt a project into the skill (per-project enablement)

> **Never turn this step into a multiple-choice question.** Do NOT stop and ask
> the user "connect repo / install locally / catalog-only?" — that interactive
> menu is exactly the friction this skill exists to remove. Apply the deterministic
> defaults below (auto-run any local install doc in the prompt; report-and-finish
> when the repo isn't a connected HQ project) and only surface a prompt if a
> command **fails** and needs a real decision.

**If the prompt carried a literal install doc, run it — don't just catalog it.**
When the user pasted real install instructions for an external tool (a
`git clone … && ./setup`, a CLAUDE.md edit, an `npm install`, etc. — gstack is the
canonical example), the catalog registration is only half the ask. Also perform
the local install automatically, no questions:
  1. Run the install commands verbatim (e.g. `git clone --single-branch --depth 1
     <repo> ~/.claude/skills/<tool> && cd ~/.claude/skills/<tool> && ./setup`).
  2. Make the CLAUDE.md edits the doc describes (e.g. add the tool's section,
     listing its skills / browse rules).
  3. Commit just those changes with the developer's own `git` (conventional
     message, never `--no-verify`) so teammates get it via the repo.
Report what you ran. If a step genuinely fails (clone error, `./setup` non-zero),
say so plainly — that is the only case where you may pause for input.

**Fail-fast gate — check FIRST, and STOP if it fails.** Per-project opt-in needs
the repo to be a **connected HQ project** AND you to have its `projectId`. There
is NO list endpoint to enumerate: `GET /projects` is admin-only (`401`), and
`/me/projects` / `/org/projects` don't exist. So:

- If you already have a `projectId` (the user gave one, or a known connected
  project), use it.
- Otherwise **derive the candidate the way HQ and the daemon do** and probe THAT
  one id. A connected project's id is the slug of the repo's **`owner/repo`** (NOT
  the bare repo name, NOT the folder name): take the git remote's `owner/repo`,
  lowercase it, and collapse every run of non-alphanumeric characters to a single
  dash. Example: remote `git@github.com:MatthewDaw/fractions_tutorial.git` →
  `matthewdaw-fractions-tutorial` (this is exactly `projectIDFor` in
  `wrapper/internal/daemon/runtime.go`, guarded by `reponame_test.go`). Concretely:

  ```bash
  remote=$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null)
  # strip scheme/host/.git, keep last two path segments as owner/repo, then slug
  ownerRepo=$(echo "$remote" | sed -E 's#^.*[:/]([^/]+/[^/]+?)(\.git)?/?$#\1#')
  candidate=$(echo "$ownerRepo" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g')
  ```

  Do **ONE** probe — `GET $HQ/projects/<candidate>`. If it returns a project, you
  have the `projectId` — proceed to opt-in. Only if that correctly-derived id
  returns `{"error":"not found"}` is this repo genuinely **not an HQ project**:
  **STOP** the opt-in attempt — but **do not ask** what to do instead. Do NOT then
  flail across other id shapes (bare repo name, folder name, local state) — the
  owner/repo slug is the one and only id HQ mints, so if it misses, no record
  exists (a locally-run claude+ repo with no remote has none by design).
  Registering in the org catalog (plus the local install above, if a doc was
  given) IS the deliverable; tell the user verbatim and then finish at step 6:
  *"Registered + bundled in the org catalog. It's not enabled on a project yet —
  connect this repo in the HQ web app (which mints a projectId) or toggle the
  bundle on the HQ **Skills** tab."*

Once you HAVE a `projectId`, a whole bundle enables all its members in one call
(`POST $HQ/projects/<projectId>/bundles/<bundleName>`); or enable a skill directly
or via an agent:

```
# enable a skill directly on a project (idempotent)
POST   <HQ_API>/projects/<projectId>/skills/<skillName>
DELETE <HQ_API>/projects/<projectId>/skills/<skillName>

# enable an agent — this also UNIONS the agent's skills (bundles flattened to
# leaves) into the project's enabledSkills, so its dependencies come along
POST   <HQ_API>/projects/<projectId>/agents/<agentName>
DELETE <HQ_API>/projects/<projectId>/agents/<agentName>   # leaves enabledSkills intact
```

Each returns the updated `Project` with `enabledSkills` / `enabledAgents`
populated. Auth gate: org admin **or** the project owner. The skill/agent name
must already exist in the org catalog (else `404`). You can also do this from the
HQ project **Skills** / **Agents** tabs.

> The old per-skill scope picker and the `POST /skills/<name>/scope` change
> endpoint are **gone** (`410`). The versioning replacement is
> `POST /skills/<name>/promote { variantId, rev? }` — repoint the org-wide **TRUE**
> variant (the default shown in the UI and added to a project). **Any authed org
> member may promote**; it only repoints the TRUE pointer and never edits or
> deletes a variant. A project's enabled-set entry carries `{ name, variantId }`
> (default = TRUE), switchable per-repo via a dropdown.

## 6 · Make it available in this session — always do this

This step is **mandatory, not optional** — it's the difference between "the skill
is in the catalog" and "the user can run it right now." Always finish here:

Run `claude+ sync` (or `/hq-update-skills`) to pull the **linked project's
enabled** skill(s) into the isolated `~/.claude+` registry so they're usable now.
Writes go to `~/.claude+`, never your personal `~/.claude`. If you registered a
skill but didn't opt the project in (step 5), the sync won't pull it — enable it on
the project first. Report the printed result (e.g. `skills synced: pulled 1,
pushed 0`); the skill is available on the next turn. Do not end the run telling the
user to sync later — do it for them.

## Notes

- Bundle membership is **data, not code**: for the `command-hq-starter` bundle,
  adding a `catalog/skills/<name>/` file is all that's needed — the seed picks it
  up. For other catalog bundles, membership is the `members` array on the bundle
  record (REST or Skills tab).
- This skill covers the whole lifecycle for a single skill OR a bundle: scaffold
  the `SKILL.md`(s), register them in the org catalog (seed or REST), opt the
  project in, and sync so they're usable now.
