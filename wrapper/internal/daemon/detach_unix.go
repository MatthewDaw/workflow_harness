//go:build !windows

package daemon

import (
	"os/exec"
	"syscall"
)

// detachAttr puts the daemon child in its own session so it is not killed when
// the launching shell (or the SSH connection) goes away.
func detachAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}
