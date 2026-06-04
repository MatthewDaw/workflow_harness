package pty

import (
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	pty "github.com/aymanbagabas/go-pty"

	"github.com/workflow-harness/claude-plus/internal/config"
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

	cmd *pty.Cmd
	pt  pty.Pty // the pseudo-terminal (master/console)

	mu         sync.RWMutex
	status     Status
	closed     bool
	cols       int
	rows       int
	firstSet   bool // whether the provisional first-turn auto-name was consumed
	titleSet   bool // whether the LLM-generated title was applied (apply once)
	manualName bool // user set the name explicitly; auto-naming must not override

	histMu sync.Mutex
	hist   []byte // recent raw PTY output, replayed to newly-attached sinks
}

// maxHist caps a session's replay buffer. claude is a full-screen TUI that
// repaints frequently, so a freshly-attached client only needs enough recent
// output to reconstruct the current frame; the next repaint corrects any escape
// sequence clipped at the trim boundary.
const maxHist = 256 * 1024

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

// DefaultSpawn launches the real `claude` CLI in the repo root. When the daemon
// runs in dangerous mode — CLAUDE_PLUS_DANGEROUS is set, propagated from
// `claude+ --dangerously-skip-permissions` — every spawned child inherits
// `--dangerously-skip-permissions` so sub-agents share the wrapper's permission
// posture.
func DefaultSpawn(repoRoot, sessionID string) CmdSpec {
	// Pin Claude Code's session id to ours so its transcript is written as
	// <sessionID>.jsonl — the exact path the capture tailer reads. Without this,
	// claude generates its own UUID and the tailer follows a file that never
	// exists, so no tool/message/cost events are ever captured.
	args := []string{"--session-id", sessionID}
	if os.Getenv("CLAUDE_PLUS_DANGEROUS") != "" {
		args = append(args, "--dangerously-skip-permissions")
	}
	return CmdSpec{
		Name: "claude",
		Args: args,
		Dir:  repoRoot,
		// Also tag the child via the environment for any out-of-band correlation.
		Env: append(os.Environ(), "CLAUDE_PLUS_SESSION="+sessionID),
	}
}

// newSession starts a claude child under a PTY with the given dimensions.
func newSession(id, name, repoRoot string, cols, rows int, spawn SpawnFunc) (*Session, error) {
	spec := spawn(repoRoot, id)

	// Point the real claude launch at the stable isolated config root ~/.claude+
	// (U21), so bundled skills + claude+ history stay out of the user's personal
	// ~/.claude while auth/transcripts/settings persist across restarts. A failure
	// here must never block a session — fall back to the inherited ~/.claude.
	if dir, err := config.EnsureConfigDir(); err == nil {
		spec.Env = append(spec.Env, "CLAUDE_CONFIG_DIR="+dir)
	} else {
		log.Printf("pty: isolated config root failed, using ~/.claude: %v", err)
	}

	// Resolve a bare command name against PATH up front. go-pty/os-exec would
	// otherwise resolve it relative to Dir (the repo root) and fail to find a
	// PATH binary like `claude` once a working directory is set.
	cmdName := spec.Name
	if filepath.Base(cmdName) == cmdName {
		if lp, lpErr := exec.LookPath(cmdName); lpErr == nil {
			cmdName = lp
		}
	}
	pt, err := pty.New()
	if err != nil {
		return nil, err
	}
	c := pt.Command(cmdName, spec.Args...)
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
		ID: id, Name: name,
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

// recordHist appends raw output to the replay buffer, trimming the oldest bytes
// once it exceeds maxHist.
func (s *Session) recordHist(b []byte) {
	s.histMu.Lock()
	defer s.histMu.Unlock()
	s.hist = append(s.hist, b...)
	if len(s.hist) > maxHist {
		trimmed := make([]byte, maxHist)
		copy(trimmed, s.hist[len(s.hist)-maxHist:])
		s.hist = trimmed
	}
}

// History returns a copy of the session's replay buffer.
func (s *Session) History() []byte {
	s.histMu.Lock()
	defer s.histMu.Unlock()
	out := make([]byte, len(s.hist))
	copy(out, s.hist)
	return out
}

// Resize propagates new terminal dimensions to the PTY. It records the size even
// when there is no underlying PTY (s.pt == nil), which only happens for
// test-constructed sessions — production sessions always have a live PTY.
func (s *Session) Resize(cols, rows int) error {
	s.mu.Lock()
	s.cols, s.rows = cols, rows
	pt := s.pt
	s.mu.Unlock()
	if pt == nil {
		return nil
	}
	return pt.Resize(cols, rows)
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

// MaybeName applies a provisional auto-name from the first user turn exactly
// once. It is a no-op once a first turn has already been consumed, or if the
// user has already set a manual name. The richer LLM title (ApplyTitle) later
// upgrades this provisional slug.
func (s *Session) MaybeName(firstTurn string, taken map[string]bool) (renamed bool, newName string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.firstSet || s.manualName {
		return false, s.Name
	}
	s.firstSet = true
	n := AutoName(firstTurn)
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

// ApplyTitle upgrades the session to an LLM-generated title derived from its
// first exchange, overriding the provisional first-turn slug. It applies at most
// once and never overrides a manual rename. title is raw text; it is slugged and
// disambiguated here.
func (s *Session) ApplyTitle(title string, taken map[string]bool) (renamed bool, newName string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.manualName || s.titleSet {
		return false, s.Name
	}
	s.titleSet = true
	n := Slug(title, 5)
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

// Rename forces a manual name (the GUI double-click / ⌃R path), disambiguated
// against taken names. A manual name is sticky: it locks out both the
// provisional auto-name and the LLM title so neither overrides the user's choice.
func (s *Session) Rename(name string, taken map[string]bool) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := Disambiguate(Slug(name, 5), taken)
	s.Name = n
	s.firstSet = true
	s.titleSet = true
	s.manualName = true
	return n
}

// Shutdown terminates the session gracefully: it sends SIGTERM to the child and
// waits up to `timeout` for it to exit, escalating to a force kill if it does
// not. On platforms where a graceful signal is not deliverable to a child (e.g.
// Windows, where os.Process.Signal rejects everything but Kill), or when there
// is no live child, it falls back to the force path. Like Close it is idempotent
// and leaves the session StatusDone with its PTY closed. It returns nil on any
// successful termination — the child's signal-induced exit code is not an error
// here, since the termination was intentional.
func (s *Session) Shutdown(timeout time.Duration) error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	s.status = StatusDone
	proc := s.cmd.Process
	pt := s.pt
	s.mu.Unlock()

	closePTY := func() {
		if pt != nil {
			_ = pt.Close()
		}
	}

	// No live child (test-constructed or never started): just close the PTY.
	if proc == nil {
		closePTY()
		return nil
	}

	// Try graceful terminate. A non-nil error from Signal means SIGTERM is not
	// deliverable here (Windows), so go straight to the force path.
	if proc.Signal(syscall.SIGTERM) == nil {
		done := make(chan error, 1)
		go func() { done <- s.cmd.Wait() }()
		select {
		case <-done:
			// Exited within the grace window.
			closePTY()
			return nil
		case <-time.After(timeout):
			// Ignored SIGTERM — escalate to a force kill.
		}
		_ = proc.Kill()
		<-done // reap the in-flight Wait so the child is not left a zombie
		closePTY()
		return nil
	}

	// Force path (graceful unsupported): kill now, mirroring Close's order.
	closePTY()
	_ = proc.Kill()
	_ = s.cmd.Wait()
	return nil
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
