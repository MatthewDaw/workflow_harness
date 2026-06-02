package daemon

import (
	"os"
	"os/exec"
	"time"
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
		if alive(e.Sock) {
			return e, nil
		}
		// Stale record: clean it and respawn.
		_ = removeMeta(repoRoot)
	}

	self, err := os.Executable()
	if err != nil {
		return Entry{}, err
	}
	cmd := exec.Command(self, "__daemon", repoRoot)
	cmd.Stdin = nil
	cmd.Stdout = nil
	cmd.Stderr = nil
	cmd.Env = append(os.Environ(), "CLAUDE_PLUS_DAEMON=1")
	detachAttr(cmd) // platform-specific: new session / process group
	if err := cmd.Start(); err != nil {
		return Entry{}, err
	}
	// Let the parent return; the child is detached.
	_ = cmd.Process.Release()

	// Wait for the socket to answer.
	sock, err := SockPath(repoRoot)
	if err != nil {
		return Entry{}, err
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if alive(sock) {
			if e, ok, _ := Find(repoRoot); ok {
				return e, nil
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	return Entry{}, os.ErrDeadlineExceeded
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
	rt := StartRuntime(d, instanceID)
	defer rt.Stop()
	return d.Serve()
}
