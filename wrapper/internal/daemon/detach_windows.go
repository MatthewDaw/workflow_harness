//go:build windows

package daemon

import (
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
