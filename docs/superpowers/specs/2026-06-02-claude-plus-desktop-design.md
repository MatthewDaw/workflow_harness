# claude+ Desktop — Design Spec

- **Date:** 2026-06-02
- **Status:** Draft (awaiting user review)
- **Topic:** A desktop GUI for claude+ that shares one codebase and one running
  daemon with the existing terminal CLI, on Windows, macOS, and Linux.

## Goal

Ship a native desktop application for claude+ that is "basically the same" as
the terminal CLI, sharing as much as possible. The terminal version already
exists (`wrapper/`, a Bubble Tea TUI over a per-repo daemon). This effort adds a
graphical frontend that reuses the same Go core and attaches to the same daemon,
so the two are one product in two shells.

## Hard constraints (from the user)

1. **Single codebase for all systems.** One source tree, one build configuration,
   produces the app for Windows, macOS, and Linux. No per-OS forks.
2. **Tight coupling to the CLI.** The desktop app and the CLI must not be able to
   silently drift. Coupling at compile time is preferred over coupling by
   convention.
3. **Cross-platform.** Must run on Windows, macOS, and Linux.

## Background: the architecture already supports this

The existing `wrapper/` module has a clean **daemon ⇄ client split**:

- **Core (the daemon).** A per-repo, detach-surviving background process
  (`internal/daemon`) that hosts the real `claude` CLI over a PTY
  (`internal/pty`), multiplexes auto-named sessions, captures events
  (`internal/capture`), and streams them outbound to Command HQ
  (`internal/transport`).
- **Attach protocol** (`internal/daemon/attach.go`). Newline-delimited JSON
  control frames plus raw PTY byte passthrough over loopback TCP. It already
  carries **structured** data (`sessls`/`sessack` → `[]SessInfo` with id, name,
  focused, status) *and* raw bytes (`input`/`output`) for the live session.
- **Terminal frontend** (`internal/tui`). A Bubble Tea TUI that is *just a
  client* of the attach protocol.

The frontend is therefore already decoupled from the core. A desktop app is a
**second attach client** — the same role the terminal TUI plays today.

## Decision: Wails, one Go module, one shared daemon

