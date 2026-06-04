---
name: add-skill
description: >-
  Add one skill or a whole bundle of skills to Command HQ from inside the claude+
  PTY. Reads the prompt to decide single-skill vs bundle, scaffolds each
  `.claude/skills/<name>/SKILL.md`, then registers them in Command HQ at the scope
  you choose — project, user (your global), or org (global / everyone). Use when
  the user says "/add-skill", "add a skill", "add a bundle of skills", "register
  a skill in HQ", "add these skills at user/project/global scope", or "put this
  skill in command-hq-starter".
---

# /add-skill

Author and register Command HQ skills — a single one or a whole bundle — and set
their scope. Runs in the developer's claude+ session, inside a connected repo.

## 1 · Decide: single skill or a bundle

Read the prompt:

- **Single skill** — "add a skill that does X". Produce one
  `.claude/skills/<name>/SKILL.md` and register it.
- **Bundle** — "add a bundle of skills", "add these N skills", or a list/theme
  ("a testing bundle: lint, unit, e2e"). Produce one `SKILL.md` per member, plus
  a **bundle record** (`kind: bundle`) whose `members` are the member names. A
  member may itself be a bundle (bundles nest; resolution is transitive).

If it's ambiguous, ask: "one skill, or a bundle of several?"

## 2 · Choose the scope

Every skill/bundle is stored at exactly one scope. Ask the user (default to
**user** if unspecified) — narrowest scope wins on a name collision (project
overrides user overrides org):

| You say | Tier | `scope` value | Who sees it |
|---------|------|---------------|------------|
| project / this repo | `project` | `{ tier: "project", id: "<projectId>" }` | only inside that repo |
| user / my global (default) | `user` | `{ tier: "user", id: "<yourUserId>" }` | you, across all your repos |
| global / org / everyone | `org` | `{ tier: "org", id: "<org>" }` | the whole org (admin-gated) |

`org` scope is the "global / shipped with the product" tier; writing to it
requires admin. The bundled `command-hq-starter` bundle lives at `org`.

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

## 4 · Insert into Command HQ at the chosen scope

A skill/bundle record is:
`{ name, scope:{tier,id}, kind:"skill"|"bundle", description, source:"custom",
members:[...], body:"<full SKILL.md text>" }`.

Pick the path that matches the scope:

- **Global / org (the product bundle):** the seed path is automatic. The
  seed (`infra/scripts/seed-skills.mjs` → `packages/backend/src/seed/skills.ts`
  `buildSeedSkills`) reads every `.claude/skills/*/SKILL.md` and writes them at
  **org** scope as members of `command-hq-starter`. So once the file is committed
  to `main`, the `seed-skills.yml` workflow re-seeds it in; or run it manually:
  `npm run build -w @harness/backend && SEED_ORG=<org> node infra/scripts/seed-skills.mjs`.

- **User or project scope:** register via the Command HQ skills REST, authorized
  with the device/session token claude+ already holds (HQ API base from
  `claude+ login`). One record per skill, then one for the bundle:

  ```
  # create each member skill at the chosen scope
  POST <HQ_API>/skills
    { "name":"<name>", "scope":{"tier":"user","id":"<uid>"}, "kind":"skill",
      "description":"<desc>", "source":"custom", "body":"<SKILL.md text>" }

  # create the bundle (members reference the skill names)
  POST <HQ_API>/skills
    { "name":"<bundle>", "scope":{"tier":"user","id":"<uid>"}, "kind":"bundle",
      "description":"<desc>", "source":"custom", "members":["<a>","<b>"] }

  # …or add a member to an existing bundle
  POST <HQ_API>/skills/<bundle>/members   { "member":"<name>" }
  ```

  For `project` scope use `{"tier":"project","id":"<projectId>"}`. For `org`
  scope the caller must be an admin (`canWriteScope` gates it).

- **Quick local-to-HQ for user scope:** `claude+ sync-skills` (a.k.a.
  `/update-skills`) pushes any local-only `.claude/skills/*` up to **user** scope
  and pulls HQ's set down. Use this when user scope is all you need and you don't
  want to hand-call the REST.

## 5 · Set / change scope later

To move a skill or bundle between scopes after the fact, use the HQ **Skills**
tab scope picker, or `POST <HQ_API>/skills/<name>/scope { "scope": {tier,id} }`
(re-keys the record; org tier is admin-gated). Add/remove bundle members from the
bundle's drill-in view or the `/members` endpoints above.

## 6 · Make it available in this session

Run `claude+ sync-skills` (or `/update-skills`) to pull the newly-registered
skill(s) into the isolated `~/.claude+` registry so they're usable now. Writes go
to `~/.claude+`, never your personal `~/.claude`.

## Notes

- Bundle membership is **data, not code**: for the org `command-hq-starter`
  bundle, adding a `.claude/skills/<name>/` file is all that's needed — the seed
  picks it up. For user/project bundles, membership is the `members` array on the
  bundle record (REST or Skills tab).
- This is the multi-skill / scope-aware companion to `/create-hq-skill` (which
  scaffolds a single skill). Both ride the same seed + REST registration paths.
