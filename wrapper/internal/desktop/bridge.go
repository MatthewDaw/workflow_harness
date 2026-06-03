// Package desktop adapts the daemon attach client to a GUI frontend: it turns
// daemon push-callbacks (PTY output, session-list updates) into emitter events
// the webview consumes, and exposes control methods the webview calls. It is
// frontend-agnostic and Wails-free so it can be unit-tested without a window.
package desktop

import (
	"encoding/base64"

	"github.com/workflow-harness/claude-plus/internal/daemon"
	"github.com/workflow-harness/claude-plus/internal/event"
)

// Event names emitted to the webview. Keep in sync with the frontend listeners.
const (
	EventOutput   = "pty:output"
	EventSessions = "sessions:update"
	EventStream   = "stream:event"
	EventStatus   = "status:update"
)

// OutputEvent carries a chunk of a session's PTY output to the webview. Bytes
// are base64-encoded because Wails serializes event payloads as JSON and PTY
// output is arbitrary binary (escape sequences, UTF-8).
type OutputEvent struct {
	SessID  string `json:"sessId"`
	DataB64 string `json:"dataB64"`
}

// attachClient is the subset of the daemon attach client the bridge needs.
// *daemon.Client is adapted to this in clientAdapter (app.go) so tests can fake
// it without a live daemon.
type attachClient interface {
	SetHandlers(out func(sessID string, b []byte), onSessions func([]daemon.SessInfo))
	SetEventHandler(fn func(env event.Envelope))
	SetStatusHandler(fn func(s daemon.StatusSnapshot))
	Input(b []byte) error
	Resize(cols, rows int) error
	Focus(sessID string) error
	NewSession(ticket string) error
	Detach() error
	Run() error
	InitialSessions() []daemon.SessInfo
}

// Emitter pushes named events to the webview. *wailsEmitter (app.go) wraps the
// Wails runtime; fakeEmitter is used in tests.
type Emitter interface {
	Emit(name string, data ...interface{})
}

// Bridge connects one attach client to one webview emitter.
type Bridge struct {
	c  attachClient
	em Emitter
}

// New builds a bridge over an attach client and an emitter.
func New(c attachClient, em Emitter) *Bridge { return &Bridge{c: c, em: em} }

// Start wires the daemon push-callbacks to emitter events. Call once before the
// read loop runs.
func (b *Bridge) Start() {
	b.c.SetHandlers(
		func(sessID string, data []byte) {
			b.em.Emit(EventOutput, OutputEvent{
				SessID:  sessID,
				DataB64: base64.StdEncoding.EncodeToString(data),
			})
		},
		func(list []daemon.SessInfo) {
			b.em.Emit(EventSessions, list)
		},
	)
	b.c.SetEventHandler(func(env event.Envelope) {
		b.em.Emit(EventStream, env)
	})
	b.c.SetStatusHandler(func(s daemon.StatusSnapshot) {
		b.em.Emit(EventStatus, s)
	})
}

// Run blocks on the attach read loop until the daemon connection closes.
func (b *Bridge) Run() error { return b.c.Run() }

// --- bound methods (called from the webview) ---

// SendInput forwards keystrokes (a UTF-8 string from xterm) to the focused PTY.
func (b *Bridge) SendInput(data string) error { return b.c.Input([]byte(data)) }

// Resize forwards terminal dimensions to the daemon.
func (b *Bridge) Resize(cols, rows int) error { return b.c.Resize(cols, rows) }

// Focus switches the daemon's focused session.
func (b *Bridge) Focus(sessID string) error { return b.c.Focus(sessID) }

// NewSession spawns a session (optionally linked to a ticket; empty in Phase 1).
func (b *Bridge) NewSession(ticket string) error { return b.c.NewSession(ticket) }

// ListSessions returns the current session list (seeded from the attach ack).
func (b *Bridge) ListSessions() []daemon.SessInfo { return b.c.InitialSessions() }
