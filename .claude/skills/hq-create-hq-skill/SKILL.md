---
name: hq-create-hq-skill
description: >-
  Scaffold a new Command HQ skill: create a `.claude/skills/<name>/SKILL.md` with
  valid frontmatter (name + description), explain how it gets registered/seeded
  into Command HQ (the org-scope `command-hq-starter` bundle via the seed path),
  and how to add it to a bundle in the HQ Skills tab. Use when the user says
  "/hq-create-hq-skill", "create a new HQ skill", "scaffold a skill", "add a Command
  HQ skill", or "make a new bundled skill".
---

# /hq-create-hq-skill

Authoring helper for **Command HQ skills**. A Command HQ skill is just a
`.claude/skills/<name>/SKILL.md` file in the repo: HQ's seed reads that directory
and writes each skill into the registry at **org scope**, grouped under the
`command-hq-starter` bundle, so every user in the org sees it. This skill walks
you through creating one correctly and getting it registered.

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
4. **Scaffold the file.** Create `.claude/skills/<name>/SKILL.md` with the above.
   A minimal valid template:

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
  directory, parses each `SKILL.md`, and (via the backend's
  `buildSeedSkills` in `packages/backend/src/seed/skills.ts`) writes one
  `kind:'skill'` record per file plus a `command-hq-starter` `kind:'bundle'`
  record listing them as members — all at **org scope**, `source:'built-in'`.
  Run `npm run build -w @harness/backend` first (the seed imports the compiled
  builder), then the seed script. It is idempotent.
- **Per-device sync:** `claude+ sync-skills` (and the auto-sync on each new
  claude+ session) reconciles HQ's effective registry into the isolated
  `~/.claude+` config root, so a just-seeded skill becomes usable without
  restarting.

## How to add it to a bundle

- **Default bundle:** running the seed automatically lists every
  `.claude/skills/*` skill as a member of `command-hq-starter`, so a new file is
  in that bundle on the next seed.
- **In the HQ Skills tab:** open the bundle card → use the searchable "add skill"
  combobox to add the skill as a member, or the scope picker to elevate/demote a
  skill between org / my-global / project scopes. Members of a bundle are hidden
  from the top-level grid by default (toggle "Show skills that are in bundles" to
  reveal them).

## Contract

This skill depends only on things that already exist: the `.claude/skills/`
convention, `infra/scripts/seed-skills.mjs`, `buildSeedSkills`, the skills REST
(`packages/backend/src/rest/skills.ts`), and `claude+ sync-skills`. It invents no
new backend surface.
