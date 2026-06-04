---
status: active
type: feature
created: 2026-06-02
completion: 90
feature: claude-cli-wrapper
---

# Feature 4 — Claude CLI Wrapper (`claude+`)

How we wrap the real `claude` CLI in `claude+` so that every session is captured
and streamable without changing the native experience.

## Daemon + thin attach client (tmux model)

`claude+` starts (or attaches to) a **background daemon bound to the repo**; the
terminal UI is a thin client attached over a local socket. Sessions survive the terminal
closing and are re-attachable from another terminal on the same machine.
(v1 is single-laptop; no remote/SSH.)

- `claude+` in a repo → attach-or-create for the current directory.
- `claude+ ls` → enumerate daemons/instances.
- `claude+ --session=N` → attach by index.
- `claude+ --gui` → launch the desktop app (a Wails build already exists under
  `wrapper/cmd/claude-plus-desktop/`).

*Code today:* `wrapper/cmd/claude-plus/` (`main.go`, `attach.go`, `gui.go`),
`wrapper/internal/daemon/` (attach protocol, client).

## Capture without screen-scraping

Three sources, never PTY text parsing:

1. **PTY pass-through** renders the native interactive `claude` unchanged.
2. The daemon **tails Claude Code's transcript JSONL**
   (`<config>/projects/<hash>/<sid>.jsonl`) for structured message/tool events and
   cost/token deltas. The transcript lives under whatever config root the inner
   Claude runs against — which is the isolated `~/.claude+` (below), so the tailer
   resolves its base from `config.ConfigDir()` and falls back to `~/.claude` only
   when isolation is inactive. The per-project `<hash>` is Claude Code's slug:
   **every** non-alphanumeric character (including `_`) becomes `-`, runs are not
   collapsed (`C:\Users\me\workflow_harness` → `C--Users-me-workflow-harness`);
   replacing only path separators missed the underscore and captured nothing.
   *Code:* `capture/parse.go` (`TranscriptPath`/`projectHash`/`slugifyPath`).
3. **Hooks** (`settings.json` PreToolUse/PostToolUse/Stop/Notification) post
   low-latency lifecycle + `status.change` signals to the daemon's local socket.

*Code today:* `wrapper/internal/capture/` (`hooks.go`, `jsonl.go`, `parse.go`).

## Isolated `~/.claude+` config root (no `~/.claude` pollution)

claude+ launches its inner `claude` with `CLAUDE_CONFIG_DIR` pointed at an
**isolated config root, `~/.claude+`**, so the product-bundled skills/agents and
the session history claude+ generates never land in the user's personal
`~/.claude`. Because the env var is set only on the child claude+ spawns, a normal
`claude` run (claude+ not running, or a separate session) still reads `~/.claude`
and never sees this root.

**As-built shape (divergence from the migration plan).** The plan
([U21](../2026-06-03-001-feat-command-hq-new-model-migration-plan.md)) described a
*per-session* `~/.claude+/run/<id>` dir built with symlinks and torn down on close.
The code instead uses a **single stable `~/.claude+`** root that is **seeded once**
from `~/.claude` (`.credentials.json`, `.claude.json`, `settings.json`, `.mcp.json`
— copied only when absent) and **never torn down**, so auth, onboarding, settings,
MCP, and transcripts persist across claude+ restarts; claude+ then owns its own
copies. `skills/`/`agents/` under it receive pulled (synced) items. This is simpler
and more durable than the per-session symlink design, at the cost of write-through
credential sharing (the stable root keeps its own seeded credentials rather than
symlinking the live `~/.claude` login). *Code:* `internal/config/overlay.go`
(`EnsureConfigDir`/`ConfigDir`), `internal/pty/session.go` (`DefaultSpawn` sets
`Isolate`; `newSession` sets `CLAUDE_CONFIG_DIR`).

## Transport & auth

- Outbound-only **WebSocket** to the cloud, with **offline buffering** that
  replays after reconnect. Control frames come back down the same socket
  ([feature 3 steering](./03-claude-code-integration.md)).
- **Device-token auth:** `claude+ login` runs a device-code flow against HQ; the
  token authorizes the daemon's WS `$connect` and scopes its data to that user.
  No inbound ports needed — the daemon dials out. The token carries a TTL, a
  revoke control in HQ, and lives in the OS keychain (not a plaintext dotfile).
  Device tokens are signed/verified with a backend `DEVICE_TOKEN_SECRET` (sourced
  from secrets at deploy, not a baked placeholder); **rotating that secret
  invalidates all outstanding tokens**, so devices must re-run `claude+ login`.
  *Code today:* `wrapper/internal/config/`, HQ side `backend/src/auth/device.ts`;
  connection setup captured in the project memory note `hq-connection-setup`.

## Distribution

Go cross-compiled with **goreleaser**; an **npm wrapper package**
(`npm/claude-plus/`) ships per-platform prebuilt binaries (`npm i -g claude-plus`,
no Go toolchain), plus `scripts/install.sh` for `curl | sh`. The release publishes
`checksums.txt` and `install.sh` verifies a SHA-256 before executing the binary —
v1 ships unsigned, so the checksum is the integrity gate.

## Desktop app (Wails) — same core, graphical shell

A native desktop GUI for the *same users and the same work* as the terminal — a
more polished UX, not a new capability. Durable decisions (migrated from the
desktop design spec):

- **Wails, one Go module, one shared daemon.** The desktop `main`
  (`wrapper/cmd/claude-plus-desktop`) lives **inside the existing `wrapper/`
  module**, linking the same `internal/daemon`/`pty`/`transport`. A protocol change
  breaks both binaries at compile time — coupling over convention. A Go-hosted
  system webview (WebView2 / WKWebView / WebKitGTK) keeps one cross-platform
  codebase with small binaries.
