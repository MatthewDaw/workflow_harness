//go:build !windows

package daemon

import (
	"os"
	"os/exec"
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
