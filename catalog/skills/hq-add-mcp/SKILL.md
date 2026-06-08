---
name: hq-add-mcp
description: >-
  Author and register an MCP server in the org-scoped Command HQ catalog from
  inside the claude+ PTY, then opt the current project in and sync. Gathers a
  STRUCTURED transport record (stdio command/args/env OR http/sse url/headers),
  warns that secrets are stored plaintext, registers it (admin-gated org write),
  enables it on the project, and materializes it. Use when the user says
  "/hq-add-mcp", "add an mcp server", "register an mcp server in HQ", "wire up a
  connector", or "enable an mcp server on this project".
---

# /hq-add-mcp

Author a **Command HQ MCP server** — the third pillar alongside skills and agents
— register it in the **org catalog**, then run the round trip so it's live on the
project and materialized in this session. Runs in the developer's claude+ session,
inside a connected repo.

The catalog model and the shared **resolve → enable → sync → verify** back half
live in [`hq-catalog-opt-in.md`](../../../docs/superpowers/hq-catalog-opt-in.md).
This skill is the MCP-specific front half plus that round trip.

## 1 · Gather the structured transport record

An MCP server is **not** a markdown body — it is a discriminated union on
`transport` (`mcpServerSchema` in `packages/shared/src/dto.ts`). Pick the
transport, collect its fields:

- **`stdio`** (local subprocess the daemon spawns): `command` (required), `args`
  (string[], default `[]`), `env` (`Record<string,string>`, default `{}` — **holds
  the secrets**).
- **`http`** (remote request/response): `url` (required, valid URL), `headers`
  (`Record<string,string>`, default `{}` — **holds the secrets**).
- **`sse`** (remote server-sent events): same shape as `http`.

Keep `name` kebab-case and unique (`GET <HQ>/mcp-servers` to check). Ask only for
the chosen transport's fields.

### Plaintext-secret warning — always surface this

`env` / `headers` values are stored in DynamoDB as **PLAINTEXT** and served to any
authed org member (KMS/env-ref resolution is a documented follow-up, not built).
Before registering a server that carries a secret, tell the user plainly: "This
`<env|headers>` value is stored unencrypted and readable by every member of your
org." Prefer a value the org is comfortable sharing; never invent or log a secret.
For OAuth connectors, do **not** put a bearer token here — those authenticate
interactively (sync's needs-auth flow), not via a static header.

## 2 · Register into the org catalog

With `HQ="$CLAUDE_PLUS_API_URL"` and `TOK="$(sed -n 2p ~/.claude-plus/credentials)"`
(the org-scope write is admin-gated; the server forces `scope` + `createdBy`):

```
# stdio transport
POST <HQ>/mcp-servers   { "name":"<name>", "transport":"stdio",
                          "command":"npx", "args":["-y","@scope/mcp"], "env":{"API_KEY":"<value>"} }
# remote transport
POST <HQ>/mcp-servers   { "name":"<name>", "transport":"http",
                          "url":"https://example.com/mcp", "headers":{"Authorization":"Bearer <value>"} }
# update an existing server (preserves createdBy)
PUT  <HQ>/mcp-servers/<name>   { ...same body shape... }
GET  <HQ>/mcp-servers          # read the catalog
```

An **edit** from a project context forks/updates the `(name, repo, person)`
variant and snapshots a revision — it never clobbers the base or another person's
variant. Promote a variant to the org-wide TRUE default (any authed member):
`POST <HQ>/mcp-servers/<name>/promote { variantId, rev? }`.

## 3 · Opt the project in, sync, and verify — the round trip

Follow [resolve → enable → sync → verify](../../../docs/superpowers/hq-catalog-opt-in.md#resolve--enable--sync--verify)
in full. In short:

1. **Resolve** `$CLAUDE_PLUS_PROJECT_ID`; `GET $HQ/projects/$PID`. If it 404s,
   derive the candidate from `git remote get-url origin` (HQ's exact slug
   algorithm), probe that then the bare-repo slug, prefer the `gh/`-prefixed
   record if more than one resolves; STOP only if none do.
2. **Enable:** `POST $HQ/projects/$PID/mcp-servers/<name>` (idempotent; defaults
   the variant to TRUE). Enabling an **agent** that declares the server in its
   `mcpServers[]` also unions it in.
3. **Sync:** `claude+ sync`. It writes the server into the location Claude reads
   (`<root>/.claude.json` `mcpServers`, merge-not-clobber) and classifies it:
   - **`ok`** — launch dependency + static env resolved; usable now.
   - **`needs-auth`** — an OAuth connector; sync reports the exact `/mcp` login
     command. It does **not** auto-complete OAuth (interactive browser consent by
     design). Never claim auto-auth.
   - **`failed`** — surfaced by the verification gate, named with the missing piece.
4. **Verify:** assert the server is present in `~/.claude+/.claude.json`
   `mcpServers`. `pulled 0`/absent after a successful enable = stale daemon
   binding — restart it by re-running `claude+`, then sync again.
5. **Report:** `enabled <N> · pulled <N> · status <ok|needs-auth|failed>` — never
   "toggle it in the web app."

If `$CLAUDE_PLUS_PROJECT_ID` is empty or no candidate resolves, the repo isn't a
connected HQ project: registration in the org catalog is the deliverable — say so
and finish.

## Contract

Depends only on existing surface: `mcpServerSchema` (`packages/shared/src/dto.ts`),
the MCP REST (`packages/backend/src/rest/mcpServers.ts`:
`GET/POST/PUT/DELETE /mcp-servers`, `/mcp-servers/:name/promote`,
`/mcp-servers/:name/usage`), the per-project opt-in
(`POST/DELETE /projects/:id/mcp-servers/:name`), and `claude+ sync`. It invents no
new backend surface.
