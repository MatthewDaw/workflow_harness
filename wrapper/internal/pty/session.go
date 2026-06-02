package pty

import (
	"io"
	"os"
	"sync"

	pty "github.com/aymanbagabas/go-pty"
)

// Status mirrors the session lifecycle status used in the event contract.
type Status string

const (
	StatusActive     Status = "active"
	StatusNeedsInput Status = "needs_input"
	StatusIdle       Status = "idle"
	StatusDone       Status = "done"
)

// Session is one `claude` child process running under a PTY. The daemon owns a
// set of these and multiplexes them; only the focused session's output is
// streamed to the attached client, but all sessions keep running.
//
// The PTY is provided by go-pty, which uses ConPTY on Windows and native
// pseudo-terminals on macOS/Linux — so a session hosts a real terminal on every
// platform.
type Session struct {
	ID     string
	Name   string
	Ticket string

	cmd *pty.Cmd
	pt  pty.Pty // the pseudo-terminal (master/console)

	mu       sync.RWMutex
	status   Status
	closed   bool
	cols     int
	rows     int
	firstSet bool // whether AutoName already consumed a first turn
}

// CmdSpec describes the child process to launch under a PTY. It is platform
// neutral (name + args + dir + env) so go-pty can build the command for the
// host OS; tests substitute a cross-platform fake (e.g. `cat` / `more`).
type CmdSpec struct {
	Name string
	Args []string
	Dir  string
	Env  []string
}

// SpawnFunc produces the CmdSpec for a claude child. It is a field so tests can
// substitute a fake command without a real claude install.
type SpawnFunc func(repoRoot, sessionID string) CmdSpec

// DefaultSpawn launches the real `claude` CLI in the repo root.
func DefaultSpawn(repoRoot, sessionID string) CmdSpec {
	return CmdSpec{
		Name: "claude",
		Dir:  repoRoot,
		// Tag the child so the capture layer can correlate its transcript.
		Env: append(os.Environ(), "CLAUDE_PLUS_SESSION="+sessionID),
	}
}

// newSession starts a claude child under a PTY with the given dimensions.
func newSession(id, name, ticket, repoRoot string, cols, rows int, spawn SpawnFunc) (*Session, error) {
	spec := spawn(repoRoot, id)
	pt, err := pty.New()
	if err != nil {
		return nil, err
	}
	c := pt.Command(spec.Name, spec.Args...)
	if spec.Dir != "" {
		c.Dir = spec.Dir
	}
	if spec.Env != nil {
		c.Env = spec.Env
	}
	if err := c.Start(); err != nil {
		_ = pt.Close()
		return nil, err
	}
	// Size the pseudo-terminal once the child is attached.
	_ = pt.Resize(cols, rows)
	return &Session{
		ID: id, Name: name, Ticket: ticket,
		cmd: c, pt: pt, status: StatusActive, cols: cols, rows: rows,
	}, nil
}

// Write sends bytes to the session's PTY stdin (used by focus input + inject).
func (s *Session) Write(p []byte) (int, error) {
	s.mu.RLock()
	closed := s.closed
	s.mu.RUnlock()
	if closed {
		return 0, io.ErrClosedPipe
	}
	return s.pt.Write(p)
}

// Read reads bytes from the session's PTY stdout. The mux pumps this into a
// per-session output buffer / fan-out.
func (s *Session) Read(p []byte) (int, error) { return s.pt.Read(p) }

// Resize propagates new terminal dimensions to the PTY.
func (s *Session) Resize(cols, rows int) error {
	s.mu.Lock()
	s.cols, s.rows = cols, rows
	s.mu.Unlock()
	return s.pt.Resize(cols, rows)
}

// SetStatus updates the cached lifecycle status (driven by capture hooks).
func (s *Session) SetStatus(st Status) {
	s.mu.Lock()
	s.status = st
	s.mu.Unlock()
}

// Status returns the cached lifecycle status.
func (s *Session) Status() Status {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.status
}

// MaybeName applies an auto-name from the first user turn exactly once. It is a
// no-op if the session already has a ticket-derived or manual name, or once a
// first turn has already been consumed.
func (s *Session) MaybeName(firstTurn string, taken map[string]bool) (renamed bool, newName string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.firstSet || s.Ticket != "" {
		s.firstSet = true
		return false, s.Name
	}
	s.firstSet = true
	n := AutoName("", firstTurn)
	if n == "" {
		return false, s.Name
	}
	n = Disambiguate(n, taken)
	if n == s.Name {
		return false, s.Name
	}
	s.Name = n
	return true, n
}

// Rename forces a manual name (⌃R), disambiguated against taken names.
func (s *Session) Rename(name string, taken map[string]bool) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := Disambiguate(Slug(name, 5), taken)
	s.Name = n
	s.firstSet = true
	return n
}

// Close terminates the child process and closes the PTY.
func (s *Session) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	s.status = StatusDone
	s.mu.Unlock()
	_ = s.pt.Close()
	if s.cmd.Process != nil {
		_ = s.cmd.Process.Kill()
	}
	return s.cmd.Wait()
}
