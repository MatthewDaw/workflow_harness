---
name: hq-sync
description: >-
  Sync a project's skills, agents, AND MCP servers down to this machine's
  isolated, PER-PROJECT claude+ registry so the claude+ session picks them up
  without restarting. Each repo gets its own config root
  (~/.claude+/roots/<project>/) holding ONLY that project's enabled set — no
  cross-project union. Two sources: (1) the connected repo's own .claude/skills/,
  .claude/agents/, and .mcp.json — so things you just edited locally go live
  immediately, WITHOUT waiting for a Command HQ deploy/re-seed — and (2) the
  Command HQ org catalog (changed/newly-published skills, agents, and MCP servers
  for the project's enabled set). Pull-focused and additive; your personal
  ~/.claude is never touched. Use when the user says "/hq-sync", "sync my skills",
  "refresh my skills", "sync agents and mcp servers", "pull the latest from HQ",
  "did anything change in command hq", "pick up my local edits", or after editing
  a skill/agent/MCP server in the repo or an admin publishing one.
---

# /hq-sync

Sync everything the claude+ session reads for the connected project — **skills,
agents, and MCP servers** — into that project's own isolated config root, from two
sources: the **connected repo's own `.claude/` + `.mcp.json`** and the **Command HQ
org catalog**. Runs in the developer's claude+ session. Both sources write only
into the claude+ root — your personal `~/.claude` is never touched.

> **Per-project isolation.** claude+ launches each repo's session against its OWN
> config root, `~/.claude+/roots/<projectSlug>/` (the slug is derived from the repo
> path). A sync for `repoA` writes only `repoA`'s root, and `repoA`'s session reads
> only `repoA`'s enabled set — never the union of every project you've ever synced.
> The shared `~/.claude+` base holds just your Claude auth/identity, which each
> project root keeps in sync automatically (so one `claude+ login` covers them all).

> **Why the repo source matters.** claude+ reads its config from the project root,
> not from the repo working tree. Editing a skill, agent, or MCP server in the repo
> (`.claude/skills/...`, `.claude/agents/...`, `.mcp.json`) does **not** change what
> a running claude+ session sees, and pushing the edit to GitHub only reaches the HQ
> catalog after a **deploy + re-seed**. This step closes that gap: it copies your
> local repo edits straight into the project root so they go live on the next turn —
> no deploy, no round-trip through HQ.

## What it does

All three kinds — skills, agents, and MCP servers — are reconciled in the **same
pass**, into this project's root. (`claude+ sync-skills` is kind-generic despite
its name; it pulls skills, agents, and MCP servers together, scoped to the linked
project.)

1. **Reconcile the connected repo's local config (no deploy needed).** Resolve the
   repo root (`git rev-parse --show-toplevel`), then copy the repo's authoritative
   copies into THIS project's config root (`~/.claude+/roots/<projectSlug>/`)
   whenever they are new or differ:
   - `.claude/skills/<name>/SKILL.md` → `<root>/skills/<name>/SKILL.md`
   - `.claude/agents/<name>.md` → `<root>/agents/<name>.md`
   - each `mcpServers[<name>]` entry in the repo's `.mcp.json` → merged into
     `<root>/.mcp.json` under the same key (other servers and unrelated top-level
     keys in that file are preserved).

   The repo working copy is **authoritative for this machine** — you are the one
   editing it — so on a difference the repo copy wins locally; report each one
   updated. The project root is the same `CLAUDE_CONFIG_DIR` claude+ launches Claude
   against for this repo, never `~/.claude`.
2. **Check for HQ drift.** Fetch the org catalog (`GET /skills`, `GET /agents`,
   `GET /mcp-servers`), filter it to the linked project's enabled set
   (`enabledSkills` / `enabledAgents` / `enabledMcpServers`, from
   `GET /projects/:id`), and compare that effective set against this project's root.
   If nothing changed (and step 1 also copied nothing), report "already up to date"
   and stop.
3. **Pull HQ changes.** For anything new or updated in HQ that the repo step did
   not already supply, materialize it into this project's root: skills into
   `<root>/skills/<name>/SKILL.md`, agents into `<root>/agents/`, MCP servers merged
   into `<root>/.mcp.json` under `mcpServers[<name>]` (the catalog's `transport` →
   the on-disk `type`). Anything the repo step already supplied is **not**
   overwritten by an older HQ copy — local repo edits take precedence on the
   developer's own machine.
4. **Report.** Print the counts per kind so you know what's now live, e.g.
   `repo: updated 2 · HQ: pulled 1` covering skills + agents + MCP servers.

## How to run

Two moves, both landing in this project's root:

1. **Repo → project root** (step 1). Copy the connected repo's `.claude/skills/**`
   and `.claude/agents/**` into `~/.claude+/roots/<projectSlug>/`, and merge the
   repo's `.mcp.json` `mcpServers` entries into that root's `.mcp.json`. Create
   subdirectories as needed and overwrite only when the repo copy differs; for
   `.mcp.json`, merge rather than overwrite so unrelated servers survive.
2. **HQ → project root** (steps 2–3):

   ```bash
   claude+ sync-skills
   ```

   `sync-skills` performs the HQ-side one-shot reconcile for **all three kinds**
   (skills, agents, and MCP servers), into the linked project's root. Report its
   printed `pulled N` count. Note the CLI message says only "skills synced" even
   though agents and MCP servers are included in the same pass.

A freshly reconciled skill, agent, or MCP server is usable on the **next turn**
(claude+ re-reads the project root); no rebuild of the claude+ binary is involved —
these are runtime files, not compiled in.

## When nothing changes

- `repo: updated 0 · HQ: pulled 0` → this project's root already matches both the
  repo and HQ; nothing to do.
- `not signed in to HQ` → the repo step (step 1) still runs and can update the
  project root from your local edits; run `claude+ login` first only if you also
  want the HQ pull.
- For the HQ pull, a `differs` item (the same skill/agent/MCP server edited in both
  the project root and HQ) is reported, not overwritten. Note the repo step has
  already run, so if the difference came from your repo edit that is the intended
  state — keep it; only delete the local copy and re-run if you want to adopt HQ's
  version instead.

## Relation to /hq-update-skills

`/hq-update-skills` does the **bidirectional** HQ reconcile (pulls the project's
enabled changes AND pushes your local-only skills/agents/MCP servers up to the org
catalog) — use it when you want your edits to reach **other people** through HQ.
`/hq-sync` is **local-first and pull-only**: it makes _this project's own_ root
reflect the connected repo's `.claude/` + `.mcp.json` and HQ's latest, without
publishing anything. Reach for `/hq-sync` right after editing a skill, agent, or
MCP server in the repo so the running claude+ session picks it up immediately; reach
for `/hq-update-skills` when you're ready to share that edit org-wide (which still
requires the normal HQ publish/seed path to land in the catalog).
