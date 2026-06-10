# claude-plus-desktop

The claude+ desktop GUI (Wails + React/TS): a windowed front end over the same
per-repo claude+ daemon the CLI attaches to, with terminal, session tabs, an
event stream, and a status bar. Launched via `claude+ --gui` or by running the
binary in a repo. The Go/JS boundary lives in `app.go` (bound methods) and
`internal/desktop` (daemon-to-webview bridge); `frontend/wailsjs/` is generated.
Develop with `wails dev`, build with `wails build`, from this directory.
