---
name: update-skills
description: >-
  Run inside the claude+ PTY to refresh the locally-available skills from Command
  HQ. It performs a one-shot reconcile of HQ's effective (org + user + project)
  skill/agent registry into the isolated ~/.claude+ config root claude+ launches
  Claude against — pulling newly-published bundled skills and pushing your local
  ones — without ever touching your personal ~/.claude. Use when the user says
  "/update-skills", "update my skills", "sync skills", "pull the latest skills",
  or after an admin publishes a new skill bundle in HQ.
---

# /update-skills

On-demand refresh of the skills claude+ makes available to Claude. claude+
already auto-syncs once per new session; this skill forces that refresh now, so a
just-published bundled skill is usable without restarting a session.

## What it does

Runs the wrapper's one-shot reconcile (`claude+ sync-skills`), which:

1. Fetches HQ's **effective** skill/agent set for your scopes (org → user →
   project; narrowest scope wins).
2. **Pulls** any HQ skill missing locally into `~/.claude+/skills/<name>/SKILL.md`
   (and agents into `~/.claude+/agents/`).
3. **Pushes** any local-only skill up to your HQ **user** scope.

It writes only into the isolated `~/.claude+` root — never your personal
`~/.claude` — so bundled product skills never pollute your normal Claude Code
dataset. Items that exist on both sides with different content are reported as
`differs` and left for you to resolve (a sync never silently overwrites an edited
definition).

## How to run

In the developer's claude+ session, inside a connected repo (the device token +
HQ endpoint established at `claude+ login` are reused):

```bash
claude+ sync-skills
```

Report the printed result to the user, e.g. `skills synced: pulled 2, pushed 0`.
A freshly-pulled skill is available immediately for the next turn (claude+'s
inner Claude reads skills from `~/.claude+`).

## When nothing happens

- `pulled 0, pushed 0` — already in sync; nothing to do.
- `not signed in to HQ` — run `claude+ login` first, then retry.
- A `differs` item — surface it to the user; they decide whether to keep the
  local edit or adopt HQ's version (re-run after deleting the local file to adopt
  HQ's).
