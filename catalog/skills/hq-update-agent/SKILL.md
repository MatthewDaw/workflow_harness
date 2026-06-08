---
name: hq-update-agent
description: >-
  Edit an existing Command HQ agent — its prompt, model, tools, and the
  `skills[]` / `mcpServers[]` it depends on — from inside the claude+ PTY,
  cutting a NEW version (fork-on-edit) instead of clobbering the org item. Loads
  the agent from HQ, applies the requested edits, shows a diff, and on
  confirmation writes it back via the existing agents REST as a new variant
  revision. Use when the user says "/hq-update-agent", "edit an agent", "update
  an agent's skills", "change an agent's mcp servers", or "modify an agent's
  prompt and tools".
---

# /hq-update-agent

The general agent **editor** for Command HQ: change an agent's full definition —
`prompt`, `model`, `tools`, the `skills[]` it pulls in, and the `mcpServers[]` it
declares — and persist it as a **new version**. Where `/hq-optimize-agent` only
refines the *prompt* via a scored refine loop, this skill edits the **whole
record** and is the right tool for "this agent now needs skill X" or "point it at
this MCP server." It mirrors `/hq-optimize-agent`'s registration pattern (load →
edit → confirm → write-back via the agents REST) for full edits.

## Where this runs

In the developer's claude+ session (the PTY), inside a connected repo. It uses the
**same authenticated HTTP transport the other HQ skills use** (the device/session
bearer token established at `claude+ login`, HQ API base from the same login) to
read and write agents. No model keys, no Bedrock — the edits are applied directly.

> **Scope model (live, 3-tier).** The catalog code is firmly 3-tier
> (`org` / `user` / `project`; see `packages/backend/src/rest/scopeauth.ts`).
> Agents live in the **org catalog** by default; an **org-scope write is
> admin-gated** (`canWriteOrgCatalog` ⇒ the `custom:admin` claim). A user may
> write their **own** user scope without admin, a project owner their own project
> scope. The server **forces** `scope` and preserves `createdBy` — you never set
> them.

> **Versioning (live) — fork-on-edit.** A version is `(baseName, repoId,
> userId)`. Editing the org agent `A` from project `R` as person `P`
> **forks/updates** the variant `(A, R, P)` and **snapshots an immutable
> revision** — it never edits the org **base** variant or anyone else's. One
> org-wide **TRUE** version per name is the UI default and the default added to a
> project; **any authed org member may promote** a variant to true via
> `POST /agents/:name/promote { variantId, rev? }` (NOT admin-gated). Promotion
> only repoints the per-name TRUE pointer; it never edits or deletes a variant.

## Inputs it gathers

- **Target agent (required).** The agent to edit, by `name`. Ask the user which
  agent if not given. (The current TRUE variant is the default starting point;
  the user can pick another variant to edit.)
- **The edits (required).** What to change — any of: a new/updated `prompt`, a
  different `model`, added/removed `tools`, added/removed `skills` (the skills
  the agent pulls in — these are **name pointers** to catalog skills), or
  added/removed `mcpServers` (name pointers to catalog MCP servers). Gather the
  concrete deltas; do not invent capabilities the user did not ask for.

## Steps

1. **Load the agent (read).** `GET /agents/{name}` with the bearer token. A 404
   means it doesn't exist (or is unreadable) — stop and report. Capture the full
   record: `name`, `model`, `prompt`, `description`, `skills`, `tools`,
   `mcpServers`.
2. **Apply the requested edits in-session.** Produce the new record by applying
   only the user's deltas on top of the loaded record; leave untouched fields
   exactly as they were (re-send the **full** record on write — the handler
   upserts the whole object). For `skills[]` / `mcpServers[]`, edit the **name
   pointer arrays** — the agent and the skills/MCP servers it references are
   distinct catalog records.
   - **Referenced skills/servers must exist.** Every name in the new `skills[]`
     and `mcpServers[]` should already be a catalog item so the pointers resolve
     when the agent is consumed. If the edit needs a brand-new skill or MCP
     server, mint/register it first (`/hq-add-skill`, `/hq-add-mcp`), then point
     the agent at it.
3. **Show the diff.** Present the before → after for each changed field
   (prompt, model, tools, skills, mcpServers) so the user sees exactly what
   changes. Make clear it is **not yet saved**.
4. **Write back on confirmation (new version).** Only if the user confirms,
   persist the edited agent via the existing agents REST — re-send the full
   `agentSchema` record:
   - `PUT /agents/{name}` (or `POST /agents`; the handler upserts) with body
     `{ name, model, prompt, description, skills, tools, mcpServers }` and the
     bearer token. The server forces `scope` and preserves `createdBy`.
   - **This cuts a new version.** Editing from a project context forks/updates
     the `(name, repo, person)` variant and appends a new immutable revision —
     it never mutates the org base variant or another person's variant.
   - **Auth.** An org-scope write is admin-gated; a user/project-scope write is
     allowed for the author/owner. If the caller cannot write the target scope,
     report it and leave HQ unchanged (offer to save the edit at the caller's own
     user scope instead).
5. **Optionally promote to TRUE.** If the user wants this edit to become the
   org-wide default, promote the new variant:
   `POST /agents/{name}/promote { variantId, rev? }` — callable by **any authed
   org member**, not admin-gated. This only repoints the TRUE pointer.
6. **Confirm + materialize.** Print the agent's HQ URL so the change is visible,
   then run `claude+ sync` (or `/hq-sync`) so the linked project picks up
   the edited agent — and, per the agent-deps guarantee, any skill in its
   `skills[]` missing locally is pulled too, and any `mcpServers[]` is wired into
   the project's enabled set. Report the printed sync result. Do not end telling
   the user to sync later — do it for them.

## Notes

- This is the full-record **editor**; `/hq-optimize-agent` is the prompt-only
  **refiner** (scored refine→judge→keep-best loop). Both write back through the
  same `PUT/POST /agents` REST and both cut a new version on edit.
- The agent stores its `skills[]` / `mcpServers[]` as **name pointers**, not
  inlined definitions — editing those arrays changes which catalog items the
  agent pulls in, not the items themselves.

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. Verified by running it: it loads an
agent from HQ, applies the user's edits to the full record (prompt / model /
tools / skills / mcpServers), shows a diff, and on confirmation re-saves via the
existing `PUT/POST /agents` REST as a **new variant revision** (org writes
admin-gated; fork-on-edit never clobbers the base/another variant). It never
writes without confirmation, and finishes by syncing so the project picks up the
edited agent + its dependencies.
