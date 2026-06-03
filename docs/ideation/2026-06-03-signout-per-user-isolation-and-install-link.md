---
status: design
type: feat
created: 2026-06-03
origin: user request (sign-out + per-user data isolation; claude+ install link)
depth: design
---

# feat: sign-out, per-user data isolation, claude+ install link, and live-session controls in Command HQ

## Summary

Connected changes to Command HQ's web app (`packages/web`), plus a control-plane
slice spanning `packages/shared` → `wrapper` → `packages/backend` for shutting
down a live session:

1. **Explicit Sign-out button** — replace the "click your `@username` to sign out"
   affordance in the top nav with a clearly-labelled control that looks like a
   sign-out button.
2. **Per-user data isolation on sign-out / user-switch** — when one user signs
   out and another signs in **in the same browser**, the second user must never
   see the first user's data. The backend is already scoped per user; the gap is
   that the client (RTK Query) cache and live-WS state are **not reset** on
   sign-out, so cached projects/sessions/agents can briefly render for the wrong
   identity. We reset all cached state on sign-out and on identity change.
3. **claude+ install link, folded into the Link Device screen** — turn the
   existing "Link Device" screen into a short "Get started" / onboarding screen
   that shows how to **install** `claude+` (the `curl | sh` one-liner, the npm
   command, and a releases link) above the existing device-code approval step.
4. **Live-session controls in `/sessions`** (added 2026-06-03, second pass) —
   the cross-project Sessions list gains a **last-activity timestamp** ("how long
   ago a session last streamed") and a **Shut down** action (graceful, with a
   Force fallback) to terminate a running Claude session from HQ. Both are scoped
   to the caller's own sessions, which we **verify** is already enforced
   end-to-end. See "Part 2 — Live-session controls" below.

The concrete user-acceptance phrasing: _"if I create a login with
`mattdaw7@gmail.com` I won't see anything that `bill@cow.com` is creating on his
profile,"_ and _"make the sign-out button look like an actual sign-out button."_

This is a small, focused change. No new backend endpoints are required for the
isolation fix; the work is mostly in `packages/web` plus a verification pass on
the WS feed and one product decision about org-tier data (below).

---

## Problem Frame

### What already works

A 2026-06-03 read of the codebase confirms the backend REST is **already scoped
per authenticated principal**:

- `GET /projects` → `repo.listProjectsForUser(principal.userId)` (owner GSI;
  never a full scan). `rest/projects.ts`
- `GET /sessions` → `repo.listLiveSessions()` then
  `.filter(s => s.ownerUserId === principal.userId)`, and the firehose is
  gathered from the caller's own projects. `rest/sessions.ts`
- `GET /projects/:id`, `GET /sessions/:id`, `POST /sessions/:id/control` →
  return **404** when the resource is not owned by the caller (no id
  enumeration, no cross-tenant read/write). `rest/projects.ts`, `rest/sessions.ts`
- Agents/Skills → scope-checked reads/writes (`canReadScope`/`canWriteScope`),
  org-tier writes gated on the `custom:admin` claim. `rest/scopeauth.ts`

`AuthProvider` already implements `signOut()` (calls the client's `signOut`,
clears `user`, clears the id token), and `AppShell` already has a sign-out
trigger — it's just disguised as the `@username` label.

### The actual gaps

1. **Sign-out is not discoverable.** In `AppShell.tsx` the only way to sign out
   is clicking the `@username` text in the nav's right corner. It does not look
   like a button. (Wireframe screens variously show `@matt`, `user ▾ @matt`, or
   nothing — there is no real control.)

2. **Client cache is not reset on sign-out (the real data-bleed).**
   `AuthProvider.signOut()` does **not** dispatch `baseApi.util.resetApiState()`.
   RTK Query keeps every cached query (`getProjects`, `getSessions`,
   `getAgents`, objectives, etc.) in the Redux store across a sign-out. When a
   second user signs in **in the same tab**, already-mounted components can
   render the previous user's cached results until a refetch completes — and
   any cache entry not actively re-subscribed can persist. The new id token
   means _new_ requests return the correct user's data, but the residual cache
   is a real, visible cross-user leak. Sign-out must wipe it.

3. **Live WebSocket state may carry across identities.** `AppShell` opens the WS
   on auth and tears it down on sign-out, but live-feed data already merged into
   the api cache rides along with #2. We reset it as part of the same cache
   wipe, and re-open the socket fresh under the new identity.

