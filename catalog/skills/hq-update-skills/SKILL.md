---
name: hq-update-skills
description: >-
  Run inside the claude+ PTY to refresh the locally-available skills from Command
  HQ. It performs a one-shot reconcile of the linked project's enabled skills/agents
  (from the org catalog) into the isolated ~/.claude+ config root claude+ launches
  Claude against — pulling newly-enabled skills and pushing your local ones up to
  the org catalog — without ever touching your personal ~/.claude. Use when the user
  says "/hq-update-skills", "update my skills", "sync skills", "pull the latest
  skills", or after an admin enables a skill/agent on this project in HQ.
---

# /hq-update-skills

On-demand refresh of the skills claude+ makes available to Claude. claude+
already auto-syncs once per new session; this skill forces that refresh now, so a
just-published bundled skill is usable without restarting a session.

## What it does

Runs the wrapper's one-shot reconcile (`claude+ sync`), which:

1. Fetches the catalog (`GET /skills`, `GET /agents`) — the **org catalog merged
   with the caller's own user-scoped items** (the 3-tier model in
   `scopeauth.ts`: a user-scoped item shadows an org-scoped one of the same name)
   — then resolves this repo's linked project and reads its `enabledSkills` /
   `enabledAgents` (`GET /projects/:id`).
2. Computes the **effective set** = catalog items whose name is in the project's
   `enabledSkills` (skills) or `enabledAgents` (agents), each resolved to the
   variant the project pinned (`{ name, variantId }`, default = the org-wide
   **TRUE** variant). An enabled agent's own skills are already in `enabledSkills`
   (the server union-added them when the agent was enabled), so no extra expansion
   is needed here.
3. **Pulls** any effective item missing locally (or pinned to a newer variant)
   into `~/.claude+/skills/<name>/SKILL.md` (and agents into `~/.claude+/agents/`).
4. **Pushes** any local-only skill up to the catalog. An org-scope write is
   **admin-gated** (a non-admin push to the org catalog is rejected); editing an
   existing item from a project context instead **forks a new version/variant**
   (`(name, repo, person)`, immutable revision) — it never clobbers the org base.

This is **not** a narrowest-wins flatten of three independent tiers — it is the
org catalog (with the caller's user-scoped overrides) filtered by one project's
opt-in, resolving each enabled name to its pinned variant. It writes only into the
isolated
`~/.claude+` root — never your personal `~/.claude` — so bundled product skills
never pollute your normal Claude Code dataset. Items that exist on both sides with
different content are reported as `differs`: a sync never silently overwrites an
edited definition. To publish a local edit, **fork a new version/variant** (an
edit from a project context cuts the `(name, repo, person)` variant + an immutable
revision — never clobbering the org base); to adopt the catalog's version, delete
the local file and re-run so the pinned variant re-pulls.

## How to run

In the developer's claude+ session, inside a connected repo (the device token +
HQ endpoint established at `claude+ login` are reused):

**Getting the `projectId` — never guess it.** claude+ injects the authoritative HQ
project id into the session as `$CLAUDE_PLUS_PROJECT_ID` (alongside `$CLAUDE_PLUS_REPO`
and `$CLAUDE_PLUS_API_URL`), mirrored in `$CLAUDE_CONFIG_DIR/hq-project.json`. Use
`$CLAUDE_PLUS_PROJECT_ID` directly as the `projectId` in any `/projects/<projectId>/…`
call. Do NOT derive, slug, or probe candidate ids. If `$CLAUDE_PLUS_PROJECT_ID` is empty,
this session is not running under claude+ — say so and stop; do not guess.

```bash
claude+ sync
```

Report the printed result to the user, e.g. `synced (skills + agents + mcp): pulled 2, pushed 0`.
A freshly-pulled skill is available immediately for the next turn (claude+'s
inner Claude reads skills from `~/.claude+`).

## Then seed the deployed catalog so it's LIVE on the website

`claude+ sync` reconciles `~/.claude+` (this machine) and pushes local-only skills
via the admin REST, but the **website's Skills tab reads the deployed org
catalog**, which is only repopulated by the seed. When the goal is "run the skill
and see it live on the website," always finish by seeding the deployed `harness`
table directly — do **not** wait for the `seed-skills.yml` GitHub Action:

```bash
npm run build -w @harness/backend \
  && SEED_ORG="${SEED_ORG:-personasearch}" HARNESS_TABLE=harness AWS_REGION=us-east-1 \
     node infra/scripts/seed-skills.mjs
```

- The seed reads **every** repo `.claude/skills/*/SKILL.md` and upserts each into
  the org catalog as a standalone skill, plus one bundle per entry in the manifest
  `.claude/skills/bundles.json` (a skill joins `command-hq-starter` only if listed
  there). It is idempotent, so re-running is always safe.
- `SEED_ORG` must match the org the website serves (deployed default
  `personasearch`; confirm via `packages/web/.env*` `VITE_ORG`).
- **Requires local AWS credentials** for the `harness` table. If the seed fails
  with a credentials / AccessDenied error, report it and fall back to running the
  `seed-skills` GitHub Action (Actions → seed-skills → Run workflow) — and do not
  claim the website is updated when the seed did not succeed.
- For the website to show skills from the repo (not just `~/.claude+`), the new
  `.claude/skills/<name>/` file(s) must be on `main` first if you also want the
  Action path / other machines to pick them up — commit + push them, then seed.

After seeding, the skill shows in the web **Skills** tab. A standalone skill
appears directly in the top-level grid; a bundled one (listed in the manifest)
sits inside its bundle — toggle **"show in bundles"** on `/skills` or open the
bundle to see it (a hard page refresh picks up the new catalog).

## When nothing happens

- `pulled 0, pushed 0` — already in sync; nothing to do.
- A catalog skill you expected didn't pull — the linked project hasn't **opted in**
  to it. Enable it on the project first (`POST /projects/$CLAUDE_PLUS_PROJECT_ID/skills/:name`, or
  enable an agent that brings it, or the HQ project Skills/Agents tabs), then re-run.
- `not signed in to HQ` — run `claude+ login` first, then retry.
- A `differs` item — surface it to the user; they decide whether to keep the
  local edit or adopt HQ's version (re-run after deleting the local file to adopt
  HQ's).
