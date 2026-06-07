//go:build windows

package pty

import (
	"os/exec"
	"strconv"
	"syscall"
)

// killTree forcibly terminates the process named by pid AND its entire descendant
// tree. The real `claude` child is a process tree (a node launcher plus the model
// worker and any tools it spawns) running under a ConPTY; killing only the PTY's
// top process (os.Process.Kill) leaves the worker alive, and that survivor keeps
// writing the transcript and firing hooks — which re-emit `active`/activity and
// make a force-shut-down session "come back online". `taskkill /T` walks the tree
// by parent PID and `/F` forces termination, so the whole conversation dies.
//
// Best-effort: a process that is already gone (taskkill exits non-zero) is not an
// error. CREATE_NO_WINDOW keeps the detached daemon from flashing a console.
func killTree(pid int) {
	if pid <= 0 {
		return
	}
	cmd := exec.Command("taskkill", "/F", "/T", "/PID", strconv.Itoa(pid))
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
	_ = cmd.Run()
}
