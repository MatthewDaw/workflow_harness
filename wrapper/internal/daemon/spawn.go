package daemon

import (
	"fmt"
	"os"
	"os/exec"
	"time"

	"github.com/workflow-harness/claude-plus/internal/diag"
)

// EnsureDaemon finds the daemon for repoRoot, or spawns a detached one and waits
// for its socket to come up. This is what bare `claude+` calls before Dial: it
// implements the attach-or-create contract (KTD2).
//
// The detached child re-execs this same binary with the hidden `__daemon` verb
// so the daemon runs independently of the launching shell and survives SSH
// disconnect.
func EnsureDaemon(repoRoot string) (Entry, error) {
	if e, ok, err := Find(repoRoot); err == nil && ok {
		// Reuse a daemon only when it is alive, speaks our ProtocolVersion, AND
		// already matches the requested permission posture. An older/newer-build
		// daemon that lingered across a rebuild still answers a ping (so the old
		// `alive` check happily reused it) but would dead-end the attach handshake
		// on a version mismatch — the exact lifecycle bug. Separately, a daemon
		// started WITHOUT dangerous mode would spawn children lacking
		// --dangerously-skip-permissions, so a launch that requests dangerous mode
		// must not silently reuse it. Either way: kill it and respawn a fresh,
		// matching daemon.
		comp := compatible(e.Sock)
		if reuseDaemon(comp, wantDangerous(), e.Dangerous) {
			return e, nil
		}
		// A compatible-but-non-dangerous daemon is being retired solely to honor
		// the dangerous-mode upgrade; tell the user so the restart isn't silent.
		if comp && wantDangerous() && !e.Dangerous {
			fmt.Fprintln(os.Stderr, "claude+: restarting daemon in dangerous mode (--dangerously-skip-permissions)")
		}
		// Stale, incompatible, or wrong permission posture: retire the daemon
		// (kill its PID so a wedged old image can no longer hold the port) and its
		// record, then respawn below.
		stopStale(e)
	}

	if err := spawnDaemon(repoRoot); err != nil {
		return Entry{}, err
	}

	// Wait for the daemon to register its loopback address and answer a ping with
	// a matching protocol version (compatible, not merely alive) so we never hand
	// back a record for a daemon the client cannot actually attach to.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if e, ok, _ := Find(repoRoot); ok && compatible(e.Sock) {
			return e, nil
		}
		time.Sleep(50 * time.Millisecond)
	}
	return Entry{}, os.ErrDeadlineExceeded
}

// reuseDaemon reports whether an existing daemon entry can be reused as-is for a
// new launch. A daemon is reusable only when it speaks our protocol (compatible)
// AND its permission posture already satisfies the launch: a launch that wants
// dangerous mode cannot reuse a daemon started without it, because that daemon's
// claude children would lack --dangerously-skip-permissions. (The reverse — a
// plain launch finding a dangerous daemon — is intentionally left reusable: the
// mode is fixed at start and we never downgrade a running daemon out from under
// existing sessions.)
func reuseDaemon(compatible, wantDangerous, entryDangerous bool) bool {
	if !compatible {
		return false
	}
	if wantDangerous && !entryDangerous {
		return false
	}
	return true
}

// wantDangerous reports whether this launch requested dangerous mode, carried
// from `claude+ --dangerously-skip-permissions` via CLAUDE_PLUS_DANGEROUS (set
// in main before any EnsureDaemon call).
func wantDangerous() bool { return os.Getenv("CLAUDE_PLUS_DANGEROUS") != "" }

// spawnDaemon launches a fresh detached daemon for repoRoot. It is a package
// var so lifecycle tests can substitute an in-process daemon (the real
// implementation re-execs this binary, which a `go test` process cannot do).
// The default re-execs the same binary with the hidden `__daemon` verb so the
// daemon runs independently of the launching shell and survives SSH disconnect.
var spawnDaemon = func(repoRoot string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	// Re-exec needs a path the OS loader accepts. On Windows that means a real
	// executable extension: a binary launched from Git Bash as an extension-less
	// `claude+` reports os.Executable() WITHOUT `.exe`, and exec.Command then fails
	// with "executable file not found in %PATH%" even though the file exists. Resolve
	// to the adjacent `<self>.exe` when needed so daemon self-spawn works regardless
	// of how the launcher was named. No-op on unix.
	self = resolveExe(self)
	cmd := exec.Command(self, "__daemon", repoRoot)
	cmd.Stdin = nil
	cmd.Stdout = nil
	cmd.Stderr = nil
	cmd.Env = append(os.Environ(), "CLAUDE_PLUS_DAEMON=1")
	detachAttr(cmd) // platform-specific: new session / process group
	if err := cmd.Start(); err != nil {
		return err
	}
	// Let the parent return; the child is detached.
	return cmd.Process.Release()
}

// RunDaemon is the entrypoint for the detached `__daemon` child process: it
// constructs the daemon, wires the capture+transport runtime (if HQ is
// configured), and serves until stopped.
func RunDaemon(repoRoot string) error {
	d, err := New(repoRoot, nil)
	if err != nil {
		return err
	}
	instanceID := repoKey(repoRoot) + "@" + hostName()

	// Part B cross-restart resume: load the persisted session store and, for each
	// session recorded before the daemon last stopped, recreate it under the SAME
	// id via `claude --resume <TabID>` and restore its naming latches — BEFORE the
	// runtime's capture loop starts, so the resumed sessions are already live in the
	// mux when captureLoop announces them. The store is then handed to the runtime,
	// which seeds each resumed session's tailer offset + seq floor from it and keeps
	// persisting (offset, NextSeq, name) as the conversation streams.
	store, err := Load(repoRoot)
	if err != nil {
		// A store load failure must never block the daemon; run without resume.
		store = nil
	}
	if store != nil {
		for _, ps := range store.Sessions() {
			s, serr := d.Mux().SpawnResumed(ps.TabID, ps.Name)
			if serr != nil {
				diag.Logf("resume: SpawnResumed %s failed: %v", ps.TabID, serr)
				continue
			}
			s.SeedNaming(ps.Name, ps.FirstSet, ps.TitleSet, ps.ManualName)
		}
	}

	rt := StartRuntimeWithStore(d, instanceID, store)
	defer func() {
		rt.Stop()
		if store != nil {
			store.Stop()
		}
	}()
	return d.Serve()
}
