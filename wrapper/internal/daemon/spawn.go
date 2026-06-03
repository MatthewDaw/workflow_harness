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
		// Only reuse a daemon that is alive AND speaks our ProtocolVersion. An
		// older/newer-build daemon that lingered across a rebuild still answers a
		// ping (so the old `alive` check happily reused it) but would dead-end the
		// attach handshake on a version mismatch — the exact lifecycle bug. Treat
		// such a daemon as stale: kill it and respawn a fresh, compatible one.
		if compatible(e.Sock) {
			return e, nil
		}
		// Stale or incompatible record: retire the daemon (kill its PID so a
		// wedged old image can no longer hold the port) and its record, then
		// respawn below.
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
	rt := StartRuntime(d, instanceID)
	defer rt.Stop()
	return d.Serve()
}
