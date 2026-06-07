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
	sinks    map[string]*sink        // output fan-out by sink id
	clients  map[string]*clientState // per-attach-client focus + size
}

// sink is one output consumer (an attach client) with its own buffered delivery
// channel and pump goroutine. Fan-out under the mux lock only does a non-blocking
// enqueue; the actual (potentially slow) socket write happens off-lock in the
// per-sink pump. When the buffer is full the oldest frame is dropped so a stuck
// client can never freeze the mux or stall the other clients (HOL fix #10). A
// dropped frame is harmless: claude is a full-screen TUI that repaints, so the
// next frame reconstructs the screen.
type sink struct {
	id   string
	fn   func(sessID string, b []byte)
	ch   chan sinkFrame
	done chan struct{}
}

// sinkFrame is one queued output chunk tagged with its originating session.
type sinkFrame struct {
	sessID string
	b      []byte
}

// sinkBuf caps a sink's pending-frame queue. Sized to absorb a brief stall
// (a TUI repaint is tens of frames) while bounding memory for a wedged client.
const sinkBuf = 256

// deliver enqueues a frame to the sink without blocking. On a full buffer it
// drops the oldest queued frame to make room, so the newest output always wins
// and the producer (mux.pump) never blocks on a slow consumer.
func (s *sink) deliver(f sinkFrame) {
	for {
		select {
		case s.ch <- f:
			return
		default:
			// Full: drop oldest, then retry. The drain is also non-blocking so a
			// concurrent pump draining the channel can't deadlock us.
			select {
			case <-s.ch:
			default:
			}
		}
	}
}

// run drains the sink's buffer and performs the real (blocking) write per frame.
// It exits when the channel is closed (RemoveSink).
func (s *sink) run() {
	for f := range s.ch {
		s.fn(f.sessID, f.b)
	}
	close(s.done)
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
		focusIdx: -1, sinks: map[string]*sink{},
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

// Spawn creates a new session, optionally seeded with a name, focuses it, and
// starts pumping its output to the sinks.
func (m *Mux) Spawn(name string) (*Session, error) {
	m.mu.Lock()
	// Full UUID, not a truncated prefix: this id is passed to `claude
	// --session-id` (see DefaultSpawn), so it becomes Claude Code's session id and
	// thus its transcript filename (<id>.jsonl). The capture tailer keys on the
	// same id, so the two must match exactly — a truncation here is why the tailer
	// historically read a nonexistent file and emitted no transcript events.
	id := uuid.NewString()
	if name == "" {
		name = AutoName("")
	}
	if name == "" {
		name = "session"
	}
	name = Disambiguate(name, m.takenNames())
	// Pass onSessionExit as the watcher's onExit so the child's real exit (Wait
	// returning) drives the same removal bookkeeping as the pump's EOF path. Both
	// converge through removeLocked, which is idempotent, so a double-trigger
	// (watcher + pump) for the same id is a harmless no-op. onSessionExit takes
	// m.mu, but it runs on the watcher goroutine and Close never holds m.mu, so
	// there is no lock-ordering deadlock.
	s, err := newSession(id, name, m.repoRoot, m.cols, m.rows, m.spawn, false, m.onSessionExit)
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

// SpawnResumed recreates a persisted session under the SAME id by launching
// `claude --resume <id>` (resume mode of the injectable spawn seam), so the
// child reattaches to the existing Claude conversation after a daemon restart
// and HQ keeps streaming it as the same logical session. Unlike Spawn it does
// NOT mint a new id and does NOT disambiguate the name — the id and name are
// authoritative restored values from the store. It wires onExit exactly like
// Spawn (so a resume-failure exit drives the same removal/done bookkeeping the
// integrator's onExit watcher relies on), appends in insertion order to
// preserve sub-tab order, focuses the session, and starts the shared pump. The
// caller is expected to follow up with SeedNaming (to restore the naming
// latches) and to seed the session's tailer offset + Seq from the store.
func (m *Mux) SpawnResumed(id, name string) (*Session, error) {
	if name == "" {
		name = "session"
	}
	m.mu.Lock()
	// Reuse newSession (single Wait-owner watcher + onExit wiring) in resume mode
	// so we never duplicate the lifecycle/pump logic; only the launch args differ.
	s, err := newSession(id, name, m.repoRoot, m.cols, m.rows, m.spawn, true, m.onSessionExit)
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
			// Fan out under the lock with only a non-blocking enqueue per sink; the
			// slow socket write happens off-lock in each sink's pump. A stuck client
			// drops frames instead of blocking the mux and the other clients (#10).
			m.mu.RLock()
			for _, sk := range m.sinks {
				sk.deliver(sinkFrame{sessID: s.ID, b: b})
			}
			m.mu.RUnlock()
		}
		if err != nil {
			break
		}
	}
	m.onSessionExit(s.ID)
}

