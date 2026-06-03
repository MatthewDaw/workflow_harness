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
	clients  map[string]*clientState                  // per-attach-client focus + size
}

// clientState is one attach client's view: which session it drives (focus) and
// the dimensions of its window. A session's PTY is sized to the smallest of the
// clients currently focused on it (tmux semantics), so two clients on different
// sessions each get their full size and two on the same session share the
// smaller one.
type clientState struct {
	focus string // focused session id ("" = none)
	cols  int
	rows  int
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
		clients: map[string]*clientState{},
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

// Spawn creates a new session, optionally seeded with a name and ticket link,
// focuses it, and starts pumping its output to the sinks.
func (m *Mux) Spawn(name, ticket string) (*Session, error) {
	m.mu.Lock()
	id := uuid.NewString()[:8]
	if name == "" {
		name = AutoName(ticket, "")
	}
	if name == "" {
		name = "session"
	}
	name = Disambiguate(name, m.takenNames())
	s, err := newSession(id, name, ticket, m.repoRoot, m.cols, m.rows, m.spawn)
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

// onSessionExit removes a finished session, re-focuses a neighbor (legacy global
// focus), and repoints any per-client focus that pointed at the dead session.
func (m *Mux) onSessionExit(id string) {
	m.mu.Lock()
	idx := -1
	for i, s := range m.sessions {
		if s.ID == id {
			idx = i
			break
		}
	}
	if idx < 0 {
		m.mu.Unlock()
		return
	}
	m.sessions = append(m.sessions[:idx], m.sessions[idx+1:]...)
	if len(m.sessions) == 0 {
		m.focusIdx = -1
	} else if m.focusIdx >= len(m.sessions) {
		m.focusIdx = len(m.sessions) - 1
	}
	// Repoint any client focused on the dead session onto a surviving neighbor.
	newFocus := ""
	if len(m.sessions) > 0 {
		ni := idx
		if ni >= len(m.sessions) {
			ni = len(m.sessions) - 1
		}
		newFocus = m.sessions[ni].ID
	}
	for _, c := range m.clients {
		if c.focus == id {
			c.focus = newFocus
		}
	}
	m.mu.Unlock()
	if newFocus != "" {
		m.recompute(newFocus)
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
			ID: s.ID, Name: s.Name, Ticket: s.Ticket,
			Status: string(s.Status()), Focused: i == m.focusIdx,
		}
	}
	return out
}

// SessionView is a read-only snapshot of a session for the TUI/attach client.
type SessionView struct {
	ID      string
	Name    string
	Ticket  string
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

// --- per-client focus + sizing (concurrent attach) ---
//
// Each attach client has its own focused session and window size. A session's
// PTY is sized to the smallest of the clients currently focused on it, so two
// clients viewing different sessions each get their full size, and two viewing
// the same session share the smaller one (tmux semantics). Input from a client
// routes to that client's focused session, never a global one.

// getLocked finds a session by id; caller holds m.mu.
func (m *Mux) getLocked(id string) *Session {
	for _, s := range m.sessions {
		if s.ID == id {
			return s
		}
	}
	return nil
}

// minSizeLocked returns the smallest cols/rows across clients focused on sessID.
// ok is false when no client currently views the session (leave its size as-is).
// Caller holds m.mu.
func (m *Mux) minSizeLocked(sessID string) (cols, rows int, ok bool) {
	cols, rows = 1<<30, 1<<30
	for _, c := range m.clients {
		if c.focus == sessID {
			if c.cols > 0 && c.cols < cols {
				cols = c.cols
			}
			if c.rows > 0 && c.rows < rows {
				rows = c.rows
			}
			ok = true
		}
	}
	return cols, rows, ok
}

// recompute re-sizes each named session to the smallest of its current viewers,
// applying the PTY resize outside the lock. Sessions with no viewer are left
// untouched.
func (m *Mux) recompute(sessIDs ...string) {
	type rz struct {
		s    *Session
		c, r int
	}
	var todo []rz
	m.mu.Lock()
	seen := map[string]bool{}
	for _, sid := range sessIDs {
		if sid == "" || seen[sid] {
			continue
		}
		seen[sid] = true
		s := m.getLocked(sid)
		if s == nil {
			continue
		}
		c, r, ok := m.minSizeLocked(sid)
		if !ok {
			continue
		}
		todo = append(todo, rz{s, c, r})
	}
	m.mu.Unlock()
	for _, t := range todo {
		_ = t.s.Resize(t.c, t.r)
	}
}

// RegisterClient adds an attach client with its initial window size, defaulting
// its focus to the first session (if any) and sizing that session to include it.
func (m *Mux) RegisterClient(id string, cols, rows int) {
	if cols <= 0 {
		cols = m.cols
	}
	if rows <= 0 {
		rows = m.rows
	}
	m.mu.Lock()
	focus := ""
	if len(m.sessions) > 0 {
		focus = m.sessions[0].ID
	}
	m.clients[id] = &clientState{focus: focus, cols: cols, rows: rows}
	m.mu.Unlock()
	m.recompute(focus)
}

// UnregisterClient drops a client and re-sizes its formerly-focused session to
// the remaining viewers (it may grow back if a smaller client left).
func (m *Mux) UnregisterClient(id string) {
	m.mu.Lock()
	old := ""
	if c := m.clients[id]; c != nil {
		old = c.focus
		delete(m.clients, id)
	}
	m.mu.Unlock()
	m.recompute(old)
}

// SetClientFocus points a client at a session and re-sizes both the old and new
// sessions (the old may grow now that this client left it; the new may shrink).
func (m *Mux) SetClientFocus(id, sessID string) error {
	m.mu.Lock()
	c := m.clients[id]
	if c == nil {
		m.mu.Unlock()
		return fmt.Errorf("no client %q", id)
	}
	if m.getLocked(sessID) == nil {
		m.mu.Unlock()
		return fmt.Errorf("no session %q", sessID)
	}
	old := c.focus
	c.focus = sessID
	m.mu.Unlock()
	m.recompute(old, sessID)
	return nil
}

// SetClientSize updates a client's window dimensions and re-sizes its focused
// session accordingly.
func (m *Mux) SetClientSize(id string, cols, rows int) {
	m.mu.Lock()
	c := m.clients[id]
	if c == nil {
		m.mu.Unlock()
		return
	}
	c.cols, c.rows = cols, rows
	focus := c.focus
	m.mu.Unlock()
	m.recompute(focus)
}

// WriteForClient routes input bytes to the client's focused session's PTY stdin.
func (m *Mux) WriteForClient(id string, p []byte) (int, error) {
	m.mu.RLock()
	var s *Session
	if c := m.clients[id]; c != nil {
		s = m.getLocked(c.focus)
	}
	m.mu.RUnlock()
	if s == nil {
		return 0, fmt.Errorf("no focused session for client %q", id)
	}
	return s.Write(p)
}

// ListFor returns the session list with the Focused flag set per this client's
// own focus (each client sees its own highlighted session).
func (m *Mux) ListFor(id string) []SessionView {
	m.mu.RLock()
	defer m.mu.RUnlock()
	focus := ""
	if c := m.clients[id]; c != nil {
		focus = c.focus
	}
	out := make([]SessionView, len(m.sessions))
	for i, s := range m.sessions {
		out[i] = SessionView{
			ID: s.ID, Name: s.Name, Ticket: s.Ticket,
			Status: string(s.Status()), Focused: s.ID == focus,
		}
	}
	return out
}
