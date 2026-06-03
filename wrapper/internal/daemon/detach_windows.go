//go:build windows

package daemon

import (
	"os"
	"os/exec"
	"syscall"
)

// detachAttr puts the daemon child in its own process group on Windows so it is
// not signaled when the launching console closes. Note: Unix-domain sockets are
// supported on modern Windows 10+; this build is provided for completeness, but
// the primary distribution targets are darwin/linux (see .goreleaser.yaml).
func detachAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP,
	}
}

// terminatePID forcibly stops the daemon process on Windows. There is no signal
// model, so we go straight to a hard kill (TerminateProcess). A missing process
// (already dead) is not an error.
func terminatePID(pid int) {
	if pid <= 0 {
		return
	}
	if p, err := os.FindProcess(pid); err == nil {
		_ = p.Kill()
	}
}

// processAlive reports whether a PID names a live process. On Windows
// os.FindProcess always succeeds, so we open the handle and check the exit code:
// a still-running process reports STILL_ACTIVE (259).
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	p, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	// Signal(0) on Windows reports an error once the process has exited.
	return p.Signal(syscall.Signal(0)) == nil
}