// removeLocked removes the session with the given id from the sub-tab list,
// re-focuses a neighbor (legacy global focus), and repoints any per-client focus
// that pointed at the removed session. It is the single source of truth for the
// removal/re-focus bookkeeping shared by onSessionExit (EOF-driven) and Kill
// (client-driven), so the two paths cannot diverge. It returns the removed
// session (nil if the id was not present — an idempotent no-op) and the id of
// the session focus moved to ("" when the list is now empty). Caller holds m.mu;
// the (off-lock) recompute on newFocus is the caller's responsibility.
func (m *Mux) removeLocked(id string) (removed *Session, newFocus string) {
	idx := -1
	for i, s := range m.sessions {
		if s.ID == id {
			idx = i
			break
		}
	}
	if idx < 0 {
		return nil, ""
	}
	removed = m.sessions[idx]
	m.sessions = append(m.sessions[:idx], m.sessions[idx+1:]...)
	if len(m.sessions) == 0 {
		m.focusIdx = -1
	} else if m.focusIdx >= len(m.sessions) {
		m.focusIdx = len(m.sessions) - 1
	}
	// Repoint any client focused on the removed session onto a surviving neighbor.
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
	return removed, newFocus
}

// onSessionExit removes a finished session (EOF-driven from the pump), re-focuses
// a neighbor, and repoints per-client focus via the shared removeLocked helper.
func (m *Mux) onSessionExit(id string) {
	m.mu.Lock()
	_, newFocus := m.removeLocked(id)
	m.mu.Unlock()
	if newFocus != "" {
		m.recompute(newFocus)
	}
}

// Kill force-closes one session: it synchronously removes the session from the
// sub-tab list and re-focuses a neighbor (the same bookkeeping onSessionExit
// performs, via the shared removeLocked helper), then closes the removed session
// (kills the child, closes the PTY). Killing the last session leaves the list
// empty — the daemon only auto-spawns at attach-handshake time, never on a later
// empty transition. Killing an unknown id is a no-op. Idempotent against the
// pump's own later onSessionExit(id) for the same session: by then the id is
// already gone, so removeLocked returns nil and onSessionExit is a harmless
// no-op (no panic, no double-remove).
func (m *Mux) Kill(id string) {
	m.mu.Lock()
	removed, newFocus := m.removeLocked(id)
	m.mu.Unlock()
	if removed == nil {
		return // unknown id (or already removed by onSessionExit) — no-op
	}
	if newFocus != "" {
		m.recompute(newFocus)
	}
	_ = removed.Close()
}

// AddSink registers an output consumer keyed by id (e.g. an attach client) and
// replays each session's recent output to it, so a freshly-attached client
// renders the current screen immediately instead of staying blank until the
// next repaint.
func (m *Mux) AddSink(id string, fn func(sessID string, b []byte)) {
	// Snapshot the current sessions, then replay their recent output SYNCHRONOUSLY
	// — and before the sink is registered for live fan-out — so the freshly
	// attached client renders the current screen immediately and strictly ahead of
	// any subsequent live frame (no interleave/reorder with the async pump). This
	// replay runs on the caller's (attach handler's) goroutine, so a slow client
	// only blocks its own attach, never the mux.
	m.mu.RLock()
	sessions := append([]*Session(nil), m.sessions...)
	m.mu.RUnlock()
	for _, s := range sessions {
		if h := s.History(); len(h) > 0 {
			fn(s.ID, h)
		}
	}

	sk := &sink{id: id, fn: fn, ch: make(chan sinkFrame, sinkBuf), done: make(chan struct{})}
	go sk.run()
	m.mu.Lock()
	// Replace any existing sink for this id, shutting the old pump down.
	if old := m.sinks[id]; old != nil {
		close(old.ch)
	}
	m.sinks[id] = sk
	m.mu.Unlock()
}

// RemoveSink deregisters an output consumer and stops its pump goroutine (closing
// the channel drains and exits run()). Closing under the lock makes the swap with
// the producer's RLock fan-out safe: pump never sends on a closed channel because
// it holds the RLock while delivering and RemoveSink takes the write lock.
func (m *Mux) RemoveSink(id string) {
	m.mu.Lock()
	sk := m.sinks[id]
	delete(m.sinks, id)
	m.mu.Unlock()
	if sk != nil {
		close(sk.ch)
	}
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
	return target.MaybeName(firstTurn, m.takenExcept(sessID))
}

// ApplyTitle feeds an LLM-generated title (derived from a session's first
// exchange) into the session, upgrading its provisional auto-name. Returns the
// new name if it changed.
func (m *Mux) ApplyTitle(sessID, title string) (bool, string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	target := m.getLocked(sessID)
	if target == nil {
		return false, ""
	}
	return target.ApplyTitle(title, m.takenExcept(sessID))
}

// Rename applies a manual name to a session (the GUI double-click / ⌃R path),
// disambiguated against the other sessions. ok is false if no such session
// exists. A manual name is sticky against later auto-naming/auto-titling.
func (m *Mux) Rename(sessID, name string) (newName string, ok bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	target := m.getLocked(sessID)
	if target == nil {
		return "", false
	}
	return target.Rename(name, m.takenExcept(sessID)), true
}

// takenExcept returns the set of session names excluding sessID, for
// disambiguation. Caller holds m.mu.
func (m *Mux) takenExcept(sessID string) map[string]bool {
	taken := make(map[string]bool, len(m.sessions))
	for _, s := range m.sessions {
		if s.ID != sessID {
			taken[s.Name] = true
		}
	}
	return taken
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
			ID: s.ID, Name: s.Name,
			Status: string(s.Status()), Focused: s.ID == focus,
		}
	}
	return out
}
