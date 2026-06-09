---
name: hq-skill-idea-iterate
description: >-
  Human-in-the-loop fold of a skill's accumulated ideas back into the skill body.
  Run inside the claude+ PTY when you want to improve a skill from the corroborated
  ideas the loop attached to it: it fetches that skill's ideas from Command HQ
  (GET /skills/{name}/ideas), shows you the corroborated ones with their session
  counts and provenance, lets you pick which to fold, drafts a merged revision of
  the skill body, writes it as a forked variant (PUT /skills/{name} — built-ins
  fork rather than overwrite), runs the golden-set regression if present, then —
  after you approve — promotes the variant to the org default (POST /skills/{name}/promote)
  and marks the idea folded (POST /skills/{name}/ideas/{ideaId}/fold). Use when the
  user says "/hq-skill-idea-iterate", "iterate this skill", "fold the ideas into
  <skill>", "promote a skill idea", "merge skill ideas", "improve <skill> from its
  ideas", "apply the corroborated learnings to <skill>", or after Command HQ shows
  a skill has accumulated corroborated ideas worth folding.
---

# /hq-skill-idea-iterate

The deliberate, human-gated path that turns a skill's accumulated **ideas** —
session-derived, independently corroborated learnings the loop attached to the
skill — into an improved skill body. It never auto-edits: a person picks what to
fold, the agent drafts the merge, and a person promotes it. This mirrors the
research the loop is built on (curated folds beat blind overwrites), so the
revision is written but the org-wide default pointer only moves on explicit
approval.

## What it does

Walks one skill through fetch → pick → draft → write → regress → promote → mark:

1. **Fetch** the skill's ideas — `GET /skills/{name}/ideas` returns **every** idea
   for the skill (corroborated, uncorroborated, and already-folded history), each
   decorated with `corroborationCount`, `status`, `foldedIntoRev`, and the full
   `sources` provenance (distinct `sessionId`s with `snippet`/`projectId`/`repoId`).
2. **Pick** — surface the **open** ideas ordered strongest-first (corroboration
   then recency) and let the human choose which one(s) to fold. Bias to the
   corroborated ones (`corroborationCount ≥ 2`); show uncorroborated and folded
   history for context but call them out as not-yet-corroborated / already-in-body.
   The human is also the merge step the strict same-lesson matcher skips — point
   out near-duplicate open ideas so they can be folded together.
3. **Draft** — the agent reads the current skill body and the chosen idea's `text`
   (a synthesized, skill-ready learned concept, not raw transcript) and writes a
   **merged revision** of the body that integrates the lesson cleanly. Show the
   human the diff and get sign-off on the draft before writing anything.
4. **Write the revision** — `PUT /skills/{name}` with the merged body. A catalog
   `hq-*`/built-in base is git-seed owned, so this **forks a variant** (set
   `repoId` + `authorUserId`) rather than overwriting the base; the server mints a
   new immutable revision and leaves the org-wide `#TRUE` pointer untouched.
5. **Golden-set regression** — if the skill has golden cases, replay them against
   the candidate revision (the fold flow records each folded lesson as a
   before→after expectation). Surface any regression to the human; it is advisory
   — the human is the gate — but a flagged case means a prior fold may be undone, so
   stop and confirm before promoting. If there are no golden cases, note that and
   continue.
6. **Promote** — only after the human approves: `POST /skills/{name}/promote` with
   the forked `{ variantId, rev }` repoints the org-wide `#TRUE` to the new
   revision. Promotion never edits or deletes a variant; it only moves the pointer.
7. **Mark folded** — `POST /skills/{name}/ideas/{ideaId}/fold` flips the idea's
   `status` to `folded` and stamps `foldedIntoRev`, so it drops out of the live
   candidate-learnings block (its lesson is now in the body) but persists as HQ
   history. This write is optimistic-concurrency guarded: a `409` means a
   corroboration landed between your read and the mark — re-read the idea and
   re-fold against the fresh version.

A fold can also be performed in one call — `POST /skills/{name}/ideas/{ideaId}/fold`
accepts the drafted `{ body, description?, repoId, authorUserId }`, writes the
forked revision **and** marks the idea folded atomically, leaving `#TRUE` for the
separate promote. Use that when you want the revision write and the mark coupled;
use the explicit `PUT` then `fold` when you want to draft/regress/show-diff before
the idea is touched. Either way, **promote is always a separate, human-approved
step.**

## How to run

In the developer's claude+ session, inside a connected repo (the device token +
HQ endpoint established at `claude+ login` are reused).

