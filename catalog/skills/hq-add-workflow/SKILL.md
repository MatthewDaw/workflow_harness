---
name: hq-add-workflow
description: >-
  Author and register a NEW Command HQ workflow in the org-scoped catalog from
  inside the claude+ PTY, then opt the current project in and sync. A workflow is
  a DAG of catalog AGENTS: gathers the workflow record (nodes[], each pointing to
  an existing agent by name, wired by dependsOn[] edges, with an optional
  rerun-until-done rule), registers it via the workflows REST (admin-gated org
  write), enables it on the project — which also unions every referenced agent
  (and transitively its skills + MCP servers) — and materializes it. Use when the
  user says "/hq-add-workflow", "add a workflow", "create a workflow", "register a
  workflow in HQ", or "enable a workflow on this project". For editing an existing
  workflow use /hq-update-workflow.
---

# /hq-add-workflow

Author a **new Command HQ workflow** — a **DAG of catalog agents** — register it
in the **org catalog**, then run the round trip so it's live on the project and
usable in this session. Runs in the developer's claude+ session, inside a
connected repo. This is the workflow sibling of `/hq-add-agent`, `/hq-add-skill`,
and `/hq-add-mcp`; to **edit** an existing workflow's record use
`/hq-update-workflow`.

The catalog model and the shared **resolve → enable → sync → verify** back half
live in [`hq-catalog-opt-in.md`](../../../docs/superpowers/hq-catalog-opt-in.md).
This skill is the workflow-specific front half plus that round trip.

## 1 · Gather the workflow record

A workflow is a STRUCTURED record (`workflowSchema` in
`packages/shared/src/dto.ts`), not a markdown body. It is a **DAG of agents** —
each node runs an existing catalog agent, and edges are `dependsOn` dependencies.
Collect:

- **`name`** (required) — kebab-case, unique among workflows (`GET <HQ>/workflows`
  to check).
