//go:build windows

package main

import "syscall"

// processAlive reports whether a process with the given PID is currently
// running on Windows. os.FindProcess always succeeds (even for dead PIDs), so
// we open the process and inspect its exit code: STILL_ACTIVE (259) means it is
// still running. A failed open (e.g. the PID is gone) means "not running".
func processAlive(pid int) bool {
	const (
		processQueryLimitedInformation = 0x1000
		stillActive                    = 259
	)
	h, err := syscall.OpenProcess(processQueryLimitedInformation, false, uint32(pid))
	if err != nil {
		return false
	}
	defer syscall.CloseHandle(h)
	var code uint32
	if err := syscall.GetExitCodeProcess(h, &code); err != nil {
		return false
	}
	return code == stillActive
}
