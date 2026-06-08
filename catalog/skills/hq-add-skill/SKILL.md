---
name: hq-add-skill
description: >-
  Add one skill or a whole bundle of skills to Command HQ from inside the claude+
  PTY: scaffold each `.claude/skills/<name>/SKILL.md`, register it in the org
  catalog (direct REST with the claude+ device token), then opt the current
  project in and sync so it's usable this session. Use when the user says
  "/hq-add-skill", "add a skill", "add a bundle of skills", "register a skill in
  HQ", or "enable a skill on this project".
---

# /hq-add-skill

Author a Command HQ skill — one or a whole bundle — register it in the **org
catalog**, then run the full round trip so it's live on the project and usable in
this session. Runs in the developer's claude+ session, inside a connected repo.

The catalog model (org scope, admin-gated writes, fork-on-edit, the seed-path
fallback) and the shared **resolve → enable → sync → verify** back half live in
[`hq-catalog-opt-in.md`](../../../docs/superpowers/hq-catalog-opt-in.md). This
skill is the skill-specific front half plus that round trip.

## 1 · Decide: single skill or a bundle

- **Single skill** — "add a skill that does X" → one
  `.claude/skills/<name>/SKILL.md`.
- **Bundle** — "add a bundle", "add these N skills", a themed list → one
  `SKILL.md` per member **plus** a `kind:"bundle"` record whose `members` are the
  member names (bundles nest; resolution is transitive).

If ambiguous, ask: "one skill, or a bundle of several?"

## 2 · If the prompt carried a local install doc, run it

When the user pasted real install instructions for an external tool (a
`git clone … && ./setup`, a CLAUDE.md edit, an `npm install` — gstack is the
canonical example), catalog registration is only half the ask. Also perform the
local install, no questions: run the commands verbatim, make the CLAUDE.md edits
the doc describes, and commit just those changes with the developer's own `git`
(conventional message, never `--no-verify`). Report what you ran; only pause if a
step genuinely fails.

## 3 · Scaffold the SKILL.md file(s)

For each skill write `.claude/skills/<name>/SKILL.md` (kebab-case `<name>`):

```
---
name: <name>
description: >-
  <what it does> … Use when the user says "/<name>", "…".
---

# /<name>

<body: what it does, when it runs, the concrete steps/commands>
```

Never overwrite an existing skill file — if `<name>` exists, stop and tell the user.

> **Bundle name collision — check before registering a bundle.** `claude+ sync` is
> bidirectional and upserts catalog records by name from local skills, so a bundle
> whose name equals a local skill dir gets **clobbered** (its members wiped). If
> `~/.claude/skills/<bundle>` or `.claude/skills/<bundle>` exists, pick a distinct
> bundle name or remove the local copy first. The server also rejects this now
> (`409`); see [the reference](../../../docs/superpowers/hq-catalog-opt-in.md#bundle-vs-local-skill-name-collision-registration-time-hq-add-skill).

## 4 · Register into the org catalog

With `HQ="$CLAUDE_PLUS_API_URL"` and `TOK="$(sed -n 2p ~/.claude-plus/credentials)"`:

```
# each member skill (server forces scope + stamps createdBy)
POST <HQ>/skills   { "name":"<name>", "kind":"skill", "description":"<desc>",
                     "source":"custom", "body":"<SKILL.md text>" }
# the bundle (members reference the skill names)
POST <HQ>/skills   { "name":"<bundle>", "kind":"bundle", "description":"<desc>",
                     "source":"custom", "members":["<a>","<b>"] }
# …or add one member to an existing bundle
POST <HQ>/skills/<bundle>/members   { "member":"<name>" }
```

`201`/`200` = live immediately. `403` = your profile isn't an org admin · `401` =
deployed API predates device-token writes · `409` = a canonical built-in (or a
bundle you'd clobber). For `403`/`401`, use the
[seed-path fallback](../../../docs/superpowers/hq-catalog-opt-in.md#fallback-the-seed-path-bootstrap--not-an-admin).

## 5 · Opt the project in, sync, and verify — the round trip

Follow [resolve → enable → sync → verify](../../../docs/superpowers/hq-catalog-opt-in.md#resolve--enable--sync--verify)
in full. In short:

1. **Resolve** `$CLAUDE_PLUS_PROJECT_ID`; `GET $HQ/projects/$PID`. If it 404s,
   derive the candidate from `git remote get-url origin` (HQ's exact slug
   algorithm) and probe that, then the bare-repo slug. Prefer the `gh/`-prefixed
   record if more than one resolves; STOP only if none do.
2. **Enable:** `POST $HQ/projects/$PID/skills/<name>` per skill, or
   `POST $HQ/projects/$PID/bundles/<bundle>` for the whole bundle.
3. **Sync:** `claude+ sync`.
4. **Verify:** assert `pulled ≥ 1` (or the new `~/.claude+/skills/<name>/SKILL.md`
   exists). `pulled 0` after a successful enable = stale daemon binding — restart
   it by re-running `claude+` in this repo, then sync again.
5. **Report:** `enabled <N> · pulled <N> · root now <M>` — never "toggle it in the
   web app."

If `$CLAUDE_PLUS_PROJECT_ID` is empty or no candidate resolves, the repo isn't a
connected HQ project: registration in the org catalog (plus any local install in
§2) is the deliverable — say so and finish.