4. **Org-tier data is shared within an org (product decision needed).**
   Objectives are org-scoped (`repo.listObjectives(org)`), and org-tier
   agents/skills are shared among org members **by design**. In the current
   sign-in clients **every** login is assigned the same org (`mockClient` hard-codes
   `org: 'acme'`; `cognitoClient` reads a single `VITE_ORG`). So two distinct
   emails would land in the **same** tenant and _would_ share objectives —
   contradicting the "separate profiles" example. See the Decision below.

5. **No install affordance anywhere in HQ.** The installer exists
   (`scripts/install.sh` = `curl | sh`; `npm i -g claude-plus`; GitHub
   releases), and HQ has a Link-Device screen, but nothing tells a new user how
   to _get_ `claude+` in the first place.

---

## Decision: what "their own data" means

We adopt **per-user isolation as the default boundary**, with org-tier data
shared only among genuine members of the same org:

- **Per-user (already enforced server-side):** projects, sessions, instances,
  agents/skills authored at user scope. These are keyed by `ownerUserId` and
  are already isolated.
- **Org-tier (shared by design):** objectives, org-scoped agents/skills, the
  org Definition of Done. Shared among members of the same org.

To make the `mattdaw7@gmail.com` vs `bill@cow.com` example behave as the user
expects (fully separate profiles, including objectives), **distinct logins must
resolve to distinct orgs** unless they were deliberately provisioned into the
same org. Minimal, non-invasive approach:

- **Mock/dev client:** derive `org` from the email/username (e.g. the email
  domain, or `org-<username>`) instead of the hard-coded `'acme'`, so two
  different logins are two different tenants in local/demo use. This is what
  makes the demo show true isolation end-to-end.
- **Cognito (real):** keep org from the verified token claim (`custom:org` /
  configured pool), which is the correct multi-tenant source of truth. No
  change to the trust model; we only stop _defaulting_ everyone to one org in
  the mock.

This keeps the change small and avoids inventing a full org-provisioning/signup
flow (explicitly out of scope — see Non-Goals).

---

## Requirements (trace)

| Req                                                  | Source       | Advanced by                             |
| ---------------------------------------------------- | ------------ | --------------------------------------- |
| Sign-out is an obvious, labelled button              | user         | C1 (AppShell), W1 (wireframe nav)       |
| Signing out wipes all client-cached data             | user         | C2 (AuthProvider resetApiState)         |
| A different user signing in sees only their own data | user         | C2 + C3 + verified server scoping       |
| Live WS state does not bleed across identities       | audit        | C3 (reset + reconnect)                  |
| Distinct logins are distinct profiles in dev/demo    | user example | C4 (mock org derivation)                |
| HQ shows how to install claude+                      | user         | C5 (Get-started screen), W2 (wireframe) |
| Device-code approval still works (unchanged)         | existing     | C5 (kept below install)                 |
| `/sessions` shows when each session last streamed    | user         | S1 (last-activity column)               |
| User can shut down a running Claude session from HQ  | user         | S2 (shutdown), S3 (wrapper), S4 (web)   |
| Force-terminate a session that won't exit gracefully | user         | S3 (SIGTERM→timeout→kill escalation)    |
| A user only ever sees / steers their own sessions    | user         | S5 (verified, already enforced)         |
| All currently-green suites stay green                | repo norm    | every unit's Verification               |

---

## Components & Changes

Scoped to `packages/web` except the wireframe. Each unit is independently
testable.

### C1 — Explicit Sign-out button (`components/AppShell.tsx`)

Replace the clickable `@username` with a small user cluster: the `@username`
label (non-interactive, or a future menu trigger) **and** a distinct
**`Sign out`** button styled like the other `hq-btn` controls. Clicking it calls
the existing `signOut()` from `useAuth()`.

- _Interface unchanged:_ still consumes `useAuth().signOut`.
- _Test:_ `AppShell` renders a control with an accessible name "Sign out";
  clicking it invokes the injected client's `signOut`.

### C2 — Reset client cache on sign-out (`auth/AuthProvider.tsx`)

In `signOut()`, after clearing the user and token, dispatch
`baseApi.util.resetApiState()` so every cached query/subscription is discarded.

