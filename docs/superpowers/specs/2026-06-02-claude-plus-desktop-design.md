# claude+ Desktop — Design Spec

- **Date:** 2026-06-02
- **Status:** Reviewed; open decisions resolved — ready for planning.
- **Topic:** A desktop GUI for claude+ that shares one codebase and one running
  daemon with the existing terminal CLI, on Windows, macOS, and Linux.

## Goal

Ship a native desktop application for claude+ that delivers the **same claude+
work in a more polished, graphical UX** than a terminal can provide. The terminal
version already exists (`wrapper/`, a Bubble Tea TUI over a per-repo daemon). This
effort adds a graphical frontend that reuses the same Go core and attaches to the
same daemon, so the two are one product in two shells.

**Audience & user problem.** The desktop is for the *same users and the same
work* as the terminal claude+ — it is not chasing a new audience or a new job. Its
value is a **more advanced UX**: a real window with native panels, mouse-driven
interaction, and full graphical fidelity, instead of a text TUI. "Better
experience for existing work," not "new capability."

## Hard constraints (from the user)

1. **Single codebase for all systems.** One source tree, one build configuration,
   produces the app for Windows, macOS, and Linux. No per-OS forks.
2. **Tight coupling to the CLI.** The desktop app and the CLI must not be able to
   silently drift. Coupling at compile time is preferred over coupling by
   convention. *(This priority is why the desktop builds native panels rather than
   reusing the Command HQ web UI — see Resolved decisions.)*
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

The frontend is decoupled from the core. A desktop app is a **second attach
client** — the same role the terminal TUI plays today. **However**, the existing
protocol only carries enough for the *Session* and *Session-list* surfaces; full
parity (below) requires extending it (see v1 scope).

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
  *inside the existing `wrapper/` Go module* (`cmd/claude-plus-desktop`), linking
  the same `internal/daemon`, `internal/pty`, `internal/transport`. A change to
  the attach protocol breaks both `main`s at compile time — they cannot drift
  **within a single build**.
  - **Runtime version skew (caveat).** Compile-time coupling only binds the
    daemon and client built *together*. The daemon is a detach-surviving process
    that may be an **older build** still running when a newer client launches; a
    protocol change is then a silent runtime mismatch, not a compile error — the
    current attach loop ignores unknown frame types and the wire `Frame` has no
    version field. **Mitigation:** add a protocol version to the Hello/Ack
    handshake and, on mismatch, prompt to restart the daemon (`EnsureDaemon`
    already owns lifecycle).
- **Type-level coupling.** Wails auto-generates TypeScript types and JS bindings
  from bound Go methods and structs, so the **desktop-Go ⇄ webview** boundary is
  type-checked with no hand-written glue. Two honest limits: this does *not* type
  the desktop-Go ⇄ daemon TCP hop (guarded by the same-module Go types on the
  desktop side, not TS), and `Frame.Data` is an opaque base64 PTY payload, not
  typed content.
- **Runtime coupling — concurrent, per-client.** Both frontends attach to the
  **same per-repo daemon** via `daemon.EnsureDaemon`/`Dial`, and v1 supports the
  terminal and desktop **attached and interacting at the same time**. This
  requires new core work: `pty.Mux` today holds a single global `focusIdx` and a
  single `cols/rows`, so concurrent clients would clobber each other. v1 adds
  **per-client focus and per-client PTY sizing** (output reflowed per client) plus
  a concurrent-attach test. This is the "two shells, one live product" payoff.
- **Two-binary entry.** `claude+` (console CLI) and `claude-plus-desktop` (Wails
  GUI) are **separate binaries** built from the one module. `claude+ --gui`
  **discovers and execs** the desktop binary (looked up next to the CLI / on
  PATH), with a clear error when it isn't installed. If a window for the repo is
  already open, `--gui` focuses it instead of spawning a second.

The live `claude` surface renders in a real terminal widget (`xterm.js`) for
full fidelity; the chrome around it is native GUI.

## Architecture

```
   ┌─────────────────────┐         ┌──────────────────────────┐
   │  Terminal claude+   │         │     Desktop claude+      │
   │  (Bubble Tea TUI)   │         │  Wails: Go ⇄ webview     │
   │   internal/tui      │         │  native panels +         │
   │                     │         │  xterm.js live pane      │
   └──────────┬──────────┘         └─────────────┬────────────┘
              │   attach protocol (JSON frames + PTY bytes)    │
              │   ── extended: events, status, agent-diff ──   │
   ───────────┴────────────────────────────────────────────── │  ← shared
                       per-repo daemon (internal/daemon)
        internal/pty · internal/capture · internal/transport · internal/config
                                   │
                              (HQ for Forge/Tickets — desktop Go side
                               also talks to HQ directly, in-module)
```

Both `main` packages compile from the same module. The desktop's Go side reuses
the daemon client library verbatim and exposes bound methods to a React webview;
for HQ-backed surfaces it also reuses the in-module HQ client directly.

## v1 scope: full TUI parity

