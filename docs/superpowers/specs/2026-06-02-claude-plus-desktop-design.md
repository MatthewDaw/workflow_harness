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
Chosen because it is the only option that makes the coupling reach compile time
while remaining a single cross-platform codebase:

- **Source-level coupling.** The desktop app lives as a new `main` package
  *inside the existing `wrapper/` Go module* (e.g. `cmd/claude-plus-desktop`),
  linking the same `internal/daemon`, `internal/pty`, `internal/transport`. A
  change to the attach protocol breaks both `main`s at compile time — they
  cannot drift. (Tauri/Electron would run the Go core as a sidecar process with
  hand-maintained types across the boundary — looser by construction.)
- **Type-level coupling.** Wails auto-generates TypeScript types and JS bindings
  from bound Go methods and structs. `daemon.SessInfo`, `daemon.Frame`, and the
  event envelope become real TS types in the webview — the protocol is
  type-checked end-to-end, Go → JS, with no hand-written glue.
- **Runtime coupling.** Both frontends attach to the **same per-repo daemon**
  via the existing `daemon.EnsureDaemon`/`Dial`. They share live sessions: drive
  a session in the GUI, detach, re-attach from the terminal, and it is right
  there. The CLI remains the single entry point (`claude+ --gui` launches the
  desktop app).

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
| `internal/daemon` (existing) | Daemon, attach protocol, registry | unchanged |
| Local event stream (new frame type) | Surfaces captured structured events to *local* attach clients | `internal/capture`, `internal/transport`, `internal/daemon/attach.go` |

### Data flow

- **Live session:** webview `xterm.js` → bound `SendInput`/`Resize` → attach
  `input`/`resize` frames → daemon → PTY. PTY `output` frames → bound event →
  `xterm.js.write()`.
- **Session list / tabs:** bound `ListSessions` → attach `sessls`/`sessack` →
  `[]SessInfo` → native panel. (No new core work.)
- **Event-stream panel:** the one piece of new core plumbing. Captured events
  today flow `capture → transport → remote Command HQ` only. Add a new attach
  frame (e.g. `FrameEvent`) so local clients can subscribe to the same
  already-produced event stream. Bounded: the events exist; this exposes them
  locally.

## Cross-platform & build strategy

- Wails uses each OS's system webview: **WebView2** (Windows), **WKWebView**
  (macOS), **WebKitGTK** (Linux). Small binaries; no bundled Chromium.
- **Single codebase, per-OS build.** One source tree and one Wails config; Wails
  cross-compilation is limited, so binaries are produced by a **CI matrix**
  (extend the existing `.github/workflows/release.yml`). "Single codebase" is
  satisfied; only the build runners differ.
- **Linux WebKitGTK is the fussiest** of the three. Add an early Linux smoke
  test (render + attach) so quirks surface up front, not at release.

## Shared React components with Command HQ (phased)

Reusing `packages/web` (Command HQ) components in the desktop webview is a real
"share as much as possible" win, but it couples the desktop to the *web app*,
not to the CLI — which is not the stated priority. Therefore:

- **v1:** desktop builds lean native panels, *structured* (props/data shapes) so
  Command HQ components can be dropped in later without rework.
- **Later:** factor `packages/web` into a shared component library imported by
  both the browser app and the desktop webview.

*(Assumption flagged for user override: if web-UI reuse is actually a v1
priority, we pull the shared-component-library work forward.)*

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

- **In scope (v1):** Wails app in the existing module; attach to shared daemon;
  `xterm.js` live pane; native session-list/tabs and event-stream panels; local
  event frame; `claude+ --gui` launch; CI matrix build + smoke.
- **Out of scope (v1):** factoring the shared React component library; new
  features not present in the terminal client; remote/over-SSH desktop use
  (desktop targets the local daemon; remote stays the CLI's domain for now);
  auto-update/installer polish beyond basic signed builds.

## Risks

- **Linux WebKitGTK** rendering/dependency quirks → early smoke test.
- **Wails binding ergonomics** for a high-rate `output` byte stream → may need a
  streaming/event channel rather than per-call returns; validate early.
- **Code signing/notarization** (macOS especially) → standard but time-consuming;
  budget for it in packaging.

## Effort estimate

Roughly **1–3 weeks for a solid v1**. The expensive, risky work (cross-platform
PTY, daemon lifecycle, session muxing, capture) is already built and tested; the
new work is a presentation layer over an interface that already exists, plus one
bounded core addition (local event frame).

## Open questions for review

1. Confirm: desktop attaches to the **shared** per-repo daemon (not its own
   in-process sessions). *(Recommended; assumed throughout.)*
2. Confirm the **phased** shared-React-component approach (lean v1, share later),
   or pull it forward into v1.
3. Window/session UX: one window per repo (mirrors one daemon per repo), or one
   window with a repo switcher? *(Leaning one-window-per-repo for v1 to mirror
   the daemon model.)*
