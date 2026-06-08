---
name: hq-add-agent
description: >-
  Author and register a NEW Command HQ agent in the org-scoped catalog from inside
  the claude+ PTY, then opt the current project in and sync. Gathers the agent
  record (prompt, model, tools, and the skills[]/mcpServers[] name pointers it
  depends on), registers it via the agents REST (admin-gated org write), enables
  it on the project — which also unions its skills — and materializes it. Use when
  the user says "/hq-add-agent", "add an agent", "create an agent", "register an
  agent in HQ", or "enable an agent on this project". For editing an existing
  agent use /hq-update-agent.
---

# /hq-add-agent

Author a **new Command HQ agent** — the second pillar alongside skills and MCP
servers — register it in the **org catalog**, then run the round trip so it's live
on the project and usable in this session. Runs in the developer's claude+
session, inside a connected repo. This is the agent sibling of `/hq-add-skill` and
`/hq-add-mcp`; to **edit** an existing agent's record use `/hq-update-agent`.

The catalog model and the shared **resolve → enable → sync → verify** back half
live in [`hq-catalog-opt-in.md`](../../../docs/superpowers/hq-catalog-opt-in.md).
This skill is the agent-specific front half plus that round trip.

## 1 · Gather the agent record

An agent is a STRUCTURED record (`agentSchema` in `packages/shared/src/dto.ts`),
not a markdown body. Collect:

- **`name`** (required) — kebab-case, unique among agents (`GET <HQ>/agents` to
  check).
- **`description`** (required) — the **delegation trigger**: when this agent should
  be invoked. This is what the orchestrator reads to route work to it.
- **`prompt`** (required) — the agent's system prompt / instructions.
- **`model`** — `opus` / `sonnet` / `haiku` (or a full id). Omit to inherit.
- **`tools`** — string[] of allowed tools. Omit/`[]` = inherit the default set.
- **`skills`** — string[] of **name pointers** to catalog skills the agent pulls
  in. Enabling the agent on a project unions these into `enabledSkills`.
- **`mcpServers`** — string[] of **name pointers** to catalog MCP servers the agent
  declares.

**Referenced skills / MCP servers must already exist in the catalog.** Every name
in `skills[]` / `mcpServers[]` should resolve to a catalog item so the pointers
work when the agent is consumed. If the agent needs a brand-new one, mint it first
(`/hq-add-skill`, `/hq-add-mcp`), then point the agent at it. Don't invent
capabilities the user didn't ask for.

## 2 · Register into the org catalog

With `HQ="$CLAUDE_PLUS_API_URL"` and `TOK="$(sed -n 2p ~/.claude-plus/credentials)"`
(the org-scope write is admin-gated; the server forces `scope` + stamps `createdBy`):

```
POST <HQ>/agents   { "name":"<name>", "description":"<delegation trigger>",
                     "prompt":"<system prompt>", "model":"sonnet",
                     "tools":[], "skills":["<skill-a>"], "mcpServers":[] }
GET  <HQ>/agents          # read the catalog / check the name is free
```

`201`/`200` = live immediately. `403` = your profile isn't an org admin · `401` =
deployed API predates device-token writes · `409` = a canonical built-in (fork it,
or change `.claude/agents` and re-seed). For `403`/`401`, use the
[seed-path fallback](../../../docs/superpowers/hq-catalog-opt-in.md#fallback-the-seed-path-bootstrap--not-an-admin).

A later **edit** from a project context forks/updates the `(name, repo, person)`
variant and snapshots a revision — it never clobbers the base. Promote a variant
to the org-wide TRUE default (any authed member):
`POST <HQ>/agents/<name>/promote { variantId, rev? }`. (Editing is `/hq-update-agent`.)

## 3 · Opt the project in, sync, and verify — the round trip

Follow [resolve → enable → sync → verify](../../../docs/superpowers/hq-catalog-opt-in.md#resolve--enable--sync--verify)
in full. In short:

1. **Resolve** `$CLAUDE_PLUS_PROJECT_ID`; `GET $HQ/projects/$PID`. If it 404s,
   derive the candidate from `git remote get-url origin` (HQ's exact slug
   algorithm), probe that then the bare-repo slug, prefer the `gh/`-prefixed
   record if more than one resolves; STOP only if none do.
2. **Enable:** `POST $HQ/projects/$PID/agents/<name>`. This enables the agent **and
   unions its `skills[]`** (bundles flattened to leaves) into the project's
   `enabledSkills`, so the agent's dependencies come along automatically. Confirm
   the returned project lists the agent in `enabledAgents` and its skills in
   `enabledSkills`.
3. **Sync:** `claude+ sync`. It pulls the agent into `~/.claude+/agents/<name>.md`
   (materializing `model`/`tools`/`description` into frontmatter) and any of its
   `skills[]` missing locally.
4. **Verify:** assert `pulled ≥ 1` (or `~/.claude+/agents/<name>.md` exists).
   `pulled 0` after a successful enable = stale daemon binding — restart it by
   re-running `claude+` in this repo, then sync again.
5. **Report:** `enabled <agent> (+<K> skills) · pulled <N> · root now <M>` — never
   "toggle it in the web app."

If `$CLAUDE_PLUS_PROJECT_ID` is empty or no candidate resolves, the repo isn't a
connected HQ project: registration in the org catalog is the deliverable — say so
and finish.

## Contract

Depends only on existing surface: `agentSchema` (`packages/shared/src/dto.ts`), the
agents REST (`packages/backend/src/rest/agents.ts`: `GET/POST/PUT/DELETE /agents`,
`/agents/:name/promote`), the per-project opt-in
(`POST/DELETE /projects/:id/agents/:name`, which unions the agent's skills), and
`claude+ sync`. It invents no new backend surface.
