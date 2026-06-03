//go:build !windows

package main

import (
	"os"
	"syscall"
)

// processAlive reports whether a process with the given PID is currently
// running. On unix, signal 0 performs an existence/permission check without
// actually delivering a signal: a nil error (or EPERM — alive but not ours)
// means the process exists.
func processAlive(pid int) bool {
	p, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	err = p.Signal(syscall.Signal(0))
	if err == nil {
		return true
	}
	return err == syscall.EPERM
}
