package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/wailsapp/wails/v2/pkg/runtime"
	"github.com/workflow-harness/claude-plus/internal/daemon"
	"github.com/workflow-harness/claude-plus/internal/desktop"
	"github.com/workflow-harness/claude-plus/internal/event"
)

// App is the Wails-bound type. Its exported methods become callable from JS and
// their signatures generate TypeScript bindings.
type App struct {
	ctx    context.Context
	bridge *desktop.Bridge
	err    string // startup error surfaced to the UI, if any
}

// NewApp creates the App.
func NewApp() *App { return &App{} }

// startup runs after the window exists. It resolves the repo, ensures the
// daemon, dials it, wires the bridge to the Wails runtime emitter, and starts
// the attach read loop in the background.
func (a *App) startup(ctx context.Context) {
	a.ctx = ctx

	repo, err := resolveRepoRoot()
	if err != nil {
		a.err = fmt.Sprintf("resolve repo: %v", err)
		return
	}
	if _, err := daemon.EnsureDaemon(repo); err != nil {
		a.err = fmt.Sprintf("start daemon: %v", err)
		return
	}
	c, err := daemon.Dial(repo)
	if err != nil {
		a.err = fmt.Sprintf("attach: %v", err)
		return
	}

	a.bridge = desktop.New(&clientAdapter{c: c}, &wailsEmitter{ctx: ctx})
	a.bridge.Start()
	go func() { _ = a.bridge.Run() }() // returns on daemon disconnect
}

// StartupError lets the UI render a connect-failure state.
func (a *App) StartupError() string { return a.err }

// SendInput forwards keystrokes to the daemon's focused PTY.
func (a *App) SendInput(data string) error {
	if a.bridge == nil {
		return fmt.Errorf("not connected")
	}
	return a.bridge.SendInput(data)
}

// Resize forwards terminal dimensions to the daemon.
func (a *App) Resize(cols, rows int) error {
	if a.bridge == nil {
		return nil
	}
	return a.bridge.Resize(cols, rows)
}

// Focus switches the daemon's focused session.
func (a *App) Focus(sessID string) error {
	if a.bridge == nil {
		return fmt.Errorf("not connected")
	}
	return a.bridge.Focus(sessID)
}

// NewSession spawns a session.
func (a *App) NewSession() error {
	if a.bridge == nil {
		return fmt.Errorf("not connected")
	}
	return a.bridge.NewSession()
}

// ListSessions returns the current session list.
func (a *App) ListSessions() []daemon.SessInfo {
	if a.bridge == nil {
		return nil
	}
	return a.bridge.ListSessions()
}

// wailsEmitter adapts the Wails runtime to desktop.Emitter.
type wailsEmitter struct{ ctx context.Context }

func (w *wailsEmitter) Emit(name string, data ...interface{}) {
	runtime.EventsEmit(w.ctx, name, data...)
}

// clientAdapter adapts *daemon.Client (exported-field callbacks) to the
// desktop.attachClient interface (SetHandlers + InitialSessions).
type clientAdapter struct{ c *daemon.Client }

func (a *clientAdapter) SetHandlers(out func(string, []byte), onSess func([]daemon.SessInfo)) {
	a.c.Out = out
	a.c.OnSessions = onSess
}
func (a *clientAdapter) SetEventHandler(fn func(event.Envelope))          { a.c.OnEvent = fn }
func (a *clientAdapter) SetStatusHandler(fn func(daemon.StatusSnapshot)) { a.c.OnStatus = fn }
func (a *clientAdapter) Input(b []byte) error               { return a.c.Input(b) }
func (a *clientAdapter) Resize(cols, rows int) error        { return a.c.Resize(cols, rows) }
func (a *clientAdapter) Focus(sessID string) error          { return a.c.Focus(sessID) }
func (a *clientAdapter) NewSession() error                  { return a.c.NewSession() }
func (a *clientAdapter) Detach() error                      { return a.c.Detach() }
func (a *clientAdapter) Run() error                         { return a.c.Run() }
func (a *clientAdapter) InitialSessions() []daemon.SessInfo { return a.c.Sessions }

// resolveRepoRoot walks up from cwd to the nearest .git dir; falls back to cwd.
// (Mirrors cmd/claude-plus/main.go so the desktop targets the same daemon.)
func resolveRepoRoot() (string, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	dir := cwd
	for {
		if fi, err := os.Stat(filepath.Join(dir, ".git")); err == nil && fi.IsDir() {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return cwd, nil
		}
		dir = parent
	}
}
