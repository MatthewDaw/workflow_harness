# Close a session (✕) + terminate-all-on-quit

**Date:** 2026-06-03
**Status:** Approved design, pending implementation plan
**Area:** claude+ desktop app (`wrapper/cmd/claude-plus-desktop`) + daemon attach protocol (`wrapper/internal/{daemon,pty,desktop}`)

## Problem

The claude+ desktop app has no way to close a single Claude session, and quitting
the app leaves the background daemon and all its sessions running (the daemon is a
separate process the app merely attaches to). Two user-facing gaps:

1. **No ✕ on a session row.** The Sessions sidebar supports new (`+`), focus
   (click), and rename (double-click) only — there is no close control, and no
   `Close`/`Kill` method anywhere on the desktop bridge or attach client.
2. **Quit ≠ terminate.** Closing the desktop window tears down nothing; the daemon
   and its PTY sessions keep running in the background (detach-like behavior).

## Goals

- A small `✕` on each session row that **force-kills** that session immediately,
  no confirmation.
- Quitting claude+ **terminates every session and stops the daemon process** —
  nothing claude+ is left running afterward.
- After quitting, work is **resumable with plain `claude --resume` / `claude -c`**
  run directly in the terminal (no new claude+ resume machinery).

## Non-goals

- No "claude+ remembers and re-attaches sessions on next launch" flow. Resume is
  the underlying `claude` CLI's own transcript-resume, used outside claude+.
- No confirmation dialogs. ✕ behaves like closing a browser tab.
- No graceful (SIGTERM-with-timeout) shutdown of an individual session — ✕ is a
  hard kill. (Quit *does* go through the graceful per-child `Session.Close()` path
  via `mux.CloseAll()`, but that is the existing behavior, not new work.)
- No multi-client session-list broadcast. A kill acks the **requesting** client's
  list only — consistent with how `NewSession`/`Rename` already behave. (The
  desktop is effectively the single attached client.)

## The shaping constraint

The daemon is a **separate process** (`EnsureDaemon` spawns it; the desktop
attaches over a loopback TCP socket). Therefore neither feature can be done in the
frontend alone — both require a new **control frame** on the attach protocol so the
desktop can instruct the daemon. This bumps `ProtocolVersion` **2 → 3**. The
existing handshake auto-retires a stale v2 daemon on the next attach
(`Dial` → `errProtocolMismatch` → `stopStale` → `EnsureDaemon`), so no manual
cleanup is required. (As with any protocol bump, a running v2 daemon's live
sessions are lost when it is retired on upgrade — accepted, existing behavior.)

## Design

### Feature 1 — ✕ closes one session (force kill)

Data flow: `Sessions.tsx ✕ → App.CloseSession → Bridge.CloseSession → Client.CloseSession → FrameKill → daemon → Mux.Kill → Session.Close`.

- **Frontend** — `wrapper/cmd/claude-plus-desktop/frontend/src/Sessions.tsx`
  - Add a `✕` element to each session `<li>`. Its `onClick` calls
    `e.stopPropagation()` (so it does not also `Focus` the row) then
    `void CloseSession(s.id)`.
  - Import `CloseSession` from the regenerated `wailsjs/go/main/App` bindings.
  - No empty-state change needed: the existing
    `sessions.length === 0 ? "No sessions — click + to start one"` branch already
    renders when the last session is killed.

- **Protocol** — `wrapper/internal/daemon/attach.go`
  - Add `FrameKill FrameType = "kill"` (client → daemon, uses existing `SessID`
    field).
  - Bump `const ProtocolVersion = 3` and update the doc comment to note v3 added
    `FrameKill` + `FrameShutdown`.

- **Daemon** — `wrapper/internal/daemon/daemon.go` (attach loop `switch f.Type`)
  - New case `FrameKill`: `d.mux.Kill(f.SessID)` then
    `send(Frame{Type: FrameSessAck, List: d.sessInfosFor(clientID)})` so the row
    disappears immediately for the requesting client.

- **Mux** — `wrapper/internal/pty/mux.go`
  - New `Kill(id string)`: synchronously remove the session from `m.sessions` and
    re-focus a neighbor (the same bookkeeping `onSessionExit` performs), then call
    `Session.Close()` on the removed session (kills the child, closes the PTY).
  - Idempotency: the pump goroutine's own EOF-driven `onSessionExit(id)` runs
    afterward and is a harmless no-op because the id is already gone (`idx < 0`
    guard). Extract the shared removal/re-focus bookkeeping into a locked helper so
    `Kill` and `onSessionExit` cannot diverge.

- **Bridge** — `wrapper/internal/desktop/bridge.go`
  - Add `CloseSession(sessID string) error` to the `attachClient` interface and a
    `Bridge.CloseSession(sessID)` method delegating to `b.c.CloseSession`.

- **Client** — `wrapper/internal/daemon/client.go`
  - `Client.CloseSession(sessID string) error` → `writeFrame(FrameKill, SessID)`.

- **App** — `wrapper/cmd/claude-plus-desktop/app.go`
  - `App.CloseSession(sessID string) error` (Wails-bound) → `bridge.CloseSession`.
  - `clientAdapter.CloseSession` → `c.CloseSession`.