- _Why here:_ `AuthProvider` already owns the sign-out sequence and has
  `dispatch`. One added dispatch closes the leak deterministically.
- _Test:_ seed the store with a cached query result; call `signOut()`; assert
  the api slice is empty (no residual entries for the prior user).

### C3 — Reset on identity change + fresh WS (`auth/AuthProvider.tsx`, `components/AppShell.tsx`)

Defensive belt-and-suspenders for the OAuth path, where a redirect can switch
accounts without an explicit sign-out:

- In `AuthProvider`'s `onChange`/initial-resolve effect, when the resolved
  `userId` **differs** from the previously-held one, dispatch
  `resetApiState()` before adopting the new token.
- `AppShell`'s WS effect already keys on `user`/`token`; ensure it tears down
  and reconnects on identity change (it does, via the effect deps) so the live
  feed is re-established under the new identity against a clean cache.
- _Test:_ simulate `onChange` resolving a different `userId`; assert cache reset
  fired and the new token was set.

### C4 — Distinct orgs per login in dev/demo (`auth/mockClient.ts`)

Derive `org` from the username/email instead of the constant `'acme'` (e.g.
`org` = email domain, falling back to `org-<username>`), so `mattdaw7@gmail.com`
and `bill@cow.com` are different tenants locally. Cognito path keeps the
token-derived org.

- _Test:_ mock `signIn('mattdaw7@gmail.com', …)` and `signIn('bill@cow.com', …)`
  yield different `org` values; existing default-user test updated.

### C5 — Get-started screen: install claude+ + link device (`screens/LinkDevice/LinkDevice.tsx`, nav)

Rework the Link-Device screen into a two-part **Get started** screen:

- **Install claude+** (new, on top): the three install options as copy-able
  command blocks —
  - `curl -fsSL https://raw.githubusercontent.com/workflow-harness/claude-plus/main/scripts/install.sh | sh`
  - `npm i -g claude-plus`
  - a "Download a release" link to the GitHub releases page —
    each with a Copy button. Sourced verbatim from `scripts/install.sh` /
    `npm/claude-plus` so the page can't drift from the real installer.
- **Link a device** (existing, below): the user-code input + Approve flow,
  unchanged in behavior.
- Nav: the existing `link-device` route stays; its **label** becomes
  "Get started" (or keep "Link Device" — see Open Question) and it's reachable
  from the nav. The route path is unchanged so deep links keep working.
- _Test:_ the screen renders the three install commands and a Copy button; the
  device-approval test continues to pass against the same `useApproveDeviceMutation`.

### W1/W2 — Wireframe (`wireframe.html`)

- **W1:** add an explicit **Sign out** button to the canonical web top-nav
  (Projects screen) and note it applies app-wide; add a new **HQ — Sign in**
  screen so the signed-out → signed-in → scoped-data story is visible.
- **W2:** add a new **HQ — Get started (install claude+ & link device)** screen
  matching C5, and wire it into the prototype's screen nav + click router
  (Sign out → Sign in; "Get started"/"Link device" nav → the new screen).

---

## Data Flow (sign-out → new user)

1. User clicks **Sign out** (C1) → `useAuth().signOut()`.
2. `AuthProvider.signOut()` (C2): `client.signOut()` → `setUser(null)` →
   `setIdToken(null)` → `dispatch(baseApi.util.resetApiState())`.
3. `AppShell` WS effect sees `user === null` → `wsDisconnect()`.
4. `LoginGate` sees no user → renders the sign-in form (W1 screen).
5. New user signs in → new id token in the store; cache is already empty.
6. `AppShell` WS effect re-opens the socket with the **new** token.
7. Every RTK Query refetches from scratch under the new identity; server scoping
   (`principal.userId` / org) returns only that user's data. No residual cache.

---

## Error Handling

- **Sign-out client error:** if `client.signOut()` throws, still clear local
  user/token and reset the cache (never strand a half-signed-out session showing
  stale data). Surface a non-blocking toast/log; the gate falls back to the
  sign-in form regardless.
- **Copy-to-clipboard unsupported:** the install commands remain visible and
  selectable; the Copy button degrades to a no-op with a "select to copy" title
  rather than erroring.
- **Device approval** error/not-found behavior is unchanged (existing
  `result` states: `approved` / `notfound` / `error`).

---

## Testing