**Stack: [Wails](https://wails.io) (Go backend + system webview frontend).**
The load-bearing coupling property — a desktop `main` in the same Go module
linking `internal/daemon`, so a protocol change breaks the build — comes from
*any* Go-hosted webview, not from Wails specifically. Wails is chosen on top of
that for its TS-binding generation, window lifecycle, and cross-platform webview
management; a raw webview binding (e.g. `webview/webview`) would give the same
module coupling with worse ergonomics. The coupling reaches compile time while
remaining a single cross-platform codebase:

- **Source-level coupling.** The desktop app lives as a new `main` package
  *inside the existing `wrapper/` Go module* (e.g. `cmd/claude-plus-desktop`),
  linking the same `internal/daemon`, `internal/pty`, `internal/transport`. A
  change to the attach protocol breaks both `main`s at compile time — they
  cannot drift **within a single build**. (Tauri/Electron would run the Go core
  as a sidecar process with hand-maintained types across the boundary — looser
  by construction.)
  - **Runtime version skew (caveat).** Compile-time coupling only binds the
    daemon and client built *together*. The daemon is a detach-surviving process
    that may be an **older build** still running when a newer client launches; a
    protocol change (e.g. adding `FrameEvent`) is then a silent runtime mismatch,
    not a compile error — the current attach loop ignores unknown frame types and
    the wire `Frame` has no version field. Mitigation: add a protocol version to
    the Hello/Ack handshake and, on mismatch, prompt to restart the daemon
    (`EnsureDaemon` already owns lifecycle).
- **Type-level coupling.** Wails auto-generates TypeScript types and JS bindings
  from bound Go methods and structs, so the **desktop-Go ⇄ webview** boundary is
  type-checked (`daemon.SessInfo` and the event envelope become real TS types)
  with no hand-written glue. Two limits to keep honest: this does *not* type the
  desktop-Go ⇄ daemon TCP hop (that is guarded by the same-module Go types on the
  desktop side, not by TS), and `Frame.Data` is an opaque base64 PTY payload, not
  typed content.
- **Runtime coupling.** Both frontends attach to the **same per-repo daemon**
  via the existing `daemon.EnsureDaemon`/`Dial`. **Today this is sequential
  re-attach only** (attach → detach → re-attach, the case `daemon_test.go`
  covers): `pty.Mux` holds a single global `focusIdx` and a single `cols/rows`,
  so two clients attached *at once* would share one focus and one PTY geometry —
  the desktop window and the terminal would clobber each other's focus and size.
  Making concurrent terminal+desktop attach work (the "share a live session"
  experience) requires **per-client focus and per-client PTY sizing in
  `pty.Mux`** plus a concurrent-attach test — real core work, not zero. v1 may
  instead scope to one active client at a time (see Open Questions). The CLI
  remains the entry point (`claude+ --gui`).

The live `claude` surface renders in a real terminal widget (`xterm.js`) for
full fidelity; the chrome around it (tabs, session list, event stream, status)
is native GUI.

## Architecture

```
   ┌─────────────────────┐         ┌──────────────────────────┐
   │  Terminal claude+   │         │     Desktop claude+      │
   │  (Bubble Tea TUI)   │         │  Wails: Go ⇄ webview     │
   │   internal/tui      │         │  native chrome +         │
   │                     │         │  xterm.js live pane      │
   └──────────┬──────────┘         └─────────────┬────────────┘
              │     attach protocol (JSON frames + PTY bytes)  │
   ───────────┴────────────────────────────────────────────── │  ← shared, exists
                       per-repo daemon (internal/daemon)
        internal/pty · internal/capture · internal/transport · internal/config
```

Both `main` packages compile from the same module. The desktop's Go side reuses
the daemon client library verbatim and exposes bound methods to a React webview.

### Components and boundaries

| Unit | Responsibility | Depends on |
|---|---|---|
| `cmd/claude-plus-desktop` (new) | Wails app entry; binds Go methods to the webview; manages window lifecycle | `internal/daemon` client, Wails runtime |
| Desktop Go bridge (new, e.g. `internal/desktop`) | Adapts the attach client into bound methods/events the webview consumes (attach, focus, send input, list sessions, subscribe to events) | `internal/daemon`, `internal/event` |
| Webview frontend (new, `cmd/claude-plus-desktop/frontend`) | React app: native panels + `xterm.js` pane; calls generated bindings | Wails-generated TS bindings |
| `internal/daemon` (existing) | Daemon, attach protocol, registry | **modified** — see event tap below |
| Local event stream (new, **daemon-core work**) | A multi-consumer event tap in `daemon.Runtime` feeding both `transport` (HQ) and a new per-client `FrameEvent` attach sink; today `capture` emits to a single hard-wired sink | `internal/capture`, `internal/transport`, `internal/daemon` |

### Data flow

- **Live session:** webview `xterm.js` → bound `SendInput`/`Resize` → attach
  `input`/`resize` frames → daemon → PTY. PTY `output` frames → bound event →
  `xterm.js.write()`.
- **Session list / tabs:** bound `ListSessions` → attach `sessls`/`sessack` →
  `[]SessInfo` → native panel. (No new core work.)
- **Event-stream panel:** **net-new core work, not terminal parity.** The
  terminal client renders *no* live event stream today (`Client.Run` handles only
  `output`/`sessack`; the `tui` `StreamTab` is not wired to the attach protocol),
  so this is a new surface, not a port. Captured events flow
  `capture → transport → remote Command HQ` only, via a single hard-wired sink,
  and capture is **gated on HQ credentials** (a local-only daemon with no
  `claude+ login` emits zero events). Surfacing them locally requires a
  multi-consumer event tap in `daemon.Runtime`, a new `FrameEvent`, and running
  capture without HQ credentials. **Open decision:** cut from v1 (true terminal
  parity) or build it as the desktop's first net-new feature — see Open
  Questions.

## Cross-platform & build strategy

- Wails uses each OS's system webview: **WebView2** (Windows), **WKWebView**
  (macOS), **WebKitGTK** (Linux). Small binaries; no bundled Chromium.
- **Single codebase, per-OS build.** One source tree and one Wails config, but
  Wails links native CGO webview libraries (WebView2 loader, WKWebView,
  WebKitGTK) and **cannot ride the existing single-runner goreleaser
  cross-compile** that builds the pure-Go CLI. The desktop binary needs a
  **separate per-OS native build matrix** (windows/macos/ubuntu runners, each
  running `wails build` with Node + the platform webview SDK), added alongside —
  not inside — the current goreleaser job in `.github/workflows/release.yml`.
  "Single codebase" still holds; only the build runners differ.
- **Linux WebKitGTK is the fussiest** of the three. Add an early Linux smoke
  test (render + attach) so quirks surface up front, not at release.

## Shared React components with Command HQ (phased)

Reusing `packages/web` (Command HQ) components in the desktop webview is a real
"share as much as possible" win, but it couples the desktop to the *web app*,
not to the CLI — which is not the stated priority. Therefore:

- **v1:** desktop builds the leanest native panels that satisfy v1 — no attempt
  to pre-shape props for a future shared library (that future API isn't known
  yet, and designing v1 around it is speculative).
- **Later:** if and when web-UI sharing is prioritized, factor `packages/web`
  into a shared component library imported by both the browser app and the
  desktop webview, refactoring the v1 panels at that time.

*(Whether to reuse Command HQ's web UI at all — vs. native panels — is itself an
open question; see Open Questions.)*

## Error handling

- **Daemon not running / unreachable:** desktop reuses `EnsureDaemon`; on failure
  show an in-window error state with retry, mirroring the CLI's spawn-error
  surfacing.
- **Client disconnect:** detaching closes the window's attach connection; the
  daemon and sessions keep running (existing tmux-model contract). Re-attach on
  relaunch.
- **PTY/session death:** surfaced via `SessInfo.Status`; the live pane shows a
  terminated banner rather than hanging.

## Testing

- **Go bridge:** unit-test the desktop adapter against a fake/in-process daemon,
  reusing existing daemon test fixtures (`daemon_test.go` attach→detach→re-attach).
- **New event frame:** golden-fixture parity with the existing event envelope
  (`internal/event` already has golden-fixture tests).
- **Frontend:** component tests for panels; an integration smoke that boots a
  daemon, attaches the webview bridge, and asserts a session renders.
- **Cross-platform CI:** build + smoke (launch, attach, render) on all three OSes.

## Scope / non-goals (YAGNI)

- **In scope (v1):** Wails app in the existing module; attach to shared daemon
  (single active client — concurrent multi-client is a separate decision);
  `xterm.js` live pane; native session-list/tabs panel; `claude+ --gui` launch;
  per-OS native CI build + smoke. *(Event-stream panel + `FrameEvent`: pending
  the cut-vs-build decision in Open Questions.)*
- **Out of scope (v1):** factoring the shared React component library; new
  features not present in the terminal client; remote/over-SSH desktop use
  (desktop targets the local daemon; remote stays the CLI's domain for now);
  code signing/notarization and installer/auto-update polish (v1 ships unsigned
  developer builds).

## Risks

- **Linux WebKitGTK** rendering/dependency quirks → early smoke test.
- **High-rate `output` byte stream** → push PTY output to the webview via Wails
  runtime events (`EventsEmit`) or a Go channel binding, **not** bound-method
  return values (request/response is the wrong shape for a continuous stream).
  Bound methods stay for request/response control (focus, send input, list
  sessions). Validate throughput early.
- **Code signing/notarization** (macOS especially) → out of scope for v1: v1
  ships **unsigned developer builds** that run locally on each OS to prove the
  architecture. Signing/notarization is a pre-distribution milestone (v1.1),
  budgeted when shipping to end users.

## Effort estimate

Roughly **1–3 weeks for a solid v1** *if v1 scopes to a single active client and
defers the event-stream panel*. Much of the risky work (cross-platform PTY,
daemon lifecycle, session muxing, capture) is already built and tested. But the
estimate must also carry: per-client focus/PTY-sizing in `pty.Mux` (only if
concurrent terminal+desktop attach is wanted), the protocol-version handshake,
the separate per-OS native build matrix, and — if not cut — the event-stream
core tap. Each is a multi-day item, so the low end of the range holds only for
the reduced scope.

## Open questions for review

1. Confirm: desktop attaches to the **shared** per-repo daemon (not its own
   in-process sessions). *(Recommended — but note this gives only sequential
   re-attach unless per-client focus/PTY-sizing is added; see Runtime coupling
   and review item 4 below.)*
2. Confirm the **phased** shared-React-component approach (lean v1, share later),
   or pull it forward into v1.
3. Window/session UX: one window per repo (mirrors one daemon per repo), or one
   window with a repo switcher? *(Leaning one-window-per-repo for v1 to mirror
   the daemon model.)*

## Deferred / Open Questions

### From 2026-06-02 review

These decisions were surfaced by document review and deferred to resolve during
planning. Each should be answered before or while writing the implementation
plan.

1. **(P0) User problem.** What can a user do or feel in the desktop app that the
   terminal CLI doesn't already give them? Every current goal/constraint is build
   mechanics; without a user problem, "basically the same as the terminal" risks
   a terminal-in-a-window nobody adopts. State the job, narrow to a specific
   audience (e.g. non-CLI users), or defer the effort.
2. **(P0) Desktop navigation model.** The TUI has five top-level tabs (agents,
   forge, session, stream, tickets); v1 names only the session-list panel (plus a
   conditional event-stream panel). Decide which tabs are in v1, which are
   deferred (with reason), and which are replaced by a different affordance.
3. **(P1) Native panels vs. wrap Command HQ web.** Re-rank "tight coupling to the
   CLI" against user value, then evaluate the alternative the spec currently rules
   out: wrapping/reusing the existing Command HQ web UI (which already renders a
   session list, event stream, and steer panel) as the desktop surface. The only
   genuinely new core work (the local event tap) is needed either way, so native
   panels are pure additive cost over the web-wrap path — justify it or take it.
   Refines open question 2 above.
4. **(P1) Concurrent attach + `claude+ --gui`.** (a) Does v1 support a terminal
   and desktop client attached to the same daemon at once? If yes, build
   per-client focus/PTY-sizing in `pty.Mux`; if no, define the second-attach
   error/UX. (b) One binary (CLI branches into the Wails runtime) or two (CLI
   execs a sibling `claude-plus-desktop`)? If two: discovery rule + missing-binary
   error. (c) If a window for the repo is already open, focus it rather than spawn
   a second.
5. **(P1) Keyboard & focus model.** Focus hand-off between native chrome and the
   `xterm.js` pane (who owns focus, how it returns); `Ctrl-C` copies-on-selection
   vs. sends SIGINT; paste; right-click menu. Day-one usability if guessed wrong.
6. **(P1) Session lifecycle interactions.** How a user creates (`+`), switches,
   and closes/kills sessions in the GUI; what the `xterm.js` pane does during a
   switch (clear / re-attach); and the empty "no sessions yet" first-launch state.
7. **(P2) Window model.** Confirm one-window-per-repo for v1, then define
   multi-repo UX: window title (repo basename), focus-existing-window on relaunch,
   and how a user opens a second repo. Refines open question 3 above.
8. **(P2) Terminal-pane behaviors.** `xterm.js` resize/reflow on window resize
   (debounce; when a `resize` frame is sent; layout between fixed panels and the
   terminal). WebKitGTK WebGL→canvas renderer fallback and minimum system-webview
   versions per OS are related sub-points.
