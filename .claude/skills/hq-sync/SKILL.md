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
  for the project's enabled set). Every synced item is set up END TO END — skill
  directories in full, agent definitions plus the skills they depend on, and MCP
  servers with their launch dependency, required env, and OAuth handshake actually
  completed — and plugin registration is finalized so new commands autocomplete. A
  final verification gate fails loudly if any enabled item did not fully land. It
  is a TIGHT MIRROR, not additive: a skill or agent removed from the project's
  enabled set is DELETED from the project root, so the local claude+ set always
  matches Command HQ. The prune only ever deletes inside the per-project root, and
  only after a successful HQ fetch (a 401/transient error never wipes anything);
  your personal ~/.claude and the repo's own .claude project skills are left alone.
  Use when the user says "/hq-sync", "sync my skills",
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
pass**, into this project's root, and each is set up **end to end** (see the
contract below). (`claude+ sync-skills` is kind-generic despite its name; it pulls
skills, agents, and MCP servers together, scoped to the linked project.)

1. **Reconcile the connected repo's local config (no deploy needed).** Resolve the
   repo root (`git rev-parse --show-toplevel`), then copy the repo's authoritative
   copies into THIS project's config root (`~/.claude+/roots/<projectSlug>/`)
   whenever they are new or differ:
   - `.claude/skills/<name>/**` → `<root>/skills/<name>/**` — copy the **whole skill
     directory**, not just `SKILL.md`. Scripts, templates, and resources a skill
     references at runtime live alongside it; copying only `SKILL.md` produces a
     skill that registers but breaks the moment it runs.
   - `.claude/agents/<name>.md` → `<root>/agents/<name>.md`.
   - each `mcpServers[<name>]` entry in the repo's `.mcp.json` → merged into the
     root's MCP config (see the MCP contract — claude+ reads servers from
     `<root>/.claude.json` `mcpServers`, not a standalone `.mcp.json`).

   The repo working copy is **authoritative for this machine** — you are the one
   editing it — so on a difference the repo copy wins locally; report each one
   updated. The project root is the same `CLAUDE_CONFIG_DIR` claude+ launches Claude
   against for this repo, never `~/.claude`.
2. **Check for HQ drift.** Fetch the org catalog (`GET /skills`, `GET /agents`,
   `GET /mcp-servers`), filter it to the linked project's enabled set
   (`enabledSkills` / `enabledAgents` / `enabledMcpServers`, from
   `GET /projects/:id`), and compare that effective set against this project's root.
   If nothing changed (and step 1 also copied nothing), skip to the verification
   gate (step 5) — it must still confirm the existing install is complete before
   reporting "already up to date".
3. **Pull HQ changes.** For anything new or updated in HQ that the repo step did
   not already supply, materialize it into this project's root: skill directories
   into `<root>/skills/<name>/`, agents into `<root>/agents/`, MCP servers into the
   root's MCP config (the catalog's `transport` → the on-disk `type`). Anything the
   repo step already supplied is **not** overwritten by an older HQ copy — local repo
   edits take precedence on the developer's own machine.
4. **Set each pulled item up end to end + finalize registration** (see contract
   below). This is the step that makes the install *complete* rather than just
   *present*: agents get the skills they depend on, MCP servers get their launch
   dependency + env + auth handshake, and every enabled plugin gets a registration
   entry so its commands/skills actually load and autocomplete.
5. **Verify, then report.** Run the verification gate (below) over the full enabled
   set. Only if every item is present AND usable do you report the per-kind counts,
   e.g. `repo: updated 2 · HQ: pulled 1 · agents 6 ok · mcp 3 authed · plugins 2
   registered`. If any enabled item is missing or not usable, FAIL loudly naming the
   item and the missing piece — never report success on a partial install.

## End-to-end setup contract

"Synced" is not "set up." For each kind, the item is only done when it is actually
**usable on the next turn**, not merely written to disk.

### Skills
- The whole `<root>/skills/<name>/` directory is present (SKILL.md + any sibling
  resources), and `SKILL.md` frontmatter has a `name` — without it the skill never
  registers and never appears in `/`-autocomplete.

### Agents (end to end)
- `<root>/agents/<name>.md` is present with valid frontmatter (`name`, `description`;
  plus `tools`/`model` if the source has them) — an agent missing `name`/`description`
  is silently dropped from the registry.
- **Every skill the agent depends on is also synced** into `<root>/skills/`. HQ
  union-adds an enabled agent's skills to `enabledSkills`, but the repo-local path
  does not — so when an agent comes from the repo step, copy the skills it references
  too, or the agent loads but fails mid-run.
