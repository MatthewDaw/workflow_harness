---
name: hq-add-mcp
description: >-
  Author and register an MCP server in the single org-scoped Command HQ catalog
  from inside the claude+ PTY. A thin authoring skill over the existing
  `/mcp-servers` REST: it gathers a STRUCTURED transport record (stdio
  command/args/env OR http/sse url/headers), warns that secrets are stored
  plaintext, registers it (admin-gated org write), and opts the project in. An
  edit cuts a NEW version/variant rather than clobbering the org item. Use when
  the user says "/hq-add-mcp", "add an mcp server", "register an mcp server in
  HQ", "wire up a connector", or "enable an mcp server on this project".
---

# /hq-add-mcp

Author and register a **Command HQ MCP server** — the third pillar of a Claude
setup alongside skills and agents — into the **org catalog**, then opt the
current project into it. Runs in the developer's claude+ session, inside a
connected repo. This is the MCP companion to `/hq-add-skill`: author → register
via the existing REST → project opt-in → `claude+ sync` to materialize.

> **Scope model (live, 3-tier).** The catalog code is firmly 3-tier
> (`org` / `user` / `project`; see `packages/backend/src/rest/scopeauth.ts`
> `canReadScope` / `canWriteScope`). MCP servers, like skills/agents, live in
> the **org catalog** by default (`scope: { tier: "org", id: "<org>" }`), and an
> **org-scope write is admin-gated** (`canWriteOrgCatalog` ⇒ the `custom:admin`
> claim). A user may still write their **own** user scope without admin, and a
> project owner their own project scope. The server **forces** `scope` and
> stamps `createdBy` from the authenticated principal — you never set them.

> **Versioning (live).** A catalog item version is `(baseName, repoId, userId)`.
> Editing the org MCP server `M` from project `R` as person `P` **forks/updates**
> the variant `(M, R, P)` — it never edits the org **base** variant or anyone
> else's. Every edit **snapshots an immutable revision**. One org-wide **TRUE**
> version per name is the default shown in the UI and the default added to a
> project; **any authed org member may promote** a variant to true via
> `POST /mcp-servers/:name/promote { variantId, rev? }` (NOT admin-gated). A
> default `add` uses the current TRUE; a per-repo dropdown lets a repo pick
> another variant; sync materializes the chosen one.

## When this runs

In the developer's claude+ session (the PTY), from inside a connected repo. It
uses the **same authenticated HTTP transport the other HQ skills use** (the
device/session bearer token established at `claude+ login`, HQ API base from the
same login) to write the `/mcp-servers` REST. It invents no new backend surface.

## 1 · Gather the structured transport record

An MCP server is **not** a markdown body — it is a STRUCTURED discriminated union
on `transport` (`mcpServerSchema` in `packages/shared/src/dto.ts`). Decide the
transport, then collect its fields:

- **`stdio`** (local subprocess the daemon spawns):
  - `command` (required) — the executable, e.g. `npx`, `uvx`, a path.
  - `args` — string array passed to `command` (default `[]`).
  - `env` — `Record<string,string>` of environment variables for the subprocess
    (default `{}`). **These hold the secrets** (API keys, tokens).
- **`http`** (remote, request/response):
  - `url` (required) — the endpoint the daemon connects to (must be a valid URL).
  - `headers` — `Record<string,string>` of static request headers, e.g.
    `Authorization` (default `{}`). **These hold the secrets.**
- **`sse`** (remote, server-sent events): same shape as `http` — `url` +
  `headers`.

Ask the user only for the fields the chosen transport needs. Keep the `name`
kebab-case and unique among existing servers (`GET /mcp-servers` to check).

### Plaintext-secret warning — always surface this

