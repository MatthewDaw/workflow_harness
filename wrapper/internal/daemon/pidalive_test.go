package daemon

import (
	"fmt"
	"os/exec"
	"runtime"
	"strings"
)

// pidRunning authoritatively reports whether a process with the given pid is
// running. It is deliberately independent of the production `processAlive`
// helper: on Windows that helper uses Signal(0), which returns an error for a
// ConPTY child even while it is alive, so it cannot prove process death here. We
// query the OS directly (tasklist on Windows, kill -0 via ps on Unix) so the
// force-shutdown proof rests on ground truth.
func pidRunning(pid int) bool {
	if pid <= 0 {
		return false
	}
	if runtime.GOOS == "windows" {
		out, err := exec.Command("tasklist", "/FI", fmt.Sprintf("PID eq %d", pid), "/FO", "CSV", "/NH").Output()
		if err != nil {
			return false
		}
		// tasklist prints the row only when the PID exists; otherwise it prints an
		// "INFO: No tasks…" line (or nothing for /NH). Match the quoted PID field.
		return strings.Contains(string(out), fmt.Sprintf("\",\"%d\",", pid))
	}
	// Unix: `kill -0` succeeds iff the process exists and is signalable.
	return exec.Command("kill", "-0", fmt.Sprintf("%d", pid)).Run() == nil
}
