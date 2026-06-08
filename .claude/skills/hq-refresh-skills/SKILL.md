---
name: hq-refresh-skills
description: >-
  Refresh this machine's local skills into the isolated ~/.claude+ registry so the
  claude+ session picks them up without restarting. Two sources: (1) the connected
  repo's own .claude/skills/ — so skills you just edited locally go live
  immediately, WITHOUT waiting for a Command HQ deploy/re-seed — and (2) the
  Command HQ org catalog (changed/newly-published items for the project's enabled
  set). Pull-focused and additive; your personal ~/.claude is never touched. Use
  when the user says "/hq-refresh-skills", "refresh my skills", "pull the latest
  skills from HQ", "did any skills change in command hq", "pick up my local skill
  edits", or after editing a skill in the repo or an admin publishing one.
---

# /hq-refresh-skills

Refresh the skills the claude+ session actually reads (the isolated `~/.claude+`
root) from two sources: the **connected repo's own `.claude/skills/`** and the
**Command HQ org catalog**. Runs in the developer's claude+ session. Both sources
write only into `~/.claude+` — your personal `~/.claude` is never touched.

> **Why the repo source matters.** claude+ reads skills from `~/.claude+`, not
> from the repo working tree. Editing a skill in the repo (`.claude/skills/...`)
> does **not** change what a running claude+ session sees, and pushing the edit to
> GitHub only reaches the HQ catalog after a **deploy + re-seed**. This step closes
> that gap: it copies your local repo edits straight into `~/.claude+` so they go
> live on the next turn — no deploy, no round-trip through HQ.

## What it does

1. **Reconcile the connected repo's local skills (no deploy needed).** Resolve the
   repo root (`git rev-parse --show-toplevel`). For every
   `.claude/skills/<name>/SKILL.md` (and `.claude/agents/<name>.md`) in the repo,
   copy it into `~/.claude+/skills/<name>/SKILL.md` (resp. `~/.claude+/agents/`)
   whenever the repo copy is new or its contents differ from what's already in
   `~/.claude+`. The repo working copy is **authoritative for this machine** — you
   are the one editing it — so on a difference the repo copy wins locally; report
   each one updated. Resolve `~/.claude+` via the same isolated config root claude+
   launches Claude against (`CLAUDE_CONFIG_DIR`, default `~/.claude+`), never
   `~/.claude`.
2. **Check for HQ drift.** Fetch the org catalog (`GET /skills`, `GET /agents`),
   filter it to the linked project's enabled set (`enabledSkills` /
   `enabledAgents`), and compare that effective set against `~/.claude+`. If
   nothing changed (and step 1 also copied nothing), report "already up to date"
   and stop.
3. **Pull HQ changes.** For anything new or updated in HQ that the repo step did
   not already supply, materialize it into `~/.claude+/skills/<name>/SKILL.md`
   (agents into `~/.claude+/agents/`). A skill present in the repo step is **not**
   overwritten by an older HQ copy — local repo edits take precedence on the
   developer's own machine.
4. **Report.** Print both counts, e.g. `repo: updated 2 · HQ: pulled 1`, so you
   know which skills are now live.

## How to run

Two moves, both landing in `~/.claude+`:

1. **Repo → `~/.claude+`** (step 1). Copy the connected repo's
   `.claude/skills/**` (and `.claude/agents/**`) into the isolated config root,
   creating `~/.claude+/skills/<name>/` as needed and overwriting only when the
   repo copy differs. Use the platform's file tools (e.g. `cp -r` on Unix,
   `Copy-Item -Recurse -Force` on Windows) against the resolved `~/.claude+`.
2. **HQ → `~/.claude+`** (steps 2–3):

   ```bash
   claude+ sync-skills
   ```

   `sync-skills` performs the HQ-side one-shot reconcile; report its printed
   `pulled N` count.

A freshly reconciled skill is usable on the **next turn** (claude+ re-reads skills
from `~/.claude+`); no rebuild of the claude+ binary is involved — skills are
runtime files, not compiled in.

## When nothing changes

- `repo: updated 0 · HQ: pulled 0` → `~/.claude+` already matches both the repo and
  HQ; nothing to do.
- `not signed in to HQ` → the repo step (step 1) still runs and can update
  `~/.claude+` from your local edits; run `claude+ login` first only if you also
  want the HQ pull.
- For the HQ pull, a `differs` item (the same skill edited in both `~/.claude+` and
  HQ) is reported, not overwritten. Note the repo step has already run, so if the
  difference came from your repo edit that is the intended state — keep it; only
  delete the local copy and re-run if you want to adopt HQ's version instead.

## Relation to /hq-update-skills

`/hq-update-skills` does the **bidirectional** HQ reconcile (pulls the project's
enabled changes AND pushes your local-only skills up to the org catalog) — use it
when you want your edits to reach **other people** through HQ. `/hq-refresh-skills`
is **local-first and pull-only**: it makes _your own_ `~/.claude+` reflect the
connected repo's `.claude/skills/` and HQ's latest, without publishing anything.
Reach for `/hq-refresh-skills` right after editing a skill in the repo so the
running claude+ session picks it up immediately; reach for `/hq-update-skills` when
you're ready to share that edit org-wide (which still requires the normal HQ
publish/seed path to land in the catalog).
