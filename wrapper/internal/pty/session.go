package pty

import (
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"sync/atomic"
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

	// waited is closed by the single watcher goroutine (started in newSession)
	// once the child's cmd.Wait() returns. Close/Shutdown wait on this instead of
	// calling cmd.Wait() themselves — there must be exactly ONE Wait caller, or
	// the second concurrent Wait panics. nil for test-constructed sessions with no
	// child (no watcher is started), in which case awaitExit returns immediately.
	waited chan struct{}
	// exited records that the child process has actually exited. It is set by the
	// watcher and read concurrently by Alive (atomic so it needs no lock), so the
	// daemon's heartbeat loop can stop treating a dead-but-not-yet-removed child
	// (a ConPTY child that lingers in mux.List without an EOF) as live.
	exited atomic.Bool
	// onExit is invoked exactly once when the child actually exits (from the
	// watcher goroutine). The mux wires this to onSessionExit so the EOF path and
	// the Wait path converge on the same idempotent removeLocked bookkeeping.
	onExit func(id string)

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
	// Isolate requests an isolated, session-scoped CLAUDE_CONFIG_DIR for the
	// child (see config.BuildSessionConfigDir): product-bundled skills + claude+
	// session history stay out of the user's personal ~/.claude. Only DefaultSpawn
	// (the real `claude` launch) sets this; test/fake specs leave it false so they
	// never touch the developer's home.
	Isolate bool
}

// SpawnFunc produces the CmdSpec for a claude child. It is a field so tests can
// substitute a fake command without a real claude install.
type SpawnFunc func(repoRoot, sessionID string) CmdSpec

// toResumeSpec rewrites a fresh CmdSpec (as produced by a SpawnFunc) into a
// RESUME launch: it replaces a leading `--session-id <id>` pair with `--resume
// <id>` so the child reattaches to the EXISTING Claude conversation instead of
// starting a new one. This is the chosen seam for Part B's cross-restart resume:
//
//   - The injectable SpawnFunc signature (and every test fake) stays UNCHANGED —
//     resume is a post-transform on whatever spec the spawn func returned, so a
//     fake spawn (e.g. `cat`/`echo`) keeps working as-is and is never forced to
//     understand resume.
//   - DefaultSpawn's fresh `--session-id` behavior is left fully intact; resume
//     mode is opt-in per launch (mux.SpawnResumed), not a change to the default.
//   - CLAUDE_PLUS_DANGEROUS, Isolate, Dir and Env are preserved because we only
//     touch the leading id flag and leave the rest of the spec alone.
//
// If the spec does not begin with `--session-id <id>` (a fake/test spec, or a
// future spawn func that names the id differently), toResumeSpec prepends
// `--resume <id>` so the child still resumes; it never drops the caller's args.
func toResumeSpec(spec CmdSpec, sessionID string) CmdSpec {
	args := spec.Args
	if len(args) >= 2 && args[0] == "--session-id" && args[1] == sessionID {
		// Swap the fresh id flag for the resume flag, keeping any trailing flags
		// (e.g. --dangerously-skip-permissions) the spawn func appended.
		rest := append([]string(nil), args[2:]...)
		spec.Args = append([]string{"--resume", sessionID}, rest...)
		return spec
	}
	// No recognizable `--session-id <id>` lead (test/fake spec): resume by
	// prepending the flag without discarding the caller's own args.
	spec.Args = append([]string{"--resume", sessionID}, append([]string(nil), args...)...)
	return spec
}

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
	env := append(os.Environ(), "CLAUDE_PLUS_SESSION="+sessionID)
	// HumanLayer's ACE workflow (the imported research/plan skills) leans on the
	// extended-thinking magic words baked into those skill bodies ("ultrathink",
	// "think deeply"). Their .claude/settings.json raises the thinking ceiling to
	// 32000 so "ultrathink" can actually spend its full budget; we mirror that as a
	// DEFAULT here (commands import as skills, which have no model/thinking pin of
	// their own — claude.go materializes no settings.json env we control). It is a
	// no-clobber default: if the developer already exported MAX_THINKING_TOKENS, we
	// leave their value untouched.
	if os.Getenv("MAX_THINKING_TOKENS") == "" {
		env = append(env, "MAX_THINKING_TOKENS=32000")
	}
	return CmdSpec{
		Name: "claude",
		Args: args,
		Dir:  repoRoot,
		// Also tag the child via the environment for any out-of-band correlation.
		Env: env,
		// Run against an isolated ~/.claude+ config root (U21).
		Isolate: true,
	}
}

