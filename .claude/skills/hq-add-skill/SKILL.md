---
name: hq-add-skill
description: >-
  Add one skill or a whole bundle of skills to Command HQ from inside the claude+
  PTY. Reads the prompt to decide single-skill vs bundle, scaffolds each
  `.claude/skills/<name>/SKILL.md`, then registers them in the single org-scoped
  Command HQ catalog (admin-gated writes) and explains the per-project opt-in. Use
  when the user says "/hq-add-skill", "add a skill", "add a bundle of skills",
  "register a skill in HQ", "enable a skill on this project", or "put this skill in
  command-hq-starter".
---

# /hq-add-skill

Author and register Command HQ skills — a single one or a whole bundle — into the
**org catalog**, then opt the current project into the ones it needs. Runs in the
developer's claude+ session, inside a connected repo.

> **Model (2026-06-03):** there are **no scope tiers** anymore. Every skill and
> agent lives in one **org catalog** (`scope: { tier: "org", id: "<org>" }`).
> Catalog writes are **admin-gated**. Projects then **opt in** to individual items
> via `enabledSkills` / `enabledAgents`. There is no project/user/org scope choice
> to make, and no promote/demote. See the
> [org-catalog design spec](../../../docs/superpowers/specs/2026-06-03-org-catalog-scope-collapse-design.md).

## 1 · Decide: single skill or a bundle

Read the prompt:

- **Single skill** — "add a skill that does X". Produce one
  `.claude/skills/<name>/SKILL.md` and register it.
- **Bundle** — "add a bundle of skills", "add these N skills", or a list/theme
  ("a testing bundle: lint, unit, e2e"). Produce one `SKILL.md` per member, plus
  a **bundle record** (`kind: bundle`) whose `members` are the member names. A
  member may itself be a bundle (bundles nest; resolution is transitive).

If it's ambiguous, ask: "one skill, or a bundle of several?"

## 2 · There is no scope to choose — it's the org catalog

Every skill and bundle is org-scoped. You do **not** ask the user for project /
user / org. The only question is whether you can write the catalog: catalog writes
(`POST/PUT/DELETE /skills`, `/agents`) are **admin-gated**. On create the server
forces `scope = orgScope(<org>)` and stamps `createdBy: { userId, name }` from the
authenticated principal — you never set `scope` or `createdBy` yourself.

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

There are two registration paths; both land in the same org catalog:

- **The org catalog via the seed path.** The seed
  (`infra/scripts/seed-skills.mjs` → `packages/backend/src/seed/skills.ts`
  `buildSeedSkills`) reads every `.claude/skills/*/SKILL.md` and writes each into
  the **org catalog** as a standalone skill, stamping
  `createdBy:{userId:'system',name:'system'}`. **Bundling is opt-in via the
  manifest** `.claude/skills/bundles.json` — a new skill joins `command-hq-starter`
  (or any bundle) only if you add its name to that bundle's `members`. Unless the
  user asks to bundle it, leave it standalone. The registration *is* getting the
  file onto `main`, so **land it to `main` now — do not stop and ask**:

  1. Stage and commit just the new `.claude/skills/<name>/` file(s) with the
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
     - The seed reads **every** `.claude/skills/*/SKILL.md` and writes the new
       skill into the org catalog as a **standalone** skill (no bundle) unless its
       name is listed in `.claude/skills/bundles.json`.
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

- **Direct catalog REST (admin).** Register via the Command HQ skills REST,
  authorized with the device/session token claude+ already holds (HQ API base from
  `claude+ login`). The caller must be an org admin:

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

  Agents mirror skills (`POST <HQ_API>/agents`, etc.). If the caller is not an
  admin, the write is rejected — surface that and ask an admin to register it.

## 5 · Opt a project into the skill (per-project enablement)

Registering a skill puts it in the catalog; it is **not** active on any project
until that project opts in. A skill can be enabled directly, or pulled in by an
agent:

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
> endpoint are **gone** (`410`). Don't reach for them.

## 6 · Make it available in this session — always do this

This step is **mandatory, not optional** — it's the difference between "the skill
is in the catalog" and "the user can run it right now." Always finish here:

Run `claude+ sync-skills` (or `/hq-update-skills`) to pull the **linked project's
enabled** skill(s) into the isolated `~/.claude+` registry so they're usable now.
Writes go to `~/.claude+`, never your personal `~/.claude`. If you registered a
skill but didn't opt the project in (step 5), the sync won't pull it — enable it on
the project first. Report the printed result (e.g. `skills synced: pulled 1,
pushed 0`); the skill is available on the next turn. Do not end the run telling the
user to sync later — do it for them.

## Notes

- Bundle membership is **data, not code**: for the `command-hq-starter` bundle,
  adding a `.claude/skills/<name>/` file is all that's needed — the seed picks it
  up. For other catalog bundles, membership is the `members` array on the bundle
  record (REST or Skills tab).
- This is the multi-skill companion to `/hq-create-hq-skill` (which scaffolds a
  single skill). Both ride the same seed + org-catalog REST registration paths.