v1 matches **all five** terminal tabs as native panels, plus the status meters.
Only *Session* and *Session-list* render data the attach protocol exposes today;
the rest require new plumbing, and three surfaces require Command HQ (cloud).
**v1 requires HQ login to be fully functional** — an accepted trade for complete
parity in one release.

| Surface | Data today | New work for the desktop | HQ? |
|---|---|---|---|
| **Session** | PTY `output`/`input` frames | `xterm.js` pane (push output via Wails `EventsEmit`) | No |
| **Session list / tabs** | `sessls`/`sessack` → `[]SessInfo` | native panel | No |
| **Stream** (event feed) | capture envelopes — daemon-local, not exposed | new `FrameEvent` + a **multi-consumer event tap** in `daemon.Runtime` feeding both `transport` and per-client attach sinks; run capture even without HQ creds | No |
| **Agents / Skills** | `internal/config` sync drift (U17) | new `FrameAgentDiff` (or bound call) to surface the sync-diff + a reconcile control | Reconcile pulls from HQ |
| **Forge** | server-side semantic search (U27) | request/response to HQ — desktop Go side calls the in-module HQ client **directly** (reuses credentials/config) | **Yes** |
| **Tickets** | HQ REST tickets | desktop Go side fetches HQ REST directly; "start session on ticket" reuses the existing `new` frame (`Frame.Ticket`) | **Yes** |
| **Status line** | token/cost/drift meters | new `FrameStatus` snapshot (periodic) | Partly |

**New core surface this implies:** per-client focus + PTY sizing in `pty.Mux`;
new attach frames `FrameEvent`, `FrameStatus`, `FrameAgentDiff` (+ a reconcile
control frame); a multi-consumer tap in `daemon.Runtime`; capture running without
HQ credentials. HQ-backed surfaces (Forge, Tickets, agent reconcile) use the
desktop's own in-module HQ client rather than proxying through the daemon — the
coupling payoff of being in the same module.

### Components and boundaries

| Unit | Responsibility | Depends on |
|---|---|---|
| `cmd/claude-plus-desktop` (new) | Wails app entry; binds Go methods to the webview; window lifecycle | `internal/daemon` client, Wails runtime |
| Desktop Go bridge (new, `internal/desktop`) | Adapts the attach client into bound methods/events (attach, focus, input, list, event/status/agent subscriptions); for HQ surfaces, drives the in-module HQ client | `internal/daemon`, `internal/event`, `internal/transport`, `internal/config` |
| Webview frontend (new, `cmd/claude-plus-desktop/frontend`) | React app: native panels for all five tabs + status line + `xterm.js` pane | Wails-generated TS bindings |
| `internal/pty` (modified) | per-client focus + per-client PTY sizing | — |
| `internal/daemon` (modified) | new frames (`FrameEvent`/`FrameStatus`/`FrameAgentDiff` + reconcile); multi-consumer event tap; protocol-version handshake | `internal/capture`, `internal/transport`, `internal/config` |
| `claude+` CLI (modified) | `--gui` flag: discover + exec the desktop binary | — |

## Interaction model

- **Keyboard & focus.** `xterm.js` owns keyboard focus by default; clicking a
  chrome panel performs its action and returns focus to the terminal; a shortcut
  (e.g. Ctrl/Cmd+L) enters panel-nav mode where Tab cycles panels and Esc returns
  to the terminal. `Ctrl-C` copies when text is selected and sends SIGINT when
  not; `Ctrl/Cmd+V` pastes; right-click offers Copy/Paste.
- **Session lifecycle.** A `+` affordance (and a shortcut) creates a session via
  the existing `new` frame; clicking a row focuses that session (the pane clears
  and re-attaches before the first output byte); an explicit kill affordance
  (right-click / trash) terminates it; the empty state reads
  "No sessions — click + to start one."
- **Window model.** One window per repo (mirrors one daemon per repo); the title
  bar shows the repo basename; `claude+ --gui` focuses an already-open window for
  that repo rather than spawning a second; multi-repo means running `--gui` per
  directory.
- **Terminal-pane behavior.** `xterm.js` resize is debounced ~50 ms and sends a
  `resize` frame (cols/rows); layout is CSS flex (fixed-width side panels, the
  terminal fills the rest). WebKitGTK's WebGL→canvas renderer fallback and the
  minimum system-webview versions per OS are tracked as build/test checks.

## Cross-platform & build strategy

- Wails uses each OS's system webview: **WebView2** (Windows), **WKWebView**
  (macOS), **WebKitGTK** (Linux). Small binaries; no bundled Chromium.
- **Single codebase, per-OS build.** One source tree and one Wails config, but
  Wails links native CGO webview libraries and **cannot ride the existing
  single-runner goreleaser cross-compile** that builds the pure-Go CLI. The
  desktop binary needs a **separate per-OS native build matrix**
  (windows/macos/ubuntu runners, each running `wails build` with Node + the
  platform webview SDK), added alongside — not inside — the current goreleaser job
  in `.github/workflows/release.yml`. "Single codebase" still holds; only the
  build runners differ.
