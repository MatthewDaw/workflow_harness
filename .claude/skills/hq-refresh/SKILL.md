---
name: hq-refresh
description: >-
  Refresh a project's skills, agents, AND MCP servers down to this machine's
  isolated ~/.claude+ registry so the claude+ session picks them up without
  restarting. Two sources: (1) the connected repo's own .claude/skills/,
  .claude/agents/, and .mcp.json — so things you just edited locally go live
  immediately, WITHOUT waiting for a Command HQ deploy/re-seed — and (2) the
  Command HQ org catalog (changed/newly-published skills, agents, and MCP servers
  for the project's enabled set). Pull-focused and additive; your personal
  ~/.claude is never touched. Use when the user says "/hq-refresh", "refresh my
  skills", "refresh agents and mcp servers", "pull the latest from HQ", "did
  anything change in command hq", "pick up my local edits", or after editing a
  skill/agent/MCP server in the repo or an admin publishing one.
---

# /hq-refresh

Refresh everything the claude+ session reads for the connected project — **skills,
agents, and MCP servers** — into the isolated `~/.claude+` root, from two sources:
the **connected repo's own `.claude/` + `.mcp.json`** and the **Command HQ org
catalog**. Runs in the developer's claude+ session. Both sources write only into
`~/.claude+` — your personal `~/.claude` is never touched.

> **Why the repo source matters.** claude+ reads its config from `~/.claude+`, not
> from the repo working tree. Editing a skill, agent, or MCP server in the repo
> (`.claude/skills/...`, `.claude/agents/...`, `.mcp.json`) does **not** change what
> a running claude+ session sees, and pushing the edit to GitHub only reaches the HQ
> catalog after a **deploy + re-seed**. This step closes that gap: it copies your
> local repo edits straight into `~/.claude+` so they go live on the next turn — no
> deploy, no round-trip through HQ.

## What it does

All three kinds — skills, agents, and MCP servers — are reconciled in the **same
pass**. (`claude+ sync-skills` is kind-generic despite its name; it pulls skills,
agents, and MCP servers together.)

1. **Reconcile the connected repo's local config (no deploy needed).** Resolve the
   repo root (`git rev-parse --show-toplevel`), then copy the repo's authoritative
   copies into the isolated config root whenever they are new or differ:
   - `.claude/skills/<name>/SKILL.md` → `~/.claude+/skills/<name>/SKILL.md`
   - `.claude/agents/<name>.md` → `~/.claude+/agents/<name>.md`
   - each `mcpServers[<name>]` entry in the repo's `.mcp.json` → merged into
     `~/.claude+/.mcp.json` under the same key (other servers and unrelated
     top-level keys in that file are preserved).

   The repo working copy is **authoritative for this machine** — you are the one
   editing it — so on a difference the repo copy wins locally; report each one
   updated. Resolve `~/.claude+` via the same isolated config root claude+ launches
   Claude against (`CLAUDE_CONFIG_DIR`, default `~/.claude+`), never `~/.claude`.
2. **Check for HQ drift.** Fetch the org catalog (`GET /skills`, `GET /agents`,
   `GET /mcp-servers`), filter it to the linked project's enabled set
   (`enabledSkills` / `enabledAgents` / `enabledMcpServers`, from
   `GET /projects/:id`), and compare that effective set against `~/.claude+`. If
   nothing changed (and step 1 also copied nothing), report "already up to date"
   and stop.
3. **Pull HQ changes.** For anything new or updated in HQ that the repo step did
   not already supply, materialize it into `~/.claude+`: skills into
   `~/.claude+/skills/<name>/SKILL.md`, agents into `~/.claude+/agents/`, MCP
   servers merged into `~/.claude+/.mcp.json` under `mcpServers[<name>]` (the
   catalog's `transport` → the on-disk `type`). Anything the repo step already
   supplied is **not** overwritten by an older HQ copy — local repo edits take
   precedence on the developer's own machine.
4. **Report.** Print the counts per kind so you know what's now live, e.g.
   `repo: updated 2 · HQ: pulled 1` covering skills + agents + MCP servers.

## How to run

Two moves, both landing in `~/.claude+`:

1. **Repo → `~/.claude+`** (step 1). Copy the connected repo's `.claude/skills/**`
   and `.claude/agents/**` into the isolated config root, and merge the repo's
   `.mcp.json` `mcpServers` entries into `~/.claude+/.mcp.json`. Create
   `~/.claude+/skills/<name>/` etc. as needed and overwrite only when the repo copy
   differs. Use the platform's file tools (e.g. `cp -r` on Unix, `Copy-Item
   -Recurse -Force` on Windows) against the resolved `~/.claude+`; for `.mcp.json`,
   merge rather than overwrite so unrelated servers survive.
2. **HQ → `~/.claude+`** (steps 2–3):

   ```bash
   claude+ sync-skills
   ```

   `sync-skills` performs the HQ-side one-shot reconcile for **all three kinds**
   (skills, agents, and MCP servers). Report its printed `pulled N` count. Note the
   CLI message says only "skills synced" even though agents and MCP servers are
   included in the same pass.

A freshly reconciled skill, agent, or MCP server is usable on the **next turn**
(claude+ re-reads `~/.claude+`); no rebuild of the claude+ binary is involved —
these are runtime files, not compiled in.

## When nothing changes

- `repo: updated 0 · HQ: pulled 0` → `~/.claude+` already matches both the repo and
  HQ; nothing to do.
- `not signed in to HQ` → the repo step (step 1) still runs and can update
  `~/.claude+` from your local edits; run `claude+ login` first only if you also
  want the HQ pull.
- For the HQ pull, a `differs` item (the same skill/agent/MCP server edited in both
  `~/.claude+` and HQ) is reported, not overwritten. Note the repo step has already
  run, so if the difference came from your repo edit that is the intended state —
  keep it; only delete the local copy and re-run if you want to adopt HQ's version
  instead.

## Relation to /hq-update-skills

`/hq-update-skills` does the **bidirectional** HQ reconcile (pulls the project's
enabled changes AND pushes your local-only skills/agents/MCP servers up to the org
catalog) — use it when you want your edits to reach **other people** through HQ.
`/hq-refresh` is **local-first and pull-only**: it makes _your own_ `~/.claude+`
reflect the connected repo's `.claude/` + `.mcp.json` and HQ's latest, without
publishing anything. Reach for `/hq-refresh` right after editing a skill, agent, or
MCP server in the repo so the running claude+ session picks it up immediately; reach
for `/hq-update-skills` when you're ready to share that edit org-wide (which still
requires the normal HQ publish/seed path to land in the catalog).
