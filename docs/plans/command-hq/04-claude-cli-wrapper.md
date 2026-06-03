---
status: active
type: feature
created: 2026-06-02
completion: 82
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
   (`~/.claude/projects/<hash>/<sid>.jsonl`) for structured message/tool events
   and cost/token deltas.
3. **Hooks** (`settings.json` PreToolUse/PostToolUse/Stop/Notification) post
   low-latency lifecycle + `status.change` signals to the daemon's local socket.

*Code today:* `wrapper/internal/capture/` (`hooks.go`, `jsonl.go`, `parse.go`).

## Transport & auth

- Outbound-only **WebSocket** to the cloud, with **offline buffering** that
  replays after reconnect. Control frames come back down the same socket
  ([feature 3 steering](./03-claude-code-integration.md)).
- **Device-token auth:** `claude+ login` runs a device-code flow against HQ; the
  token authorizes the daemon's WS `$connect` and scopes its data to that user.
  No inbound ports needed — the daemon dials out. The token carries a TTL, a
  revoke control in HQ, and lives in the OS keychain (not a plaintext dotfile).
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

## Status

- **Built:** daemon + attach client, capture (hooks/jsonl), desktop bridge,
  `--gui` discovery, npm/install scaffolding.
- **Open:** full transport hardening (offline buffer edge cases), status-line and
  multiplexed-tab polish per the wireframe TUI screens.