// newSession starts a claude child under a PTY with the given dimensions. onExit
// is invoked exactly once when the child process actually exits (see the watcher
// goroutine below); pass nil to opt out. The mux passes m.onSessionExit so the
// Wait path and the EOF/pump path both funnel into the same removal bookkeeping.
//
// When resume is true the produced CmdSpec is rewritten to launch `claude
// --resume <id>` (see toResumeSpec) so the child reattaches to the existing
// conversation across a daemon restart, rather than starting a fresh one.
func newSession(id, name, repoRoot string, cols, rows int, spawn SpawnFunc, resume bool, onExit func(id string)) (*Session, error) {
	spec := spawn(repoRoot, id)
	if resume {
		spec = toResumeSpec(spec, id)
	}

	// Point the real claude launch at the stable isolated config root ~/.claude+
	// (U21), so bundled skills + claude+ history stay out of the user's personal
	// ~/.claude while auth/transcripts/settings persist across restarts. A failure
	// here must never block a session — fall back to the inherited ~/.claude.
	if spec.Isolate {
		if dir, err := config.EnsureConfigDir(repoRoot); err == nil {
			spec.Env = append(spec.Env, "CLAUDE_CONFIG_DIR="+dir)
		} else {
			log.Printf("pty: isolated config root failed, using ~/.claude: %v", err)
		}
		// Inject this project's HQ identity so the inner session (and any tool it
		// runs) sees the SAME project id / repo / API base the daemon derives, all
		// from the single config.ProjectIDFor derivation.
		spec.Env = append(spec.Env,
			"CLAUDE_PLUS_PROJECT_ID="+config.ProjectIDFor(repoRoot),
			"CLAUDE_PLUS_REPO="+config.RepoNameFor(repoRoot))
		if base, ok := config.APIBase(); ok {
			spec.Env = append(spec.Env, "CLAUDE_PLUS_API_URL="+base)
		}
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
	s := &Session{
		ID: id, Name: name,
		cmd: c, pt: pt, status: StatusActive, cols: cols, rows: rows,
		onExit: onExit,
		waited: make(chan struct{}),
	}
	// The SINGLE owner of the child's Wait(). It reaps the process exactly once,
	// flips the exited flag, closes waited (so Close/Shutdown can block on the
	// real exit instead of double-Waiting), and finally fans the exit out via
	// onExit. Close/Shutdown must NOT call cmd.Wait() — a second concurrent Wait
	// panics; they wait on this channel instead.
	go func() {
		_ = c.Wait()
		s.exited.Store(true)
		close(s.waited)
		if s.onExit != nil {
			s.onExit(s.ID)
		}
	}()
	return s, nil
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

// PID returns the operating-system process id of the session's child, or 0 if
// the session has no live child (test-constructed or already torn down). Used by
// integration tests to prove the child process actually dies on terminate.
func (s *Session) PID() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.cmd == nil || s.cmd.Process == nil {
		return 0
	}
	return s.cmd.Process.Pid
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

// SeedNaming restores a resumed session's naming state from the persistence
// store (see daemon/sessions_store.go). It sets the displayed Name and the
// firstSet/titleSet/manualName latches under s.mu so the capture layer's later
// auto-name (MaybeName) and auto-title (ApplyTitle) do NOT clobber the name the
// session already carried before the daemon restarted. An empty name leaves the
// current Name untouched (the latches are still applied), so a partially-named
// session keeps whatever it had. Call this immediately after SpawnResumed,
// before the session can observe any new transcript turns.
func (s *Session) SeedNaming(name string, firstSet, titleSet, manualName bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if name != "" {
		s.Name = name
	}
	s.firstSet = firstSet
	s.titleSet = titleSet
	s.manualName = manualName
}

// NamingState returns the session's current display name and its three naming
// latches (firstSet, titleSet, manualName) under the lock. The daemon's
// cross-restart resume store snapshots these so a restart can SeedNaming them
// back and the capture layer's auto-name/auto-title do not clobber a name the
// session already carried (Part B). It mirrors SeedNaming's field set.
func (s *Session) NamingState() (name string, firstSet, titleSet, manualName bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.Name, s.firstSet, s.titleSet, s.manualName
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
	var proc *os.Process
	if s.cmd != nil {
		proc = s.cmd.Process
	}
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
	// deliverable here (Windows), so go straight to the force path. The watcher
	// goroutine owns cmd.Wait(); we observe the exit via the waited channel.
	if proc.Signal(syscall.SIGTERM) == nil {
		select {
		case <-s.waited:
			// Exited within the grace window.
			closePTY()
			return nil
		case <-time.After(timeout):
			// Ignored SIGTERM — escalate to a force kill of the whole tree.
		}
		killTree(proc.Pid)
		_ = proc.Kill() // safety net for the top process
		s.awaitExit(timeout)
		closePTY()
		return nil
	}

	// Force path (graceful unsupported, e.g. Windows): kill the whole process
	// tree now — the real claude is a node launcher plus a worker under ConPTY, so
	// killing only the PTY top process leaves a survivor that keeps writing the
	// transcript and firing hooks (which would revive the row). Then wait on the
	// watcher's exit rather than calling cmd.Wait() ourselves.
	closePTY()
	killTree(proc.Pid)
	_ = proc.Kill()
	s.awaitExit(timeout)
	return nil
}

// Close force-terminates the child process tree and closes the PTY. Like
// Shutdown it is idempotent (the closed guard) and never calls cmd.Wait() — the
// watcher goroutine started in newSession is the single Wait owner; Close kills
// the tree and blocks on the watcher's waited channel (with a short timeout).
func (s *Session) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	s.status = StatusDone
	var proc *os.Process
	if s.cmd != nil {
		proc = s.cmd.Process
	}
	s.mu.Unlock()

	_ = s.pt.Close()
	if proc != nil {
		// Kill the whole descendant tree, not just the PTY top process, so no
		// survivor (the node worker under ConPTY) keeps writing the transcript or
		// firing hooks and revives a force-shut-down session.
		killTree(proc.Pid)
		_ = proc.Kill() // safety net for the top process
	}
	// Do NOT call cmd.Wait(): the watcher owns it. Block on its completion with a
	// short timeout so Close stays bounded even if the OS is slow to reap.
	s.awaitExit(5 * time.Second)
	return nil
}

// Alive reports whether the session's child is still running. It is false once
// the child has actually exited (the watcher set exited) OR the session has been
// closed/shut down. The daemon's heartbeat loop uses this so a dead-but-not-yet-
// removed child (a ConPTY child that lingers in mux.List without an EOF) is not
// reported as live.
func (s *Session) Alive() bool {
	if s.exited.Load() {
		return false
	}
	s.mu.RLock()
	closed := s.closed
	s.mu.RUnlock()
	return !closed
}

// awaitExit blocks until the watcher signals the child has exited (waited
// closed) or the timeout elapses, whichever comes first. It returns immediately
// when there is no watcher (waited is nil) — i.e. a test-constructed session
// with no real child.
func (s *Session) awaitExit(timeout time.Duration) {
	if s.waited == nil {
		return
	}
	select {
	case <-s.waited:
	case <-time.After(timeout):
	}
}