#### Last-session behavior (chosen: leave empty)

The daemon auto-spawns a session only at **attach handshake** time
(`attach()` when `mux.Count() == 0`), never on a later empty transition. So
killing the last session while attached leaves the list empty and the terminal
pane blank — exactly the chosen behavior, with **no auto-spawn suppression
needed**. A subsequent detach/reattach still gets a fresh session at handshake
time, which is correct.

### Feature 2 — Quit terminates all sessions and stops the daemon

Data flow: `Wails OnShutdown → App.shutdown → Bridge.Shutdown → Client.Shutdown → FrameShutdown → daemon.Stop()`.

- **Protocol** — `attach.go`: add `FrameShutdown FrameType = "shutdown"`
  (client → daemon).

- **Daemon** — `daemon.go` attach loop: new case `FrameShutdown` → `d.Stop()`
  then `return`. `d.Stop()` already: closes the stop channel, closes the listener
  (so `Serve` returns and the daemon process exits), calls `mux.CloseAll()` (which
  kills every child via `Session.Close()`), and removes the registry record. It is
  idempotent.

- **Client** — `client.go`: `Client.Shutdown() error` writes `FrameShutdown`, then
  waits briefly (short read deadline) for the daemon to close the connection before
  returning — so the desktop process does not race-exit before the frame flushes.

- **Bridge** — `bridge.go`: `Bridge.Shutdown() error` → `b.c.Shutdown()`; add
  `Shutdown() error` to the `attachClient` interface.

- **App / main** —
  - `wrapper/cmd/claude-plus-desktop/main.go`: add `OnShutdown: app.shutdown` to
    the Wails `options.App`.
  - `app.go`: `App.shutdown(ctx context.Context)` → if `a.bridge != nil`,
    `_ = a.bridge.Shutdown()`. `clientAdapter.Shutdown` → `c.Shutdown`.

**Why graceful `FrameShutdown` and not killing the daemon PID directly.**
`stopStale`/`terminatePID` already exist, but on Windows `terminatePID` is a hard
`TerminateProcess` that would **orphan the claude child processes** (they keep
running) — violating "all sessions terminated." Routing through `d.Stop()` →
`mux.CloseAll()` → `Session.Close()` kills each child first, then exits the daemon
cleanly and clears its registry record.

### Feature 3 — Resume with `claude` (no code)

Force-killing a session's PTY does not delete claude's conversation transcripts in
`~/.claude/projects` (the same files the capture layer tails). So after quitting,
`claude --resume` (pick from list) or `claude -c` (continue most recent), run
directly in the terminal, resumes the conversation. This requirement is satisfied
by *killing* rather than *deleting* — no new claude+ code.

## Error handling

- `Mux.Kill` on an unknown id: no-op (look-up miss), no error surfaced — the row
  is already gone from the client's perspective.
- `FrameKill` / `FrameShutdown` are best-effort writes; a write error on a dying
  connection is ignored (matches existing `Detach`).
- `App.CloseSession` / `App.shutdown` when `a.bridge == nil` (startup failed):
  return early / no-op.
- Double shutdown (`d.Stop()` called twice) is safe via the existing stop-channel
  guard.

## Testing

- `wrapper/internal/daemon/version_test.go`: update the expected `ProtocolVersion`
  to 3.
- `pty` mux test: `Kill(id)` removes the session, re-focuses a neighbor, leaves the
  list empty when killing the last one, and is safe when the pump's later
  `onSessionExit` fires for the same id (no panic, no double-remove).
- `desktop/bridge_test.go`: extend the fake `attachClient` with `CloseSession` and
  `Shutdown`; assert `Bridge.CloseSession`/`Bridge.Shutdown` delegate.
- Daemon attach test (if present): a `FrameKill` frame yields a `FrameSessAck` with
  the session removed; a `FrameShutdown` frame stops the daemon (registry record
  removed, socket stops answering).
- Regenerate Wails bindings (`App.d.ts` / `App.js`) so `CloseSession` is callable
  from the frontend; the desktop frontend has no unit test harness, so verify the
  ✕ wiring manually.

## Files touched (summary)

| File | Change |
|------|--------|
| `internal/daemon/attach.go` | `FrameKill`, `FrameShutdown`, `ProtocolVersion = 3` |
| `internal/daemon/daemon.go` | attach-loop cases for `FrameKill`, `FrameShutdown` |
| `internal/pty/mux.go` | `Kill(id)` + shared removal helper |
| `internal/daemon/client.go` | `CloseSession`, `Shutdown` |
| `internal/desktop/bridge.go` | interface + `CloseSession`, `Shutdown` |
| `cmd/claude-plus-desktop/app.go` | `App.CloseSession`, `App.shutdown`, adapter methods |
| `cmd/claude-plus-desktop/main.go` | `OnShutdown: app.shutdown` |
| `cmd/claude-plus-desktop/frontend/src/Sessions.tsx` | ✕ button |
| `cmd/claude-plus-desktop/frontend/wailsjs/go/main/App.{d.ts,js}` | regenerated bindings |
| `internal/daemon/version_test.go`, `internal/pty/*_test.go`, `internal/desktop/bridge_test.go` | tests |
