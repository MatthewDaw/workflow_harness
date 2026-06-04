---
name: compound-engineering
description: >-
  Install the Compound Engineering plugin (EveryInc/compound-engineering-plugin)
  on this machine — adds the plugin marketplace and installs the
  compound-engineering bundle, which ships the `ce-*` skills (ce-brainstorm,
  ce-plan, ce-work, ce-code-review, ce-debug, ce-commit-push-pr, lfg, and more).
  Use when the user says "/compound-engineering", "install compound engineering",
  "set up compound engineering", or "add the ce skills".
---

# /compound-engineering

Bootstrap the [Compound Engineering](https://github.com/EveryInc/compound-engineering-plugin)
plugin on the current machine. It ships a family of `ce-*` skills (brainstorm,
plan, work, code-review, debug, commit-push-pr, simplify-code, sessions, the full
`lfg` autonomous pipeline, etc.); this skill installs that family.

## What it does

1. **Add the marketplace and install the plugin.** Run inside Claude Code:

   ```
   /plugin marketplace add EveryInc/compound-engineering-plugin
   /plugin install compound-engineering
   ```

   `marketplace add` registers the EveryInc plugin source; `install` pulls the
   `compound-engineering` bundle and its `ce-*` skills/agents into Claude Code.
   If the marketplace is already added, skip straight to `/plugin install`.

2. **Confirm it's available.** After install, the `compound-engineering:ce-*`
   skills appear in the skill list (e.g. `ce-brainstorm`, `ce-plan`,
   `ce-code-review`, `lfg`). Reload/restart the session if they don't show up yet.

## Notes

- This installs via Claude Code's **plugin** system (not a git clone), so updates
  come through `/plugin` rather than a setup script.
- The plugin installs into the developer's Claude Code config — it is a
  per-machine tool, not committed to the repo.
