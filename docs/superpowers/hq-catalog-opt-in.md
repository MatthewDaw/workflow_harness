# HQ catalog opt-in — shared reference for the `/hq-add-*` skills

`/hq-add-skill`, `/hq-add-mcp`, and `/hq-add-agent` all do the same back half of
the job: register an item in the **org catalog**, then **opt the current project
in** so it actually materializes in the session. This doc is the single source
for that shared back half and for the catalog model the three skills assume, so
each `SKILL.md` can stay short and just link here.

If you only read one section, read **[Resolve → enable → sync → verify](#resolve--enable--sync--verify)**.

---

## The catalog model (what every `/hq-add-*` skill assumes)

- **One org catalog.** Skills, agents, and MCP servers live at
  `scope: { tier: "org", id: "<org>" }` by default. The 3-tier model
  (`org` / `user` / `project`) is still underneath (`packages/backend/src/rest/scopeauth.ts`),
  but the common path is the org catalog.
- **The server forces `scope` and stamps `createdBy`.** You send only the record
  fields (`name`, `kind`, `description`, `body`/transport, `members`, …). Never
  set `scope` or `createdBy`.
- **Writes are admin-gated, decided server-side.** An org-scope write
  (`POST/PUT/DELETE /skills`, `/agents`, `/mcp-servers`) needs the caller's
  profile to be an org admin (`profile.adminOrgs`/`profile.admin`) or the
  `custom:admin` gateway claim. Because it's derived from the profile, the
  **claude+ device token writes directly** — these routes are `HttpNoneAuthorizer`
  and the handler verifies the bearer + admin itself. `403` = your profile isn't
  an org admin; `401` = the deployed API predates device-token catalog writes
  (`cdk deploy ApiStack` to fix).
- **Edit = fork, not clobber.** Editing an item from a project context cuts a new
  variant `(baseName, repoId, userId)` and snapshots an immutable revision — it
  never overwrites the org base or anyone else's variant. One org-wide **TRUE**
  variant per name is the UI default and the default added to a project. **Any
  authed org member may promote** a variant to TRUE via
  `POST /<kind>/:name/promote { variantId, rev? }` (not admin-gated). The retired
  `POST /<kind>/:name/scope` verb is `410 Gone`.
- **Canonical built-ins are fork-only.** A `source:'built-in'` / seed-owned record
  (`createdBy.userId === 'system'`) rejects in-place `POST`/`PUT`/`DELETE` with
  `409` — fork it or change `catalog/skills` (or `.claude/agents`) and re-seed.

The full design lives in
[`specs/2026-06-03-org-catalog-scope-collapse-design.md`](specs/2026-06-03-org-catalog-scope-collapse-design.md).

---

## Environment — bind once, don't re-derive

- **HQ REST base:** `$CLAUDE_PLUS_API_URL` (injected), else line 3 of
  `~/.claude-plus/credentials`. `HQ="$CLAUDE_PLUS_API_URL"`.
- **Device token:** line 2 of `~/.claude-plus/credentials`.
  `TOK="$(sed -n 2p ~/.claude-plus/credentials)"`. The org rides inside the
  token — never prompt for it.

---

## Resolve → enable → sync → verify

This is the round trip. Do it in full — never dead-end at "go toggle it in the
web app."

### 1 · Resolve the project id (robust to a stale daemon)

claude+ injects the authoritative id as **`$CLAUDE_PLUS_PROJECT_ID`**. It is
derived by `config.ProjectIDFor` (`wrapper/internal/config/project.go`), which
slugs the git remote's `owner/repo` **byte-for-byte the same way** HQ's web mints
it (`projectIdFor` in `packages/web/src/screens/Projects/Projects.tsx`): lowercase,
every run of non-alphanumerics → one `-`, trim leading/trailing `-`. So
`git@github.com:MatthewDaw/fractions_tutorial.git` → `matthewdaw-fractions-tutorial`.

```bash
PID="$CLAUDE_PLUS_PROJECT_ID"
[ -z "$PID" ] && echo "not running under claude+; cannot opt a project in" && exit 0
```

Confirm it resolves: `GET $HQ/projects/$PID` (Bearer `$TOK`).

- **`200`** → connected. Use `$PID`. Skip to step 2.
- **`{"error":"not found"}`** → the injected id doesn't resolve. Do **not** stop
  here — this is almost always a **stale claude+ binary** injecting an old
  folder-name slug (the pre-fix `ProjectIDFor` slugged the repo folder, e.g.
  `fractions-tutorial`, not `matthewdaw-fractions-tutorial`). Derive the
  authoritative candidate from the git remote yourself and probe it:

  ```bash
  # owner/repo from origin, then HQ's exact slug algorithm
  slug="$(git remote get-url origin \
    | sed -E 's#^https?://[^/]+/##; s#^[^@]+@[^:]+:##; s#\.git$##; s#/+$##')"
  DERIVED="$(printf '%s' "$slug" | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
  BARE="$(printf '%s' "${slug##*/}" | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"   # last-resort fallback
  ```

  Probe `$DERIVED` first, then `$BARE`. Collect **every** id that returns `200`.

### Picking among matches (the duplicate-record trap)

- **Exactly one resolves** → use it.
- **More than one resolves** → there are **duplicate project records** for this
  repo (a known residue: an old slug variant left a phantom). Do **not** silently
  pick one. Print all matches with their `repo` field, and **prefer the record
  whose `repo` begins with `gh/`** — that's the canonical one the claude+ daemon
  and web UI use. Report which you chose and that a duplicate exists (it should be
  deleted from the HQ Projects tab).
- **None resolve** → this repo is genuinely not a connected HQ project. STOP the
  opt-in (don't ask what to do instead): catalog registration is the deliverable.
  Tell the user to connect the repo in the HQ web app, then finish.

> **Stale-binary signal.** If `$PID` 404'd but `$DERIVED`/`$BARE` resolved, the
> running claude+ binary is out of date (it injected the wrong id). Rebuild +
> reinstall claude+ so future sessions inject the right id. You can still finish
> the round trip now using the resolved id.

### 2 · Enable on the project

All routes are idempotent, Bearer `$TOK`, and return the updated `Project`. The
item must already exist in the org catalog (else `404`); gate is org admin **or**
project owner.

```bash
# a single skill
curl -fsS -X POST "$HQ/projects/$PID/skills/<name>"        -H "Authorization: Bearer $TOK"
# a whole bundle (enables every member in one call)
curl -fsS -X POST "$HQ/projects/$PID/bundles/<bundle>"     -H "Authorization: Bearer $TOK"
# an agent (also UNIONS the agent's skills, bundles flattened, into enabledSkills)
curl -fsS -X POST "$HQ/projects/$PID/agents/<name>"        -H "Authorization: Bearer $TOK"
# an MCP server
curl -fsS -X POST "$HQ/projects/$PID/mcp-servers/<name>"   -H "Authorization: Bearer $TOK"
```

Confirm each new name appears in the returned project's
`enabledSkills` / `enabledAgents` / `enabledMcpServers`.

### 3 · Sync into this session

```bash
claude+ sync
```

Prints, e.g.: `synced (skills + agents + mcp): pulled 2, pushed 0 (into ~/.claude+); verify: PASS`.
Writes go only to the isolated `~/.claude+` root, never your personal `~/.claude`.

### 4 · Verify the pull — assert it actually landed

- **Assert `pulled ≥ 1`** (or that the new name now exists under
  `~/.claude+/skills/<name>/SKILL.md`, `~/.claude+/agents/<name>.md`,
  `~/.claude+/workflows/<name>.json` for a workflow, or in
  `~/.claude+/.claude.json` `mcpServers` for an MCP server).
- **`pulled 0` despite a successful enable = stale daemon binding.** The per-repo
  daemon binds its sync source **once at startup** (`rt.cfgSrc` in
  `wrapper/internal/daemon/runtime.go`). A daemon started **before** this repo was
  connected holds a "no project" binding and pulls 0 even after you enable. Fix:
  restart it by re-running `claude+` in this repo (it force-restarts the daemon on
  launch), then `claude+ sync` again.

### 5 · Report the round trip

End with a concrete tally, not a deferral:

```
enabled <N> · pulled <N> · root now <M>
```

where `M` is the count under `~/.claude+/skills/` (and/or agents / mcp). Never end
by telling the user to toggle it in the web app — that's the friction these skills
exist to remove.

---

## Bundle vs local-skill name collision (registration-time, `/hq-add-skill`)

`claude+ sync` is **bidirectional** and upserts catalog records **by name** from
local skills (it scans `~/.claude` and the per-project root, pushing local-only
skills up). So a **bundle whose name equals a local skill dir gets clobbered**:
the local skill is pushed over the bundle and its members are wiped (this once
destroyed a 35-member bundle).

Before registering a bundle, check for a collision and refuse to proceed under the
colliding name:

```bash
test -d ~/.claude/skills/<bundle> -o -d .claude/skills/<bundle> && \
  echo "name collision: a local skill '<bundle>' would clobber this bundle on sync — pick a distinct bundle name or remove the local copy first"
```

The server now also rejects this server-side: a skill-push whose existing
same-name record is `kind:bundle` returns `409`
(`packages/backend/src/rest/skills.ts`). The local check above gives a clearer,
earlier message; the server guard is the backstop.

---

## Registration cheat-sheet

```
# skills / bundles
POST <HQ>/skills              { name, kind:"skill"|"bundle", description, source:"custom", body?, members? }
POST <HQ>/skills/<bundle>/members   { member }

# agents
POST <HQ>/agents              { name, model, prompt, description, skills:[], tools:[], mcpServers:[] }

# mcp servers (structured transport union; see packages/shared/src/dto.ts mcpServerSchema)
POST <HQ>/mcp-servers         { name, transport:"stdio", command, args, env }   # or http/sse: url, headers
```

`201`/`200` → live immediately. Then run the
[round trip](#resolve--enable--sync--verify).

### Fallback: the seed path (bootstrap / not an admin)

When the direct REST write returns `403`/`401`, land the item via the git seed
instead: commit the new `catalog/skills/<name>/` (or `.claude/agents/<name>`)
to `main`, then re-seed the deployed catalog:

```bash
npm run build -w @harness/backend && \
  HARNESS_TABLE=harness AWS_REGION=us-east-1 node infra/scripts/seed-all-orgs.mjs
```

`seed-all-orgs.mjs` reads every `catalog/skills/*/SKILL.md` + `bundles.json` and
upserts each org's catalog (idempotent). Requires local AWS credentials for the
`harness` table; if it fails with AccessDenied, run the `seed-skills` GitHub
Action instead and don't claim it's live until the seed succeeds.
