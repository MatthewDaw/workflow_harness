//go:build windows

package main

import "syscall"

// processAlive reports whether a process with the given PID is currently running
// on Windows.
//
// os.FindProcess always succeeds (even for dead PIDs), so we open the process and
// ask the kernel directly. The naive check — GetExitCodeProcess == STILL_ACTIVE
// (259) — is ambiguous: a process that genuinely exits with code 259 reports the
// same value as a running one, so a real exit would wrongly look alive and the
// GUI single-instance guard would never relaunch (#13). Instead we wait on the
// process handle with a zero timeout: WAIT_TIMEOUT means the handle is not
// signaled (still running); WAIT_OBJECT_0 means it has terminated, whatever its
// exit code. We only fall back to the exit-code probe if the wait itself errors.
func processAlive(pid int) bool {
	const (
		processQueryLimitedInformation = 0x1000
		synchronize                    = 0x00100000
		stillActive                    = 259
		waitObject0                    = 0x00000000
		waitTimeout                    = 0x00000102
	)
	// SYNCHRONIZE is required to wait on the handle; QUERY_LIMITED_INFORMATION for
	// the exit-code fallback.
	h, err := syscall.OpenProcess(processQueryLimitedInformation|synchronize, false, uint32(pid))
	if err != nil {
		return false // PID gone / not ours -> treat as not running
	}
	defer syscall.CloseHandle(h)

	switch ev, werr := syscall.WaitForSingleObject(h, 0); {
	case werr != nil:
		// Wait failed (e.g. handle lacked SYNCHRONIZE on a locked-down process).
		// Fall back to the exit-code probe, but disambiguate 259: a process that
		// reports STILL_ACTIVE is only "alive" if no termination is otherwise
		// observable. Without the wait we can't be certain, so prefer the
		// conservative reading and report alive only on STILL_ACTIVE.
		var code uint32
		if gerr := syscall.GetExitCodeProcess(h, &code); gerr != nil {
			return false
		}
		return code == stillActive
	case ev == waitTimeout:
		return true // not signaled -> still running
	case ev == waitObject0:
		return false // signaled -> has exited (even if exit code happens to be 259)
	default:
		return false
	}
}