`env` / `headers` values are stored in DynamoDB as **PLAINTEXT** and served to
**any authed org member** (the same trust model skills' bodies already use — see
the SECURITY note on `mcpServerSchema`). KMS / env-ref resolution is a documented
follow-up, not implemented. So **before registering any server that carries a
secret**, tell the user plainly: "This `<env|headers>` value is stored
unencrypted and readable by every member of your org." Prefer a value the org is
comfortable sharing; never invent or log a secret. For OAuth connectors, do
**not** put a bearer token here — those are authenticated interactively (see
`/hq-sync`'s needs-auth flow), not by a static header.

## 2 · Register into the org catalog (version-aware)

Register via the Command HQ MCP REST with the bearer token claude+ already holds.
The **org-scope write is admin-gated**; a non-admin org write is rejected (surface
that and ask an admin, or register at the caller's own user scope). The server
forces `scope` and stamps `createdBy` — supply only the record fields:

```
# create / update an MCP server (server forces scope + createdBy)
POST <HQ_API>/mcp-servers
  { "name":"<name>", "transport":"stdio",
    "command":"npx", "args":["-y","@scope/mcp"], "env":{"API_KEY":"<value>"} }

# remote transport variant
POST <HQ_API>/mcp-servers
  { "name":"<name>", "transport":"http",
    "url":"https://example.com/mcp", "headers":{"Authorization":"Bearer <value>"} }

# update an existing server (preserves the createdBy stamp)
PUT  <HQ_API>/mcp-servers/<name>   { ...same body shape... }

# read the current catalog / one server
GET  <HQ_API>/mcp-servers
GET  <HQ_API>/mcp-servers/<name>
```

**Fork-on-edit, not clobber.** A first registration of a brand-new `name` seeds
the org **base** variant. An **edit** from a project context forks/updates the
`(name, repo, person)` variant and snapshots a new immutable revision — it never
overwrites the base or another person's variant. The catalog stays safe: there is
no silent org-wide overwrite.

**Promote a variant to TRUE** (the org-wide default shown in the UI and added to
projects by default) — callable by **any authed org member**, not admin-gated:

```
POST <HQ_API>/mcp-servers/<name>/promote   { "variantId":"<id>", "rev":<N> }
```

Promotion only repoints the per-name TRUE pointer; it never edits or deletes a
variant. You can also promote from the HQ MCP Servers tab.

## 3 · Opt a project into the server (per-project enablement)

Registering puts the server in the catalog; it is **not** active on any project
until that project opts in. The project's enabled-set entry records **which
variant** it uses (`{ name, variantId }`); enabling defaults `variantId` to the
current TRUE, and a per-repo dropdown can switch it to another variant.

```
# enable a server on a project (idempotent); defaults the variant to TRUE
POST   <HQ_API>/projects/<projectId>/mcp-servers/<name>
DELETE <HQ_API>/projects/<projectId>/mcp-servers/<name>
```

**Getting the `projectId` — never guess it.** claude+ injects the authoritative HQ
project id into the session as `$CLAUDE_PLUS_PROJECT_ID` (alongside `$CLAUDE_PLUS_REPO`
and `$CLAUDE_PLUS_API_URL`), mirrored in `$CLAUDE_CONFIG_DIR/hq-project.json`. Use
`$CLAUDE_PLUS_PROJECT_ID` directly as the `projectId` in any `/projects/<projectId>/…`
call. Do NOT derive, slug, or probe candidate ids. If `$CLAUDE_PLUS_PROJECT_ID` is empty,
this session is not running under claude+ — say so and stop; do not guess.

Each returns the updated `Project` with `enabledMcpServers` populated. Auth gate:
org admin **or** the project owner. The server name must already exist in the
catalog (else `404`). Enabling an **agent** that declares the server in its
`mcpServers[]` also unions it into the project's `enabledMcpServers`. You can also
do this from the HQ project **MCP Servers** tab.

## 4 · Materialize it in this session — always do this

This step is **mandatory** — it's the difference between "registered in the
catalog" and "usable right now." Run `claude+ sync` (or `/hq-sync`) to pull
the linked project's enabled MCP servers into the isolated per-project claude+
config root, so the session picks them up without restarting. Sync writes the
server into the location Claude actually reads (`<root>/.claude.json` `mcpServers`,
adding file-based ones to `enabledMcpjsonServers`, merge-not-clobber) and
classifies it `ok` / `needs-auth` / `failed`:

- **`ok`** — launch dependency + static env resolved; usable now.
- **`needs-auth`** — an OAuth connector; sync **reports** it and surfaces the
  exact interactive `/mcp` login command. It does **not** auto-complete OAuth —
  that requires interactive browser consent by design. Never claim auto-auth.
- **`failed`** — surfaced by the verification gate, named with the missing piece.

Report the printed result. If you registered the server but didn't opt the project
in (step 3), the sync won't pull it — enable it on the project first.

## Contract

Depends only on things that already exist: `mcpServerSchema` (the structured
`transport` union, `packages/shared/src/dto.ts`), the MCP REST
(`packages/backend/src/rest/mcpServers.ts`: `GET/POST/PUT/DELETE /mcp-servers`,
`GET /mcp-servers/:name/usage`), the per-project opt-in
(`POST/DELETE /projects/:id/mcp-servers/:name`), the version `promote` endpoint,
and `claude+ sync`. It invents no new backend surface.