- **Unit (vitest, `packages/web`):**
  - C1: AppShell renders an accessible "Sign out" button; click → client.signOut called.
  - C2: store seeded with cached data → signOut → api slice empty.
  - C3: onChange with a changed userId → resetApiState fired + new token set.
  - C4: two different emails → two different orgs from the mock client.
  - C5: Get-started screen shows the three install commands + Copy; device flow unchanged.
- **Verification pass (no new tests, confirm during impl):** the WS
  subscribe/event handlers (`backend/src/ws/*`) authorize the live feed by
  session ownership, so a second identity cannot subscribe to the first's live
  events. If a gap is found, scope it the same way REST already is (404/deny on
  non-owner) and add a test.
- **Whole-suite gate:** `npm test` (shared/backend/web/infra) and
  `go test ./...` (wrapper) stay green; coupled tests are _updated_, not dropped.

---

---

# Part 2 — Live-session controls (`/sessions`)

_Added 2026-06-03 (second pass). Three requirements: show how long ago each
session last streamed; let the user shut down a running session (graceful +
force); confirm a user only ever sees their own sessions. The third is a
**verification** task — the boundary already exists — so this part is mostly the
first two._

## What already exists (so we don't rebuild it)

- `SessionProjection.lastEventAt` (epoch ms) is already folded from the event
  stream by the projection (`ws/projection.ts`, `dto.ts`). The `/sessions` table
  just never rendered it.
- A control plane already runs HQ → daemon: `POST /sessions/:id/control` and the
  WS `control` route validate the action against `controlActionSchema`, authorize
  that the caller **owns** the session, resolve the owning daemon connection via
  the `instanceId` reverse index, and post a `ControlFrame` the wrapper's
  `transport.Receiver` applies. Actions today: `inject | pause | interrupt`.
- The wrapper can already terminate a session: `pty.Session.Close()` does a hard
  `Process.Kill()`. `Mux.Get(id)` resolves a session by id.

## S1 — Last-activity column (`screens/Sessions/SessionsTable.tsx`)

Add a **last activity** column rendering `lastEventAt` as relative time
("just now", "3m ago", "2h ago", "done 1d ago"). Two small additions:

- `relativeTime(ts, now)` pure helper (web utils) → human string from two epoch-ms
  values. Unit-tested in isolation.
- `useNow(intervalMs = 15000)` hook returning a periodically-updated `Date.now()`
  so labels freshen without a refetch. The table computes each row's label from
  `now` and `s.lastEventAt`.

Both the cross-project `Sessions` screen and `ProjectDetail/ProjectSessions`
render through `SessionsTable`, so both inherit the column. No backend or DTO
change — the field already ships in the projection.

## S2 — `shutdown` + `kill` control actions (`packages/shared/src/dto.ts`)

Extend `CONTROL_ACTIONS` to `['inject','pause','interrupt','shutdown','kill']`.
This is the single source of truth shared by the web sender, the backend control
routes (REST + WS), and the wrapper receiver — so adding the actions here makes
the REST/WS layers accept and route them **with no route changes** (they already
validate against `controlActionSchema` and authorize by ownership).

- `shutdown` — graceful terminate: end the Claude process cleanly, escalating to
  force only if it doesn't exit in time (handled daemon-side, S3).
- `kill` — immediate force terminate (explicit user override / Force button).

## S3 — Wrapper: graceful shutdown with force escalation (`wrapper`)

- **`pty.Session.Shutdown(timeout)`** (new): send `SIGTERM` to the child
  (`cmd.Process.Signal(syscall.SIGTERM)`), wait up to `timeout` (~5s) for the
  process to exit, and if it's still alive escalate to the existing force path
  (`Close()` → `Process.Kill()`). Sets `StatusDone` and closes the PTY exactly
  like `Close()`. Idempotent against an already-closed session.
- **`transport` interface**: extend `SessionWriter` (or add a sibling capability)
  so the `Receiver` can terminate a session by id, backed by `*pty.Mux`
  (`Get(id)` then `Shutdown`/`Close`). The mux's existing `pump`/`onSessionExit`
  path already removes a dead session and re-focuses a neighbor, so no extra
  bookkeeping.
- **`transport/control.go`**: add cases:
  - `ActionShutdown` ("shutdown") → `session.Shutdown(5s)`.
  - `ActionKill` ("kill") → `session.Close()`.
  Unknown actions keep NACKing (unchanged). Errors surface via the existing
  `Nack` hook.