- **Second attach client.** The desktop is just another client of the attach
  protocol (the role the TUI plays). The live `claude` surface renders in an
  `xterm.js` pane; the chrome around it is native panels.
- **Concurrent per-client attach.** Terminal and desktop can drive the same daemon
  at once. `pty.Mux` **already implements** per-client focus + per-client PTY
  sizing — this is built, not new work.
- **Two binaries.** `claude+ --gui` discovers and execs `claude-plus-desktop`
  (focuses an open window for the repo rather than spawning a second); clear error
  when it isn't installed.
- **v1 = full TUI parity** — all four tabs (Session, Agents, Forge, Stream) as
  native panels + status meters. `FrameEvent`, `FrameStatus`, and the
  protocol-version handshake **already exist**; the only genuinely new daemon work
  is `FrameAgentDiff` + reconcile and a multi-consumer event tap. Plus HQ login
  (Forge calls the in-module HQ client directly).
- **Interaction states to define (design phase):** HQ-login surface inside the app,
  pre-attach empty state, protocol-mismatch error.
- **Build.** A **separate per-OS native build matrix** (`wails build` on
  windows/macos/ubuntu) alongside — not inside — the goreleaser job; CGO webview
  libs can't ride the pure-Go cross-compile. Linux WebKitGTK is the fussiest; smoke
  it early. v1 ships unsigned developer builds (signing/notarization deferred).

## Terminal & session UX

These behaviors govern how the live `claude` surface and the session sub-tabs
feel in the desktop client. They are client-side (`xterm.js` + the desktop
bridge) except where noted.

- **Visible input cursor.** The `xterm.js` pane uses a **blinking block cursor**
  and grabs focus on mount and on click. An unfocused xterm renders a hollow,
  easy-to-miss caret, so we focus it explicitly — the caret is always visible
  while typing. *Code:* `cmd/claude-plus-desktop/frontend/src/Terminal.tsx`.
- **Scrollback.** The terminal keeps a generous scrollback buffer (10k lines) so
  you can mouse-wheel up through a session's earlier output; typing or new output
  snaps back to the live edge. *Code:* `Terminal.tsx` (`scrollback` xterm option).
- **Auto-generated session titles (LLM).** A session starts with a provisional
  keyword slug from its first user turn, then **upgrades to a concise
  LLM-generated title** once the first full exchange (first user message + first
  assistant reply) lands. The daemon runs a one-shot, time-boxed headless
  `claude -p` on the user's **own subscription** to summarize the opening
  exchange into a 2–4 word kebab title, renames the session, and emits a
  `session.rename` event. Best-effort: if the call fails or times out, the
  provisional slug stays. The title sub-process is tagged with
  `CLAUDE_PLUS_TITLE=1` so the hook shim skips it (no phantom session reaches
  HQ). A manual rename locks the name against further auto-titling. *Code:*
  `internal/title/`, `internal/capture/jsonl.go` (first-exchange hook),
  `internal/daemon/runtime.go`, `internal/pty/{session,mux}.go`.
- **Rename a session by double-clicking its tab.** Double-click a session
  sub-tab's name to edit it inline; Enter commits, Escape cancels. This is the
  GUI surface of the existing ⌃R manual rename, and a manual name is sticky —
  auto-titling will not overwrite it. *Code:* `Sessions.tsx` →
  `App.Rename` → bridge → `FrameRename` → `Mux.Rename`.
- **Session tabs hidden on Agents / Stream.** The desktop chrome has top-level
  views — **Session · Agents · Stream**. The session sub-tab row belongs to the
  Session view only; selecting Agents or Stream hides it, since those views are
  instance-scoped rather than per-session. *Code:* `App.tsx` view switcher.
- **`--dangerously-skip-permissions` propagates to every session.** Launching
  `claude+ --dangerously-skip-permissions` (or with `--gui`) starts the repo's
  daemon in **dangerous mode**: every inner `claude` child it spawns is launched
  with `--dangerously-skip-permissions`, so sub-agents inherit the wrapper's
  permission posture. The mode is carried to the detached daemon via the
  `CLAUDE_PLUS_DANGEROUS` environment variable and honored in
  `pty.DefaultSpawn`. Because the daemon is per-repo and shared, the mode is
  fixed when the daemon first starts for a repo — if a daemon is already running,
  restart it to change the mode. *Code:* `cmd/claude-plus/main.go`,
  `cmd/claude-plus-desktop/app.go`, `internal/pty/session.go` (`DefaultSpawn`).

Adding the inline-rename control channel (`FrameRename`) bumped the attach
**`ProtocolVersion`**, so a stale pre-rename daemon is auto-replaced on the next
attach rather than silently lacking the new frame.

## Status

- **Built:** daemon + attach client, capture (hooks/jsonl, with the hook receiver
  forwarding lifecycle events), desktop bridge, `--gui` discovery, npm/install
  scaffolding, terminal/session UX (visible cursor, scrollback, LLM auto-titles,
  double-click rename, view switcher, dangerous-mode propagation), the **isolated
  `~/.claude+` config root**, and config-sync (remote half + drift over the
  `~/.claude` ∪ `~/.claude+` union).
- **In progress (not on this branch):** a `UserPromptSubmit` hook that auto-renames
  a session on the first prompt and pushes the rename to the attached CLI tab; a
  further daemon **protocol-version bump** so rebuilds auto-replace a stale daemon;
  a heartbeat + ~60s freshness window so power-loss/killed daemons drop off HQ's
  live list. See the overview's "In progress / next".
- **Open:** full transport hardening (offline buffer edge cases), status-line and
  multiplexed-tab polish per the wireframe TUI screens.
