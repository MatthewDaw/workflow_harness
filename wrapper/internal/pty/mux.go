package pty

import (
	"fmt"
	"sync"

	"github.com/google/uuid"
	"github.com/workflow-harness/claude-plus/internal/diag"
)

// Mux multiplexes multiple claude PTY sessions inside one daemon. It tracks the
// focused session, routes input to it, and fans each session's output to
// registered sinks (the attached client + the capture layer). Background
// sessions keep running and producing output even when not focused.
type Mux struct {
	repoRoot string
	cols     int
	rows     int
	spawn    SpawnFunc

	mu       sync.RWMutex
	sessions []*Session // insertion order == sub-tab order
	focusIdx int
	sinks    map[string]func(sessID string, b []byte) // output fan-out by sink id
}

// NewMux creates a mux for a repo at an initial terminal size.
func NewMux(repoRoot string, cols, rows int, spawn SpawnFunc) *Mux {
	if spawn == nil {
		spawn = DefaultSpawn
	}
	if cols == 0 {
		cols = 80
	}
	if rows == 0 {
		rows = 24
	}
	return &Mux{
		repoRoot: repoRoot, cols: cols, rows: rows, spawn: spawn,
		focusIdx: -1, sinks: map[string]func(string, []byte){},
	}
}

// takenNames returns the set of current session names for disambiguation.
func (m *Mux) takenNames() map[string]bool {
	taken := make(map[string]bool, len(m.sessions))
	for _, s := range m.sessions {
		taken[s.Name] = true
	}
	return taken
}

// Spawn creates a new session, optionally seeded with a name, focuses it, and
// starts pumping its output to the sinks. Sessions without a seeded name are
// auto-named from their first user turn (see ApplyAutoName).
func (m *Mux) Spawn(name string) (*Session, error) {
	m.mu.Lock()
	id := uuid.NewString()[:8]
	if name == "" {
		name = "session"
	}
	name = Disambiguate(name, m.takenNames())
	s, err := newSession(id, name, m.repoRoot, m.cols, m.rows, m.spawn)
	if err != nil {
		m.mu.Unlock()
		return nil, err
	}
	m.sessions = append(m.sessions, s)
	m.focusIdx = len(m.sessions) - 1
	m.mu.Unlock()

	go m.pump(s)
	return s, nil
}

// pump continuously reads a session's PTY and fans output to all sinks. On EOF
// (child exited) it marks the session done and re-focuses a neighbor.
func (m *Mux) pump(s *Session) {
	defer diag.Recover("mux.pump")
	buf := make([]byte, 32*1024)
	for {
		n, err := s.Read(buf)
		if n > 0 {
			b := make([]byte, n)
			copy(b, buf[:n])
			s.recordHist(b)
			m.mu.RLock()
			for _, sink := range m.sinks {
				sink(s.ID, b)
			}
			m.mu.RUnlock()
		}
		if err != nil {
			break
		}
	}
	m.onSessionExit(s.ID)
}

// onSessionExit removes a finished session and re-focuses a neighbor if it was
// the focused one.
func (m *Mux) onSessionExit(id string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	idx := -1
	for i, s := range m.sessions {
		if s.ID == id {
			idx = i
			break
		}
	}
	if idx < 0 {
		return
	}
	m.sessions = append(m.sessions[:idx], m.sessions[idx+1:]...)
	if len(m.sessions) == 0 {
		m.focusIdx = -1
		return
	}
	// Re-focus a neighbor: prefer the previous index, clamp into range.
	if m.focusIdx >= len(m.sessions) {
		m.focusIdx = len(m.sessions) - 1
	}
}

// AddSink registers an output consumer keyed by id (e.g. an attach client) and
// replays each session's recent output to it, so a freshly-attached client
// renders the current screen immediately instead of staying blank until the
// next repaint.
func (m *Mux) AddSink(id string, fn func(sessID string, b []byte)) {
	m.mu.Lock()
	m.sinks[id] = fn
	sessions := append([]*Session(nil), m.sessions...)
	m.mu.Unlock()
	for _, s := range sessions {
		if h := s.History(); len(h) > 0 {
			fn(s.ID, h)
		}
	}
}