- The agent resolves on the next turn (it shows up as a dispatchable subagent type).

### MCP servers (end to end — this is the one most often left half-done)
Writing a config entry does **not** make an MCP server work. Finish all of:
1. **Config in the right place.** claude+ reads servers from `<root>/.claude.json`
   under `mcpServers[<name>]` (the claude.ai connectors live here); file-based
   `.mcp.json` servers must additionally be listed in `enabledMcpjsonServers` in
   `<root>/.claude.json` or they stay disabled. Merge, never clobber unrelated
   servers.
2. **Launch dependency available.** Resolve the server's `command`/`args` —
   `npx -y <pkg>` package fetchable, binary on PATH, or `uvx`/python module
   installed. If the command can't launch, the server will silently fail to connect.
3. **Required env / secrets present.** Populate every env var the server needs
   (API keys, tokens). A server missing its key connects and then errors on first
   tool call.
4. **OAuth handshake completed.** A name in `<root>/mcp-needs-auth-cache.json` means
   the server is registered but **NOT yet authenticated** (this is the usual reason a
   "synced" connector is dead). Run the connector's auth flow to completion
   (`authenticate` → `complete_authentication`) and confirm the entry clears from the
   needs-auth cache. The claude.ai connectors (Gmail, Google Drive, Google Calendar,
   Microsoft 365) all require this.
5. **Connection verified.** Confirm the server actually connects and its tools
   enumerate before counting it done. Report any that still need an interactive login
   the user must complete (e.g. a browser auth) rather than claiming success.

### Plugins (registration completeness — fixes missing autocomplete)
A plugin whose cache is downloaded and whose `enabledPlugins` flag is set will still
**not load** — no commands, no skills, nothing in `/`-autocomplete — unless it is
registered in `<root>/plugins/installed_plugins.json`. Reconcile it: for every
`<plugin>@<marketplace>` that is (a) enabled in `<root>/settings.json`
`enabledPlugins` and (b) present in `<root>/plugins/cache/<marketplace>/<plugin>/<version>/`,
ensure an entry exists with `scope`, `installPath` (that cache dir), `version` (from
the cached `.claude-plugin/plugin.json`), and `gitCommitSha` (the marketplace clone's
HEAD). This is exactly the step whose absence leaves a freshly-pulled bundle invisible.

### Verification gate
Recompute the effective enabled set and assert, per item: skill dir + `name`
frontmatter; agent file + valid frontmatter + its skills present; MCP server
connected/authed (not in the needs-auth cache); plugin registered. Emit a per-kind
table. If anything fails, report it as an explicit failure with the item and the
missing piece — a partial install must never be reported as success.

## How to run

Four moves, all landing in this project's root:

1. **Repo → project root** (step 1). Copy the connected repo's whole
   `.claude/skills/<name>/**` directories and `.claude/agents/**` into
   `~/.claude+/roots/<projectSlug>/`, and merge the repo's `.mcp.json` `mcpServers`
   entries into the root's `.claude.json` `mcpServers` (and add file-based ones to
   `enabledMcpjsonServers`). Create subdirectories as needed and overwrite only when
   the repo copy differs; merge MCP config rather than clobber so unrelated servers
   survive.
2. **HQ → project root** (steps 2–3):

   ```bash
   claude+ sync-skills
   ```

   `sync-skills` performs the HQ-side one-shot reconcile for **all three kinds**
   (skills, agents, and MCP servers), into the linked project's root. Report its
   printed `pulled N` count. Note the CLI message says only "skills synced" even
   though agents and MCP servers are included in the same pass. If it prints
   `401 Unauthorized`, the device isn't signed in — run `claude+ login` first (the
   repo step still works offline).
3. **Finish end-to-end setup** (step 4). For each pulled item, complete the
   **end-to-end setup contract** above: copy agents' dependent skills, install MCP
   launch dependencies, populate MCP env, complete any OAuth handshake (clear the
   entry from `mcp-needs-auth-cache.json`), and reconcile
   `<root>/plugins/installed_plugins.json` so every enabled+cached plugin is
   registered (without this, a pulled bundle's commands never appear in
   `/`-autocomplete).
4. **Verify** (step 5). Run the verification gate over the enabled set and only then
   report the per-kind result; fail loudly on any item that didn't fully land.

A freshly reconciled skill, agent, or MCP server is usable on the **next turn**
(claude+ re-reads the project root); no rebuild of the claude+ binary is involved —
these are runtime files, not compiled in. A plugin needs its
`installed_plugins.json` entry (step 3) before its commands load.

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
