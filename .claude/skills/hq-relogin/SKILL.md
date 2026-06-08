---
name: hq-relogin
description: >-
  Re-authenticate (relogin) to Command HQ from the claude+ PTY. Runs the
  device-code login (`claude+ login`) to mint a fresh device token when HQ stops
  accepting the old one — sessions stop streaming, "shut down" does nothing, or
  the daemon logs a 401 / "HQ rejected the device token" — then restarts the
  daemon so it reconnects with the new token. Use when the user says
  "/hq-relogin", "relogin to command hq", "re-authenticate with HQ", "my HQ token
  expired", "sessions aren't streaming", or "claude+ login".
---

# /hq-relogin

Re-authenticate this machine to Command HQ. Runs in the developer's claude+
session.

## When to use

Command HQ authenticates the wrapper with a signed **device token** (minted by
`claude+ login`, stored in `~/.claude-plus/credentials`). If that token expires,
or the server's signing secret was rotated, HQ rejects it and the daemon's
WebSocket handshake fails — events buffer locally and never reach HQ. Symptoms:

- New claude+ sessions don't appear / don't stream logs in the HQ Sessions tab.
- "Shut down" / steer from HQ does nothing.
- `~/.claude-plus/crash.log` shows `HQ rejected the device token` or a `401`.

## Steps

1. **Run the device login** in the claude+ terminal:

   ```bash
   claude+ login
   ```

   It prints a short user code (e.g. `WDJB-MJXT`) and waits, e.g.:

   ```
   To finish signing in, open Command HQ and approve this code:

       WDJB-MJXT

   Waiting for approval… (Ctrl-C to cancel)
   ```

2. **Approve the code in Command HQ.** Open HQ → **Get started** (the link-device
   screen) and enter the code, or paste it wherever HQ prompts. On approval the
   wrapper claims a freshly-signed token and rewrites
   `~/.claude-plus/credentials`.
3. **Restart the daemon** so it reads the new token. The daemon reads the token
   once at startup, so a running daemon keeps using the old one. Relaunch
   `claude+` — the version-aware liveness check auto-replaces the stale daemon
   (no manual PID kill needed). On reconnect it replays any locally-buffered
   events, so nothing captured while offline is lost.

## Verify

- `~/.claude-plus/outbound.jsonl.cursor` advances past `0` (delivery resumed), or
- the session shows up live in the HQ Sessions tab with logs streaming.

## Notes

- This only touches HQ device auth — it does NOT affect your Claude Code /
  `claude login` session or your personal `~/.claude`.
- If `claude+ login` reports the code expired before approval, just run it again.
- Pairs with `/hq-sync` / `/hq-update-skills`, which need a valid HQ
  token to reconcile the org catalog.