Windows note: `syscall.SIGTERM` is not deliverable to a child on Windows the way
it is on Unix; the wrapper's signal-style controls are already Unix-oriented
(`detach_unix.go`). On platforms without graceful-signal support, `shutdown`
falls back to the force path (`Close()`), which is correct behavior, just not
graceful. This keeps the cross-platform contract simple.

## S4 — Web: Shut-down action (`SessionsTable.tsx` + control mutation)

On each **non-`done`** row, add a **Shut down** control next to the
watch/reply/replay link:

- Primary **Shut down** → `sendControl(sessionId, 'shutdown')` behind a confirm
  ("End this session?"). Reuses the existing control mutation (the same one the
  session detail screen uses for inject/pause/interrupt).
- Secondary **Force** (shown after a failed/again click, or as a small adjacent
  affordance) → `sendControl(sessionId, 'kill')`.
- After a successful POST (202) the row's status will transition to `done` via
  the live projection; no optimistic mutation needed beyond disabling the button
  while in-flight.

## S5 — Verify per-user scoping (verification, not new code)

Confirm — and document — that a user can only ever see and steer their **own**
sessions. Already enforced end-to-end:

- `GET /sessions` filters `s.ownerUserId === principal.userId`; the firehose is
  gathered from the caller's own projects. `rest/sessions.ts`
- `GET /sessions/:id` and `POST /sessions/:id/control` → **404** for non-owners
  (no enumeration). `rest/sessions.ts`
- WS `subscribe` and `control` → **403** for non-owners before any listener
  registration or routing. `ws/subscribe.ts`, `ws/control.ts`
- Sessions are stamped with `ownerUserId = conn.userId` at creation, from the
  authenticated daemon connection. `ws/event.ts`

Deliverable: this subsection (the citation), plus **one** added backend test
asserting a non-owner `shutdown` control frame is rejected (403 WS / 404 REST) —
proving the new actions inherit the existing ownership gate. No hardening unless
that test surfaces a real gap.

## Testing (Part 2)

- **Web (vitest):** `relativeTime` unit cases (seconds/minutes/hours/days, done);
  `SessionsTable` renders the last-activity label and a **Shut down** button on
  live rows; clicking it calls the control mutation with `'shutdown'` (and
  **Force** with `'kill'`); `done` rows show no shut-down action.
- **Wrapper (`go test ./...`):** `apply(shutdown)` terminates a session and, when
  the child ignores SIGTERM, escalates to kill within the timeout; `apply(kill)`
  closes immediately; an unknown action still NACKs; a `shutdown` for a missing
  sessionId errors without panicking.
- **Shared:** `controlActionSchema` accepts `shutdown` and `kill`.
- **Backend:** a non-owner `shutdown` frame is 403 (WS) / 404 (REST).
- **Whole-suite gate:** `npm test` (shared/backend/web/infra) and `go test ./...`
  stay green.

## Non-Goals (YAGNI)

- No new backend endpoints for isolation (server scoping already exists).
- No org-provisioning / multi-tenant signup / invite flow. (We only stop the
  mock from defaulting everyone to one org.)
- No new "dashboard" landing screen — the existing Objectives/Projects landing,
  scoped to the user, is the dashboard. (Confirmed with the user.)
- No change to the device-auth protocol or the installer itself.
- No SSO/account-switching UI beyond sign-out → sign-in.
- **Part 2:** no new session lifecycle beyond `shutdown`/`kill` (no restart,
  pause-to-disk, or scheduled termination); no per-event "last streamed" history
  UI beyond the single relative-time label; no new backend control routes (the
  new actions reuse the existing REST + WS control gateway).

---

## Open Questions

1. **Nav label** for the combined screen: keep **"Link Device"**, or rename to
   **"Get started"** (clearer for install-first onboarding)? The route path stays
   `link-device` either way. _(Leaning "Get started".)_
2. **Org derivation in the mock** — domain (`gmail.com`) vs `org-<username>`?
   Domain is more realistic for a demo (two domains = two orgs) but groups all
   `@gmail.com` users together. _(Leaning email-domain, since the user's example
   uses two different domains.)_
3. **Releases URL** — confirm the public repo path for the "Download a release"
   link (installer references `workflow-harness/claude-plus`).