**Getting the `projectId` — never guess it.** claude+ injects the authoritative HQ
project id into the session as `$CLAUDE_PLUS_PROJECT_ID` (alongside `$CLAUDE_PLUS_REPO`
and `$CLAUDE_PLUS_API_URL`), mirrored in `$CLAUDE_CONFIG_DIR/hq-project.json`. Use
`$CLAUDE_PLUS_PROJECT_ID` directly as the `projectId` and `$CLAUDE_PLUS_REPO` as the
fork's `repoId` — do NOT derive, slug, or probe candidate ids. If
`$CLAUDE_PLUS_PROJECT_ID` is empty, this session is not running under claude+ — say
so and stop; do not guess.

With `HQ="$CLAUDE_PLUS_API_URL"` and `TOK="$(sed -n 2p ~/.claude-plus/credentials)"`,
and `<name>` the skill base name you're iterating:

```bash
# 1 · Fetch every idea for the skill (decorated with corroborationCount + status)
curl -s -H "Authorization: Bearer $TOK" "$HQ/skills/<name>/ideas"

# (Human picks an open, ideally corroborated idea → $IDEA_ID. Agent drafts the
#  merged body from the current skill body + the idea's `text`, and gets sign-off.)

# 2 · Write the merged revision as a FORKED variant (built-ins never overwrite).
#     repoId = $CLAUDE_PLUS_REPO so the fork lands on this repo's variant line.
curl -s -X PUT -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  "$HQ/skills/<name>" \
  -d '{ "name":"<name>", "kind":"skill", "body":"<merged SKILL.md body>",
        "description":"<unchanged or refined>",
        "repoId":"'"$CLAUDE_PLUS_REPO"'", "authorUserId":"<your userId>" }'
# Response carries the new { variantId, version } — capture them as $VARIANT/$REV.

# 3 · Run golden-set regression if the skill has cases (advisory; human is the gate).

# 4 · PROMOTE — only after the human approves the draft + regression result.
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  "$HQ/skills/<name>/promote" \
  -d '{ "variantId":"'"$VARIANT"'", "rev":'"$REV"' }'

# 5 · Mark the idea folded into that revision.
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  "$HQ/skills/<name>/ideas/$IDEA_ID/fold" \
  -d '{ "body":"<merged SKILL.md body>",
        "repoId":"'"$CLAUDE_PLUS_REPO"'", "authorUserId":"<your userId>" }'
```

Status codes: `200` = done · `403` = your profile isn't an org admin (fold,
promote, and the revision write all require the same skill-edit authority,
resolved server-side from your PROFILE) · `401` = deployed API predates
device-token skill writes · `404` = the skill or idea doesn't exist on this org ·
`409` on fold = a built-in fold without a fork identity, an already-folded idea, or
a corroboration that raced the mark (re-read and re-fold). For `403`/`401`, the
caller isn't authorized to move the org catalog — report it and stop; do not claim
the skill was promoted when it wasn't.

Report the result to the user, e.g.
`folded idea <id> into <name> rev <REV> · promoted · marked folded`.

## Then seed the deployed catalog so it's LIVE on the website

The promote moves the org-wide `#TRUE` in the deployed `harness` table directly, so
the next `claude+ sync` on any machine pulls the promoted revision. But the
**website's Skills tab** and other machines that pick skills up via the seed path
read the seeded catalog, and the canonical base body still lives in
`catalog/skills/<name>/SKILL.md` on `main`. After folding into a skill you maintain
in the repo, finish the loop like `hq-update-skills`: commit + push the edited
`catalog/skills/<name>/SKILL.md` to `main`, then **re-seed every org** so the live
catalog matches.

```bash
npm run build -w @harness/backend \
  && HARNESS_TABLE=harness AWS_REGION=us-east-1 node infra/scripts/seed-all-orgs.mjs
```

- `seed-all-orgs.mjs` seeds **every** org (not a single `SEED_ORG`), upserting each
  repo `catalog/skills/*/SKILL.md` and the bundles in `catalog/skills/bundles.json`.
  It updates only the **base** variant and **never clobbers a promoted `#TRUE`**, so
  re-seeding is safe after a promote — the promoted fork stays the org default.
- **Requires local AWS credentials** for the `harness` table. If the seed fails
  with a credentials / AccessDenied error, report it and fall back to the
  `seed-skills` GitHub Action — and do not claim the website is updated when the
  seed did not succeed.

## When nothing happens

- `ideas` returns an empty list — the skill has no attached ideas yet (no
  corroborated session-derived learnings have landed on it). Nothing to fold.
- Only uncorroborated ideas (`corroborationCount < 2`) — they exist but haven't
  been independently corroborated by `K` distinct sessions. Surface them for
  context but do not fold a single-session idea without the human's explicit call.
- All open ideas already `folded` — the lessons are in the body; the history view
  shows them but there is nothing live to iterate.
- `not signed in to HQ` / empty `$TOK` — run `claude+ login` first, then retry.