- **`description`** (required) — what the workflow accomplishes end to end.
- **`nodes`** (required) — the DAG. Each node is a structured record:
  - **`id`** (required) — unique within the workflow; the handle other nodes
    reference in `dependsOn` / `declaredBy`.
  - **`agent`** (required) — a **NAME POINTER to an existing catalog agent**. This
    is the node's worker. The referenced agent **must already exist** in the
    catalog (`GET <HQ>/agents` to check); if it doesn't, mint it first with
    `/hq-add-agent`, then point the node at it. Don't invent agents inline.
  - **`label`** — short human-facing name for the node (defaults to `''`).
  - **`prompt`** — per-node task instructions handed to that agent for this step.
  - **`dependsOn`** — string[] of **upstream node ids**. These are the **DAG
    edges**: a node runs only once every id in its `dependsOn` has finished, and
    those nodes' outputs become its context. The graph must be **acyclic** (the
    schema's `superRefine` rejects cycles, duplicate ids, and dangling
    `dependsOn`).
  - **`rerun`** (optional) — the **rerun-until-done** rule. A node with a rerun
    rule loops after each run until done or the `maxRuns` safety cap is hit:
    - **`mode: 'self'`** — after each run a Haiku judge asks whether
      **`endCriteria`** is satisfied (DONE/CONTINUE), looping until DONE.
    - **`mode: 'declared-by'`** — the node reruns until checker node
      **`declaredBy`** declares it done (the generator↔checker loop, "another
      agent declares the work fully done"). `declaredBy` is a node id; it is a
      CONTROL edge, **not** a DAG edge, so it is excluded from the acyclicity
      check (but `maxRuns` still bounds it).
    - **`maxRuns`** — positive integer safety cap (default `10`) so the loop can
      never run forever.

**Referenced agents must already exist in the catalog.** Every `agent` name on a
node should resolve to a catalog agent so the pointer works when the workflow is
consumed — enabling the workflow on a project **unions every referenced agent**
(and transitively its skills + MCP servers) into the project's enabled sets. If a
node needs a brand-new agent, mint it first (`/hq-add-agent`), then point the node
at it. Don't invent agents the user didn't ask for.

## 2 · Register into the org catalog

With `HQ="$CLAUDE_PLUS_API_URL"` and `TOK="$(sed -n 2p ~/.claude-plus/credentials)"`
(the org-scope write is admin-gated; the server forces `scope` + stamps `createdBy`):

```
POST <HQ>/workflows   { "name":"<name>", "description":"<what it does>",
                        "nodes":[
                          { "id":"gen", "agent":"<existing-agent>", "label":"Generate",
                            "prompt":"<task>", "dependsOn":[] },
                          { "id":"check", "agent":"<checker-agent>", "label":"Review",
                            "prompt":"<verify the work>", "dependsOn":["gen"],
                            "rerun":{ "mode":"declared-by", "declaredBy":"check",
                                      "maxRuns":5 } }
                        ] }
GET  <HQ>/workflows          # read the catalog / check the name is free
GET  <HQ>/agents             # confirm every node's `agent` already exists
```

`201`/`200` = live immediately. `403` = your profile isn't an org admin · `401` =
deployed API predates device-token writes · `409` = a canonical built-in (fork it,
or change `.claude/...` and re-seed). For `403`/`401`, use the
[seed-path fallback](../../../docs/superpowers/hq-catalog-opt-in.md#fallback-the-seed-path-bootstrap--not-an-admin).

A later **edit** from a project context forks/updates the `(name, repo, person)`
variant and snapshots a revision — it never clobbers the base. Promote a variant
to the org-wide TRUE default (any authed member):
`POST <HQ>/workflows/<name>/promote { variantId, rev? }`. (Editing is
`/hq-update-workflow`.)

## 3 · Opt the project in, sync, and verify — the round trip

Follow [resolve → enable → sync → verify](../../../docs/superpowers/hq-catalog-opt-in.md#resolve--enable--sync--verify)
in full. In short:

1. **Resolve** `$CLAUDE_PLUS_PROJECT_ID`; `GET $HQ/projects/$PID`. If it 404s,
   derive the candidate from `git remote get-url origin` (HQ's exact slug
   algorithm), probe that then the bare-repo slug, prefer the `gh/`-prefixed
   record if more than one resolves; STOP only if none do.
2. **Enable:** `POST $HQ/projects/$PID/workflows/<name>`. This enables the
   workflow **and unions every node's `agent`** (and transitively its skills,
   bundles flattened, + MCP servers) into the project's `enabledAgents` /
   `enabledSkills` / `enabledMcpServers`, so the workflow's dependencies come
   along automatically. Confirm the returned project lists the workflow in
   `enabledWorkflows` and its agents in `enabledAgents`.
3. **Sync:** `claude+ sync`. It pulls the workflow spec into
   `~/.claude+/workflows/<name>.json` (the structured DAG as JSON) and any of its
   referenced agents/skills missing locally.
4. **Verify:** assert `pulled ≥ 1` **and that `~/.claude+/workflows/<name>.json`
   exists** (the materialized spec). `pulled 0` after a successful enable = stale
   daemon binding — restart it by re-running `claude+` in this repo, then sync
   again.
5. **Report:** `enabled <workflow> (+<K> agents) · pulled <N> · root now <M>` —
   never "toggle it in the web app."

If `$CLAUDE_PLUS_PROJECT_ID` is empty or no candidate resolves, the repo isn't a
connected HQ project: registration in the org catalog is the deliverable — say so
and finish.

## Contract

Depends only on existing surface: `workflowSchema` (`packages/shared/src/dto.ts`),
the workflows REST (`packages/backend/src/rest/workflows.ts`:
`GET/POST/PUT/DELETE /workflows`, `/workflows/:name/promote`), the per-project
opt-in (`POST/DELETE /projects/:id/workflows/:name`, which unions every referenced
agent and its skills), and `claude+ sync` (which materializes
`~/.claude+/workflows/<name>.json`). Referenced agents are name pointers that must
already exist in the agents catalog (mint via `/hq-add-agent`). It invents no new
backend surface.
