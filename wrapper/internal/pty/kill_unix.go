//go:build !windows

package pty

import "syscall"

// killTree forcibly terminates the process named by pid AND its descendants. The
// real `claude` child runs under a PTY as a session/group leader (go-pty starts
// it with setsid), so the child's PID is also its process-group id; signalling the
// NEGATIVE pid delivers SIGKILL to every process in that group — the node launcher,
// the worker, and any tools it spawned. Killing only the top process would leave a
// survivor that keeps writing the transcript and firing hooks, which re-emits
// activity and makes a force-shut-down session "come back online".
//
// We send the group kill first, then a direct kill as a fallback for the case the
// child is not a group leader. Both are best-effort: an already-dead process (ESRCH)
// is not an error here.
func killTree(pid int) {
	if pid <= 0 {
		return
	}
	_ = syscall.Kill(-pid, syscall.SIGKILL)
	_ = syscall.Kill(pid, syscall.SIGKILL)
}
