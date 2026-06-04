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

Runs the wrapper's one-shot reconcile (`claude+ sync-skills`), which:

1. Fetches the **whole org catalog** (`GET /skills`, `GET /agents` — no scope
   resolution; every item is org-scoped), then resolves this repo's linked project
   and reads its `enabledSkills` / `enabledAgents` (`GET /projects/:id`).
2. Computes the **effective set** = catalog items whose name is in the project's
   `enabledSkills` (skills) or `enabledAgents` (agents). An enabled agent's own
   skills are already in `enabledSkills` (the server union-added them when the agent
   was enabled), so no extra expansion is needed here.
3. **Pulls** any effective item missing locally into
   `~/.claude+/skills/<name>/SKILL.md` (and agents into `~/.claude+/agents/`).
4. **Pushes** any local-only skill up to the **org catalog** (admin-gated; a
   non-admin push is rejected).

This is **not** the old org+user+project narrowest-wins resolution — it is a flat
org catalog filtered by one project's opt-in. It writes only into the isolated
`~/.claude+` root — never your personal `~/.claude` — so bundled product skills
never pollute your normal Claude Code dataset. Items that exist on both sides with
different content are reported as `differs` and left for you to resolve (a sync
never silently overwrites an edited definition).

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
- A catalog skill you expected didn't pull — the linked project hasn't **opted in**
  to it. Enable it on the project first (`POST /projects/:id/skills/:name`, or
  enable an agent that brings it, or the HQ project Skills/Agents tabs), then re-run.
- `not signed in to HQ` — run `claude+ login` first, then retry.
- A `differs` item — surface it to the user; they decide whether to keep the
  local edit or adopt HQ's version (re-run after deleting the local file to adopt
  HQ's).
