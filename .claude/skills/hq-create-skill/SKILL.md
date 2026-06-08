---
name: hq-create-skill
description: >-
  Scaffold a new Command HQ skill: create a `.claude/skills/<name>/SKILL.md` with
  valid frontmatter (name + description), explain how it gets registered/seeded
  into Command HQ, and how to add it to a bundle via the bundle manifest. A new
  skill is STANDALONE by default — it joins a bundle only when you list it in
  `.claude/skills/bundles.json`. Use when the user says "/hq-create-skill",
  "create a new HQ skill", "scaffold a skill", "add a Command HQ skill", or "make
  a new skill".
---

# /hq-create-skill

Authoring helper for **Command HQ skills**. A Command HQ skill is just a
`.claude/skills/<name>/SKILL.md` file in the repo: HQ's seed reads that directory
and writes each skill into the org catalog. This skill walks you through creating
one correctly and getting it registered.

> **A new skill is standalone by default.** Seeding it does NOT add it to any
> bundle. It joins `command-hq-starter` (or any other bundle) **only** when you
> explicitly list it in the bundle manifest, `.claude/skills/bundles.json`.

> **Scope + versioning (live).** The catalog code is 3-tier
> (`org` / `user` / `project`; see `packages/backend/src/rest/scopeauth.ts`). A
> seeded skill lands in the **org catalog** as its **base** variant (an org-scope
> write is admin-gated). Identity for a *version* is `(baseName, repoId, userId)`:
> editing the skill later from a project context **forks a new variant** + an
> immutable revision instead of clobbering the base. One org-wide **TRUE** version
> per name is the default shown/added; **any authed org member may promote** a
> variant to true (`POST /skills/:name/promote`). This skill only *scaffolds* the
> file — those flows kick in when it is edited/registered.

## When this runs

In the developer's claude+ session (the PTY), from inside the monorepo. It writes
a new `SKILL.md` under `.claude/skills/` and explains the seed/registration path.
It does not call any HQ endpoint directly — registration happens through the seed
script (`infra/scripts/seed-skills.mjs`) or `claude+ sync-skills`.

## What a Command HQ skill is

A directory `.claude/skills/<name>/` containing a `SKILL.md` whose YAML
frontmatter has two required keys:

- `name` — the skill's slash-command name (must match the directory name).
- `description` — a folded scalar (`>-`) that states what the skill does **and**
  the trigger phrases that should invoke it ("Use when the user says …").

The markdown body is the actual skill instructions. The whole file body is stored
in HQ (`skillSchema.body`) so a daemon can materialize it locally on sync.

## Steps

1. **Pick a name.** Lowercase, hyphenated, unique among `.claude/skills/`. It
   becomes both the directory name and the frontmatter `name`.
2. **Write the frontmatter.** Required `name` + `description`. Keep the
   description a single folded scalar; include explicit trigger phrases so the
   harness routes the slash command to it.
3. **Write the body.** Sections that work well: a one-line summary, "When this
   runs", "Steps", and "Contract" (any endpoints/commands it depends on — only
   reference things that actually exist).
4. **Scaffold the file.** Create `.claude/skills/<name>/SKILL.md`. A minimal valid
   template:

   ```markdown
   ---
   name: my-skill
   description: >-
     One or two sentences on what this does. Use when the user says
     "/my-skill", "do the thing", or "...".
   ---

   # /my-skill

   ## When this runs

   ...

   ## Steps

   1. ...
   ```

5. **Validate the frontmatter.** The seed parser (`parseFrontmatter` in
   `infra/scripts/seed-skills.mjs`) reads `name` and the folded `description`. If
   `name` is missing it falls back to the directory name; a missing `description`
   yields an empty one. Make both explicit.

## How it gets registered / seeded into HQ

- **Source of truth:** the repo's `.claude/skills/<name>/SKILL.md`.
- **Seed:** `node infra/scripts/seed-skills.mjs` reads every `.claude/skills/*`
  directory, parses each `SKILL.md`, reads the bundle manifest, and (via the
  backend's `buildSeedSkills` in `packages/backend/src/seed/skills.ts`) writes one
  `kind:'skill'` record per file plus one `kind:'bundle'` record per manifest
  entry — all at **org scope**, `source:'built-in'`. Run
  `npm run build -w @harness/backend` first (the seed imports the compiled
  builder), then the seed script. It is idempotent.
- **Per-device sync:** `claude+ sync-skills` (and the auto-sync on each new
  claude+ session) reconciles HQ's effective catalog into the isolated
  `~/.claude+` config root, so a just-seeded skill becomes usable without
  restarting.

## How to put it in a bundle (opt-in)

Bundles are organized by the **manifest** at `.claude/skills/bundles.json` — the
single source of truth for "what skills go in what bundles." A skill is standalone
unless a bundle lists it as a member:

```json
{
  "command-hq-starter": {
    "description": "Skills bundled with Command HQ + claude+.",
    "members": ["hq-update-progress", "hq-weekly-update", "hq-create-skill"]
  }
}
```

- To add this skill to `command-hq-starter`, append its `name` to that bundle's
  `members` array. To create a **new** bundle, add a new top-level entry with its
  own `description` + `members`. Re-seed to apply.
- Members of a bundle are hidden from the top-level Skills grid by default (toggle
  "Show skills that are in bundles" to reveal them), so a standalone skill is the
  most visible.

## Contract

This skill depends only on things that already exist: the `.claude/skills/`
convention, the `.claude/skills/bundles.json` manifest, `infra/scripts/seed-skills.mjs`,
`buildSeedSkills`, the skills REST (`packages/backend/src/rest/skills.ts`), and
`claude+ sync-skills`. It invents no new backend surface.