- **Linux WebKitGTK is the fussiest** of the three. Add an early Linux smoke
  test (render + attach) so quirks surface up front.

## Error handling

- **Daemon not running / unreachable:** desktop reuses `EnsureDaemon`; on failure
  show an in-window error state with retry.
- **Desktop binary missing:** `claude+ --gui` reports "desktop app not installed"
  with the expected location.
- **HQ unreachable:** Stream/Agents degrade to local-only; Forge/Tickets show a
  "needs Command HQ" state (mirrors the TUI's degraded handling).
- **Client disconnect:** detaching closes the window's attach connection; the
  daemon and sessions keep running (tmux-model contract). Re-attach on relaunch.
- **PTY/session death:** surfaced via `SessInfo.Status`; the live pane shows a
  terminated banner.

## Testing

- **`pty.Mux` per-client state:** new tests for concurrent attach — two clients,
  independent focus and geometry, no cross-clobber.
- **New frames:** golden-fixture parity for `FrameEvent`/`FrameStatus`/
  `FrameAgentDiff` against the existing event envelope tests.
- **Go bridge:** unit-test the adapter against a fake/in-process daemon, reusing
  `daemon_test.go` fixtures; mock the HQ client for Forge/Tickets.
- **Frontend:** component tests per panel; an integration smoke booting a daemon,
  attaching the bridge, and asserting a session renders.
- **Cross-platform CI:** build + smoke (launch, attach, render) on all three OSes.

## Scope / non-goals (YAGNI)

- **In scope (v1):** Wails app in the existing module; concurrent per-client
  attach (per-client focus + PTY sizing); two-binary `--gui`; `xterm.js` live
  pane; **all five tabs as native panels** (agents, forge, session, stream,
  tickets) + the session-list strip + status meters; new frames (`FrameEvent`/`FrameStatus`/
  `FrameAgentDiff` + reconcile) and the event tap; HQ-backed Forge/Tickets via the
  in-module HQ client; protocol-version handshake; per-OS native CI build + smoke.
- **Out of scope (v1):** factoring a shared React component library / reusing the
  Command HQ web UI (native panels chosen to preserve CLI coupling);
  remote/over-SSH desktop use (desktop targets the local daemon); code
  signing/notarization and installer/auto-update polish (v1 ships unsigned
  developer builds).

## Risks

- **Scope.** Full parity roughly doubles the attach-protocol surface and pulls in
  HQ connectivity — the dominant risk. Sequence the new frames behind a working
  Session + Session-list core so there is always a runnable app.
- **Concurrent per-client PTY sizing.** Per-client geometry implies per-client
  output reflow; validate the model early with the `pty.Mux` tests before building
  panels on top.
- **High-rate `output` byte stream** → push via Wails `EventsEmit`/channel, not
  bound-method returns. Validate throughput early.
- **Linux WebKitGTK** rendering/dependency quirks → early smoke test; confirm the
  WebGL→canvas fallback.
- **Code signing/notarization** (macOS especially) → deferred to a pre-distribution
  milestone; v1 ships unsigned developer builds.

## Effort estimate

**Multi-week — materially more than the original 1–3 week sketch.** Full parity
adds, beyond the Wails shell + `xterm.js` pane: per-client focus/PTY-sizing in
`pty.Mux`, the protocol-version handshake, three new frames + the event tap,
agent-diff + reconcile, HQ-backed Forge and Tickets, the separate per-OS native
build matrix, and five designed panels. Each is a multi-day item. The plan should
sequence a runnable Session-core first, then layer the parity surfaces.

## Resolved decisions (2026-06-02 review + brainstorm)

1. **User problem.** The desktop is a *more advanced UX* for the same users and
   the same local work — not a new audience or capability.
2. **UI approach.** **Lean native panels**, not wrapping/reusing the Command HQ
   web UI — preserves the tight CLI/Go coupling (the stated priority), at the cost
   of building the UX natively and maintaining a third UI surface.
3. **Surfaces.** **Full TUI parity in v1** — all five tabs + status meters,
   including HQ-backed Forge/Tickets, accepting multi-week scope and a cloud-login
   requirement.
4. **Concurrent attach.** **Yes** — build per-client focus + per-client PTY
   sizing so terminal and desktop can drive the same daemon simultaneously.
5. **Binary model.** **Two binaries** — `claude+ --gui` discovers and execs
   `claude-plus-desktop`, with a clear missing-binary error.
6. **Mechanics.** Keyboard/focus, session lifecycle, window model, and
   terminal-pane behavior fixed to the defaults in **Interaction model** above;
   refine specifics during planning.

## Open questions for review

None blocking — all review-surfaced decisions are resolved above. Items to settle
during planning:

- HQ access path for Forge/Tickets: confirm the desktop reads HQ credentials from
  shared config and connects directly (assumed), vs. proxying through the daemon.
- Exact shape of `FrameStatus` cadence (push interval vs. on-change).
