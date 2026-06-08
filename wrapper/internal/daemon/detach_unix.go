//go:build !windows

package daemon

import (
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

// detachAttr puts the daemon child in its own session so it is not killed when
// the launching shell (or the SSH connection) goes away.
func detachAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}

// resolveExe returns the path to re-exec for the daemon self-spawn. On unix any
// regular file is directly executable, so os.Executable()'s path is used as-is.
func resolveExe(self string) string { return self }

// listenerPID returns the PID LISTENING on sock's TCP port, or 0 when nothing
// listens. It lets the registry free a port held by a wedged/stale daemon when
// the recorded PID is no longer reliable after a messy exit. Best-effort: if
// lsof is unavailable it returns 0 and callers fall back to PID-based teardown.
func listenerPID(sock string) int {
	_, port, err := net.SplitHostPort(sock)
	if err != nil || port == "" {
		return 0
	}
	out, err := exec.Command("lsof", "-nP", "-iTCP:"+port, "-sTCP:LISTEN", "-t").Output()
	if err != nil {
		return 0
	}
	for _, f := range strings.Fields(string(out)) {
		if pid, err := strconv.Atoi(f); err == nil {
			return pid
		}
	}
	return 0
}

// terminatePID forcibly stops the daemon process group on unix. The daemon is
// launched with Setsid (its own session), so the registry PID is a session/group
// leader; signalling the negative PID reaps any child it spawned (claude PTYs)
// too. We escalate to SIGKILL because a stale/mismatched daemon may be wedged.
// A missing process (already dead) is not an error.
func terminatePID(pid int) {
	if pid <= 0 {
		return
	}
	p, err := os.FindProcess(pid)
	if err != nil {
		return
	}
	// Try a graceful group TERM first, then a hard group KILL. Fall back to the
	// single process if the group signal is rejected (not a leader).
	if err := syscall.Kill(-pid, syscall.SIGTERM); err != nil {
		_ = p.Signal(syscall.SIGTERM)
	}
	if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil {
		_ = p.Kill()
	}
}

// processAlive reports whether a PID names a live process (signal 0 probe).
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	p, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	return p.Signal(syscall.Signal(0)) == nil
}
