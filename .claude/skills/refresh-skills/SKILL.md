---
name: refresh-skills
description: >-
  Refresh this machine's local skills from Command HQ — pull down any skills that
  changed or were newly published in HQ into the isolated ~/.claude+ registry, so
  the claude+ session picks them up without restarting. Pull-focused: it checks HQ
  for drift and materializes the latest, leaving your personal ~/.claude
  untouched. Use when the user says "/refresh-skills", "refresh my skills",
  "pull the latest skills from HQ", "did any skills change in command hq", or
  after an admin publishes/updates a skill or bundle.
---

# /refresh-skills

Pull the latest skills from Command HQ into this machine. Runs in the developer's
claude+ session.

## What it does

1. **Check for drift.** Compares HQ's effective skill/agent set (org → user →
   project, narrowest wins) against the local `~/.claude+` registry. If nothing
   changed, report "already up to date" and stop.
2. **Pull changes.** For anything new or updated in HQ, materialize it into
   `~/.claude+/skills/<name>/SKILL.md` (agents into `~/.claude+/agents/`). Writes
   go only to the isolated `~/.claude+` root — never your personal `~/.claude`.
3. **Report.** Print what was pulled (e.g. `pulled 2`), so you know which skills
   are now available.

## How to run

```bash
claude+ sync-skills
```

`sync-skills` performs the one-shot reconcile that backs this skill. It pulls
HQ-only / changed items down; report the printed `pulled N` count. A freshly
pulled skill is usable on the next turn (claude+ reads skills from `~/.claude+`).

## When nothing changes

- `pulled 0` → already in sync with HQ; nothing to do.
- `not signed in to HQ` → run `claude+ login` first, then retry.
- A `differs` item (edited both locally and in HQ) is reported, not overwritten —
  surface it to the user; delete the local copy and re-run to adopt HQ's version.

## Relation to /update-skills

`/update-skills` does the **bidirectional** reconcile (pulls HQ changes AND pushes
your local-only skills up to your user scope). `/refresh-skills` is the
**pull-only** framing — "just get me HQ's latest" — for when you don't want to
publish anything, only consume updates. Both use `claude+ sync-skills` under the
hood; the pushes are a no-op when you have nothing local-only.