// RemoveSink deregisters an output consumer.
func (m *Mux) RemoveSink(id string) {
	m.mu.Lock()
	delete(m.sinks, id)
	m.mu.Unlock()
}

// Focused returns the currently focused session, or nil if none.
func (m *Mux) Focused() *Session {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.focusIdx < 0 || m.focusIdx >= len(m.sessions) {
		return nil
	}
	return m.sessions[m.focusIdx]
}

// Get returns a session by id.
func (m *Mux) Get(id string) *Session {
	m.mu.RLock()
	defer m.mu.RUnlock()
	for _, s := range m.sessions {
		if s.ID == id {
			return s
		}
	}
	return nil
}

// Focus switches the focused session by id. Input then routes only to it.
func (m *Mux) Focus(id string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i, s := range m.sessions {
		if s.ID == id {
			m.focusIdx = i
			return nil
		}
	}
	return fmt.Errorf("no session %q", id)
}

// FocusNext / FocusPrev cycle focus through the sub-tab row.
func (m *Mux) FocusNext() {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.sessions) == 0 {
		return
	}
	m.focusIdx = (m.focusIdx + 1) % len(m.sessions)
}

func (m *Mux) FocusPrev() {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.sessions) == 0 {
		return
	}
	m.focusIdx = (m.focusIdx - 1 + len(m.sessions)) % len(m.sessions)
}

// WriteFocused routes input bytes to the focused session's PTY stdin.
func (m *Mux) WriteFocused(p []byte) (int, error) {
	s := m.Focused()
	if s == nil {
		return 0, fmt.Errorf("no focused session")
	}
	return s.Write(p)
}

// WriteTo routes input bytes to a specific session (used by control inject so a
// background session can be steered without stealing focus).
func (m *Mux) WriteTo(id string, p []byte) (int, error) {
	s := m.Get(id)
	if s == nil {
		return 0, fmt.Errorf("no session %q", id)
	}
	return s.Write(p)
}

// Resize applies new dimensions to all sessions (SIGWINCH propagation). The
// focused session is what the client sees, but background PTYs are kept in sync
// so switching focus shows correctly-sized output.
func (m *Mux) Resize(cols, rows int) {
	m.mu.Lock()
	m.cols, m.rows = cols, rows
	sessions := append([]*Session(nil), m.sessions...)
	m.mu.Unlock()
	for _, s := range sessions {
		_ = s.Resize(cols, rows)
	}
}

// Count returns the number of live sessions.
func (m *Mux) Count() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return len(m.sessions)
}

// List returns a snapshot of session metadata in sub-tab order.
func (m *Mux) List() []SessionView {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make([]SessionView, len(m.sessions))
	for i, s := range m.sessions {
		out[i] = SessionView{
			ID: s.ID, Name: s.Name,
			Status: string(s.Status()), Focused: i == m.focusIdx,
		}
	}
	return out
}

// SessionView is a read-only snapshot of a session for the TUI/attach client.
type SessionView struct {
	ID      string
	Name    string
	Status  string
	Focused bool
}

// CloseAll terminates every session (daemon shutdown).
func (m *Mux) CloseAll() {
	m.mu.Lock()
	sessions := append([]*Session(nil), m.sessions...)
	m.sessions = nil
	m.focusIdx = -1
	m.mu.Unlock()
	for _, s := range sessions {
		_ = s.Close()
	}
}

// ApplyAutoName feeds a session's first user turn (observed by the capture
// layer) into auto-naming, returning the new name if it changed.
func (m *Mux) ApplyAutoName(sessID, firstTurn string) (bool, string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var target *Session
	for _, s := range m.sessions {
		if s.ID == sessID {
			target = s
			break
		}
	}
	if target == nil {
		return false, ""
	}
	taken := make(map[string]bool)
	for _, s := range m.sessions {
		if s.ID != sessID {
			taken[s.Name] = true
		}
	}
	return target.MaybeName(firstTurn, taken)
}
